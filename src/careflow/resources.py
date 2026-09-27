"""预约资源分配：人员、诊室（含清洁准备间隔）与治疗设备的统一占用规则。

所有占用区间按 UTC 存储并做半开区间 [from, until) 重叠判断，诊所时区只影响
日历展示与运营日界；确认、改期、终态释放在同一事务内完成，保证多资源原子性。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .db import Database
from .errors import Conflict, NotFound, ValidationError
from .ids import new_id
from .security import authorize, principal_for
from .validation import calendar_date, choice, integer, parsed_timestamp, text, timestamp

RESOURCE_TABLES = {"staff": "staff", "room": "rooms", "device": "devices"}
RESOURCE_LABELS = {"staff": "人员", "room": "诊室", "device": "设备"}
ROOM_KINDS = {"consult", "treatment", "recovery"}

# 终态释放规则：取消与占位到期立即释放全部资源；未到诊时人员与设备立即释放、
# 诊室保留到原定结束时刻（不再追加清洁间隔）；完成服务时人员与设备立即释放、
# 诊室保留到清洁准备间隔结束。
TERMINAL_OUTCOMES = {"cancelled", "no_show", "completed", "hold_expired"}


def parse_device_ids(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 20:
        raise ValidationError("设备清单必须为不超过 20 项的数组")
    result = []
    for item in value:
        item = text(item, "设备编号", maximum=80)
        if item in result:
            raise ValidationError("设备清单不能重复")
        result.append(item)
    return result


def resolve_resources(connection, clinic_id: str, staff_id: str | None,
                      room_id: str | None, device_ids: list[str]) -> list[dict[str, Any]]:
    """读取并校验本次预约需要的全部资源；任一缺失或停用即失败。"""
    wanted: list[tuple[str, str]] = []
    if staff_id:
        wanted.append(("staff", staff_id))
    if room_id:
        wanted.append(("room", room_id))
    wanted.extend(("device", device_id) for device_id in device_ids)
    resources = []
    for resource_type, resource_id in wanted:
        table = RESOURCE_TABLES[resource_type]
        row = connection.execute(f"SELECT * FROM {table} WHERE id=? AND clinic_id=?",
                                 (resource_id, clinic_id)).fetchone()
        label = RESOURCE_LABELS[resource_type]
        if row is None:
            raise ValidationError(f"预约{label}不存在")
        if not row["active"]:
            raise ValidationError(f"预约{label}已停用")
        resources.append({"type": resource_type, "id": resource_id, "name": row["display_name"] if resource_type == "staff" else row["name"],
                          "version": row["version"],
                          "turnover_minutes": row["turnover_minutes"] if resource_type == "room" else 0})
    return resources


def resource_versions(resources: list[dict[str, Any]]) -> dict[str, str]:
    return {f"{item['type']}:{item['id']}": item["version"] for item in resources}


def _overlaps(connection, clinic_id: str, resource_type: str, resource_id: str,
              start: str, end: str, now: str, exclude_appointment: str | None):
    # 延迟释放未到期的诊室（未到诊保留、完成后的清洁准备间隔）仍算被占用。
    return connection.execute(
        "SELECT r.id,r.appointment_id,r.occupied_from,r.occupied_until,a.state AS appointment_state,a.kind "
        "FROM appointment_resources r JOIN appointments a ON a.id=r.appointment_id "
        "WHERE r.clinic_id=? AND r.resource_type=? AND r.resource_id=? AND r.state IN ('held','booked') "
        "AND (a.state IN ('held','booked','arrived','in_service') "
        "     OR (r.release_due_at IS NOT NULL AND r.release_due_at>?)) "
        "AND (a.state!='held' OR a.hold_expires_at IS NULL OR a.hold_expires_at>?) "
        "AND r.occupied_from<? AND r.occupied_until>? "
        "AND (? IS NULL OR r.appointment_id!=?) ORDER BY r.occupied_from,r.id",
        (clinic_id, resource_type, resource_id, now, now, end, start, exclude_appointment, exclude_appointment)).fetchall()


def check_resource_conflicts(connection, clinic_id: str, resources: list[dict[str, Any]],
                             starts: str, ends: str, now: str,
                             exclude_appointment: str | None = None) -> None:
    """任一资源在时段内被占用即抛出冲突；每项说明冲突来自哪个资源和哪个时间段。"""
    conflicts: list[dict[str, Any]] = []
    for resource in resources:
        until = ends
        if resource["type"] == "room" and resource["turnover_minutes"]:
            until = timestamp(parsed_timestamp(ends) + timedelta(minutes=resource["turnover_minutes"]))
        for row in _overlaps(connection, clinic_id, resource["type"], resource["id"], starts, until, now, exclude_appointment):
            conflicts.append({"resource_type": resource["type"], "resource_id": resource["id"],
                              "resource_name": resource["name"],
                              "occupied_from": row["occupied_from"], "occupied_until": row["occupied_until"],
                              "conflicting_appointment_id": row["appointment_id"],
                              "conflicting_appointment_state": row["appointment_state"]})
    if conflicts:
        raise Conflict("预约资源在该时段已被占用", details={"conflicts": conflicts})


def place_holds(connection, clinic_id: str, appointment_id: str, resources: list[dict[str, Any]],
                starts: str, ends: str, state: str, now: str) -> None:
    for resource in resources:
        until = ends
        if resource["type"] == "room" and resource["turnover_minutes"]:
            until = timestamp(parsed_timestamp(ends) + timedelta(minutes=resource["turnover_minutes"]))
        connection.execute(
            "INSERT INTO appointment_resources(id,appointment_id,clinic_id,resource_type,resource_id,"
            "occupied_from,occupied_until,state,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (new_id("ars"), appointment_id, clinic_id, resource["type"], resource["id"],
             starts, until, state, now))


def verify_resource_versions(connection, clinic_id: str, appointment_id: str,
                             expected: dict[str, Any] | None) -> None:
    """确认预约前比对每个资源的当前版本；任一变化即整次失败。"""
    if not expected:
        return
    if not isinstance(expected, dict):
        raise ValidationError("资源版本必须为对象")
    rows = connection.execute(
        "SELECT resource_type,resource_id FROM appointment_resources WHERE appointment_id=? AND state='held'",
        (appointment_id,)).fetchall()
    for row in rows:
        key = f"{row['resource_type']}:{row['resource_id']}"
        if key not in expected:
            raise Conflict("确认预约缺少资源版本", details={"missing_resource": key})
        table = RESOURCE_TABLES[row["resource_type"]]
        current = connection.execute(f"SELECT version,active FROM {table} WHERE id=? AND clinic_id=?",
                                     (row["resource_id"], clinic_id)).fetchone()
        expected_version = expected[key]
        if current is None or not isinstance(expected_version, int) or current["version"] != expected_version:
            raise Conflict("预约资源版本已变化，确认失败",
                           details={"resource_type": row["resource_type"], "resource_id": row["resource_id"],
                                    "expected_version": expected_version,
                                    "current_version": current["version"] if current else None})
        if not current["active"]:
            raise Conflict("预约资源已停用，确认失败",
                           details={"resource_type": row["resource_type"], "resource_id": row["resource_id"]})


def release_appointment_resources(connection, appointment_id: str, now: str, reason: str) -> int:
    """立即释放预约的全部临时/正式占用（确认失败、取消、占位到期共用）。"""
    changed = connection.execute(
        "UPDATE appointment_resources SET state='released',released_at=?,release_reason=?,release_due_at=NULL,version=version+1 "
        "WHERE appointment_id=? AND state IN ('held','booked')", (now, reason, appointment_id)).rowcount
    connection.execute(
        "UPDATE appointments SET state='cancelled',version=version+1 WHERE id=? AND state='held'",
        (appointment_id,))
    return changed


def apply_terminal_release(connection, appointment_id: str, outcome: str, ends_at: str, now: str) -> list[dict[str, Any]]:
    """按终态规则释放资源，返回每条资源的释放结果供审计记录。"""
    rows = connection.execute(
        "SELECT * FROM appointment_resources WHERE appointment_id=? AND state IN ('held','booked') ORDER BY id",
        (appointment_id,)).fetchall()
    released = []
    for row in rows:
        if outcome in {"cancelled", "hold_expired"}:
            due = None
        elif outcome == "no_show":
            due = ends_at if row["resource_type"] == "room" and ends_at > now else None
            if row["resource_type"] == "room" and row["occupied_until"] != ends_at:
                # 未到诊不追加清洁准备间隔，占用截短到原定结束时刻。
                connection.execute("UPDATE appointment_resources SET occupied_until=? WHERE id=?",
                                   (ends_at, row["id"]))
        else:  # completed：诊室保留到清洁准备间隔结束
            due = row["occupied_until"] if row["resource_type"] == "room" and row["occupied_until"] > now else None
        if due is None:
            connection.execute(
                "UPDATE appointment_resources SET state='released',released_at=?,release_reason=?,release_due_at=NULL,version=version+1 WHERE id=?",
                (now, outcome, row["id"]))
        else:
            connection.execute(
                "UPDATE appointment_resources SET release_due_at=?,release_reason=?,version=version+1 WHERE id=?",
                (due, outcome, row["id"]))
        released.append({"resource_type": row["resource_type"], "resource_id": row["resource_id"],
                         "release_due_at": due, "released_at": None if due else now})
    return released


def sweep_due_releases(connection, clinic_id: str, now: str, limit: int) -> list[dict[str, Any]]:
    """释放已到期的延迟占用（未到诊诊室、完成服务后的清洁间隔）。"""
    rows = connection.execute(
        "SELECT r.* FROM appointment_resources r JOIN appointments a ON a.id=r.appointment_id "
        "WHERE r.clinic_id=? AND r.state IN ('held','booked') AND r.release_due_at IS NOT NULL AND r.release_due_at<=? "
        "AND a.state IN ('no_show','completed') ORDER BY r.release_due_at,r.id LIMIT ?",
        (clinic_id, now, limit)).fetchall()
    for row in rows:
        connection.execute(
            "UPDATE appointment_resources SET state='released',released_at=?,release_due_at=NULL,version=version+1 "
            "WHERE id=? AND state IN ('held','booked')", (now, row["id"]))
    return rows


def local_view(starts_at: str, ends_at: str, zone: ZoneInfo) -> dict[str, Any]:
    """把 UTC 区间转换为诊所本地展示，并标注跨午夜与夏令时切换。"""
    start_local = parsed_timestamp(starts_at).astimezone(zone)
    end_local = parsed_timestamp(ends_at).astimezone(zone)
    crosses_midnight = end_local.date() > start_local.date() or (
        end_local.date() == start_local.date() and end_local <= start_local)
    dst_transition = start_local.utcoffset() != end_local.utcoffset()
    return {"local_date": start_local.date().isoformat(),
            "local_end_date": end_local.date().isoformat(),
            "local_starts_at": start_local.isoformat(timespec="seconds"),
            "local_ends_at": end_local.isoformat(timespec="seconds"),
            "utc_offset_start": start_local.strftime("%z"),
            "utc_offset_end": end_local.strftime("%z"),
            "crosses_local_midnight": crosses_midnight,
            "spans_dst_transition": dst_transition}


class ResourceService:
    """诊室与设备台账、诊所时区日历和资源冲突运营查询。"""

    def __init__(self, database: Database, clock):
        self.db = database
        self.clock = clock

    def now(self) -> str:
        return timestamp(self.clock.now())

    def _clinic(self, connection, clinic_id: str):
        row = connection.execute("SELECT timezone FROM clinics WHERE id=?", (clinic_id,)).fetchone()
        if row is None:
            raise NotFound("诊所不存在")
        return row

    def register_room(self, clinic_id: str, actor_id: str, name: str, kind: str,
                      *, turnover_minutes: int = 0) -> dict[str, Any]:
        name = text(name, "诊室名称", maximum=120)
        kind = choice(kind, "诊室类型", ROOM_KINDS)
        turnover = integer(turnover_minutes, "清洁准备间隔", minimum=0, maximum=480)
        room_id, now = new_id("room"), self.now()
        with self.db.transaction() as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "staff:manage", clinic_id=clinic_id)
            connection.execute("INSERT INTO rooms(id,clinic_id,name,kind,turnover_minutes,created_at) VALUES(?,?,?,?,?,?)",
                               (room_id, clinic_id, name, kind, turnover, now))
            from . import audit
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=None,
                               aggregate_type="room", aggregate_id=room_id, action="room.registered",
                               occurred_at=now, payload={"kind": kind, "turnover_minutes": turnover})
        return {"id": room_id, "clinic_id": clinic_id, "name": name, "kind": kind,
                "turnover_minutes": turnover, "active": True, "version": 1}

    def update_room(self, clinic_id: str, actor_id: str, room_id: str, expected_version: int,
                    *, turnover_minutes: int | None = None, active: bool | None = None) -> dict[str, Any]:
        turnover = integer(turnover_minutes, "清洁准备间隔", minimum=0, maximum=480) if turnover_minutes is not None else None
        if active is not None and not isinstance(active, bool):
            raise ValidationError("启用状态必须为布尔值")
        now = self.now()
        with self.db.transaction() as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "staff:manage", clinic_id=clinic_id)
            row = connection.execute("SELECT * FROM rooms WHERE id=? AND clinic_id=?", (room_id, clinic_id)).fetchone()
            if row is None:
                raise NotFound("诊室不存在")
            if row["version"] != expected_version:
                raise Conflict("诊室已被更新", details={"expected_version": expected_version, "actual_version": row["version"]})
            version = row["version"] + 1
            connection.execute("UPDATE rooms SET turnover_minutes=COALESCE(?,turnover_minutes),"
                               "active=COALESCE(?,active),version=? WHERE id=?",
                               (turnover, None if active is None else int(active), version, room_id))
            from . import audit
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=None,
                               aggregate_type="room", aggregate_id=room_id, action="room.updated",
                               occurred_at=now, payload={"turnover_minutes": turnover, "active": active, "version": version})
        return {"id": room_id, "version": version,
                "turnover_minutes": turnover if turnover is not None else row["turnover_minutes"],
                "active": active if active is not None else bool(row["active"])}

    def register_device(self, clinic_id: str, actor_id: str, name: str, kind: str) -> dict[str, Any]:
        name = text(name, "设备名称", maximum=120)
        kind = text(kind, "设备类型", maximum=80)
        device_id, now = new_id("dev"), self.now()
        with self.db.transaction() as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "staff:manage", clinic_id=clinic_id)
            connection.execute("INSERT INTO devices(id,clinic_id,name,kind,created_at) VALUES(?,?,?,?,?)",
                               (device_id, clinic_id, name, kind, now))
            from . import audit
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=None,
                               aggregate_type="device", aggregate_id=device_id, action="device.registered",
                               occurred_at=now, payload={"kind": kind})
        return {"id": device_id, "clinic_id": clinic_id, "name": name, "kind": kind, "active": True, "version": 1}

    def update_device(self, clinic_id: str, actor_id: str, device_id: str, expected_version: int,
                      *, active: bool | None = None) -> dict[str, Any]:
        if active is not None and not isinstance(active, bool):
            raise ValidationError("启用状态必须为布尔值")
        now = self.now()
        with self.db.transaction() as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "staff:manage", clinic_id=clinic_id)
            row = connection.execute("SELECT * FROM devices WHERE id=? AND clinic_id=?", (device_id, clinic_id)).fetchone()
            if row is None:
                raise NotFound("设备不存在")
            if row["version"] != expected_version:
                raise Conflict("设备已被更新", details={"expected_version": expected_version, "actual_version": row["version"]})
            version = row["version"] + 1
            connection.execute("UPDATE devices SET active=COALESCE(?,active),version=? WHERE id=?",
                               (None if active is None else int(active), version, device_id))
            from . import audit
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=None,
                               aggregate_type="device", aggregate_id=device_id, action="device.updated",
                               occurred_at=now, payload={"active": active, "version": version})
        return {"id": device_id, "version": version, "active": active if active is not None else bool(row["active"])}

    def list_resources(self, clinic_id: str, actor_id: str) -> dict[str, Any]:
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "appointment:write", clinic_id=clinic_id)
            rooms = connection.execute("SELECT * FROM rooms WHERE clinic_id=? ORDER BY name,id", (clinic_id,)).fetchall()
            devices = connection.execute("SELECT * FROM devices WHERE clinic_id=? ORDER BY name,id", (clinic_id,)).fetchall()
            return {"rooms": [{"id": row["id"], "name": row["name"], "kind": row["kind"],
                               "turnover_minutes": row["turnover_minutes"], "active": bool(row["active"]),
                               "version": row["version"]} for row in rooms],
                    "devices": [{"id": row["id"], "name": row["name"], "kind": row["kind"],
                                 "active": bool(row["active"]), "version": row["version"]} for row in devices]}

    def calendar(self, clinic_id: str, actor_id: str, day: str | None) -> dict[str, Any]:
        """按诊所时区的运营日展示预约；跨午夜与夏令时切换均有明确标注。"""
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "appointment:write", clinic_id=clinic_id)
            clinic = self._clinic(connection, clinic_id)
            zone = ZoneInfo(clinic["timezone"])
            local_day = calendar_date(day) if day else self.clock.now().astimezone(zone).date().isoformat()
            day_date = datetime.fromisoformat(local_day).date()
            start = datetime.combine(day_date, datetime.min.time(), zone).astimezone(UTC)
            end = datetime.combine(day_date + timedelta(days=1), datetime.min.time(), zone).astimezone(UTC)
            rows = connection.execute(
                "SELECT * FROM appointments WHERE clinic_id=? AND starts_at<? AND ends_at>? "
                "ORDER BY starts_at,id", (clinic_id, timestamp(end), timestamp(start))).fetchall()
            items = []
            for row in rows:
                resources = connection.execute(
                    "SELECT resource_type,resource_id,occupied_from,occupied_until,state,release_due_at,released_at "
                    "FROM appointment_resources WHERE appointment_id=? ORDER BY resource_type,resource_id",
                    (row["id"],)).fetchall()
                items.append({"id": row["id"], "patient_id": row["patient_id"], "kind": row["kind"],
                              "state": row["state"], "starts_at": row["starts_at"], "ends_at": row["ends_at"],
                              **local_view(row["starts_at"], row["ends_at"], zone),
                              "resources": [dict(resource) for resource in resources]})
            return {"clinic_id": clinic_id, "timezone": clinic["timezone"], "local_date": local_day,
                    "window": {"starts_at": timestamp(start), "ends_at": timestamp(end)},
                    "appointments": items}

    def resource_schedule(self, clinic_id: str, actor_id: str, day: str | None) -> dict[str, Any]:
        """按资源列出占用块并报告冲突来自哪个资源和哪个时间段。"""
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "appointment:write", clinic_id=clinic_id)
            clinic = self._clinic(connection, clinic_id)
            zone = ZoneInfo(clinic["timezone"])
            local_day = calendar_date(day) if day else self.clock.now().astimezone(zone).date().isoformat()
            day_date = datetime.fromisoformat(local_day).date()
            start = datetime.combine(day_date, datetime.min.time(), zone).astimezone(UTC)
            end = datetime.combine(day_date + timedelta(days=1), datetime.min.time(), zone).astimezone(UTC)
            start_text, end_text = timestamp(start), timestamp(end)
            rows = connection.execute(
                "SELECT r.*,a.state AS appointment_state,a.kind,a.patient_id FROM appointment_resources r "
                "JOIN appointments a ON a.id=r.appointment_id WHERE r.clinic_id=? "
                "AND r.occupied_from<? AND r.occupied_until>? ORDER BY r.resource_type,r.resource_id,r.occupied_from,r.id",
                (clinic_id, end_text, start_text)).fetchall()
            names: dict[tuple[str, str], str] = {}
            blocks = []
            for row in rows:
                key = (row["resource_type"], row["resource_id"])
                if key not in names:
                    table = RESOURCE_TABLES[row["resource_type"]]
                    found = connection.execute(f"SELECT * FROM {table} WHERE id=?", (row["resource_id"],)).fetchone()
                    names[key] = (found["display_name"] if row["resource_type"] == "staff" else found["name"]) if found else None
                blocks.append({"resource_type": row["resource_type"], "resource_id": row["resource_id"],
                               "resource_name": names[key], "appointment_id": row["appointment_id"],
                               "appointment_state": row["appointment_state"], "kind": row["kind"],
                               "occupied_from": row["occupied_from"], "occupied_until": row["occupied_until"],
                               "state": row["state"], "release_due_at": row["release_due_at"],
                               "released_at": row["released_at"], "release_reason": row["release_reason"]})
            conflicts = self._schedule_conflicts(connection, clinic_id, start_text, end_text, names)
            return {"clinic_id": clinic_id, "timezone": clinic["timezone"], "local_date": local_day,
                    "window": {"starts_at": start_text, "ends_at": end_text},
                    "blocks": blocks, "conflicts": conflicts}

    def _schedule_conflicts(self, connection, clinic_id: str, start_text: str, end_text: str,
                            names: dict[tuple[str, str], str | None]) -> list[dict[str, Any]]:
        now = self.now()
        rows = connection.execute(
            "SELECT a.resource_type,a.resource_id,a.occupied_from AS a_from,a.occupied_until AS a_until,"
            "a.appointment_id AS a_appointment,b.occupied_from AS b_from,b.occupied_until AS b_until,b.appointment_id AS b_appointment "
            "FROM appointment_resources a JOIN appointment_resources b "
            "ON a.clinic_id=b.clinic_id AND a.resource_type=b.resource_type AND a.resource_id=b.resource_id AND a.id<b.id "
            "JOIN appointments aa ON aa.id=a.appointment_id JOIN appointments ab ON ab.id=b.appointment_id "
            "WHERE a.clinic_id=? AND a.state IN ('held','booked') AND b.state IN ('held','booked') "
            "AND (aa.state IN ('held','booked','arrived','in_service') OR (a.release_due_at IS NOT NULL AND a.release_due_at>?)) "
            "AND (ab.state IN ('held','booked','arrived','in_service') OR (b.release_due_at IS NOT NULL AND b.release_due_at>?)) "
            "AND (aa.state!='held' OR aa.hold_expires_at IS NULL OR aa.hold_expires_at>?) "
            "AND (ab.state!='held' OR ab.hold_expires_at IS NULL OR ab.hold_expires_at>?) "
            "AND a.occupied_from<b.occupied_until AND a.occupied_until>b.occupied_from "
            "AND a.occupied_from<? AND a.occupied_until>? "
            "ORDER BY a.resource_type,a.resource_id,a.occupied_from,a.id",
            (clinic_id, now, now, now, now, end_text, start_text)).fetchall()
        conflicts = []
        for row in rows:
            overlap_from = max(row["a_from"], row["b_from"])
            overlap_until = min(row["a_until"], row["b_until"])
            conflicts.append({"resource_type": row["resource_type"], "resource_id": row["resource_id"],
                              "resource_name": names.get((row["resource_type"], row["resource_id"])),
                              "overlap_from": overlap_from, "overlap_until": overlap_until,
                              "appointment_ids": sorted({row["a_appointment"], row["b_appointment"]})})
        return conflicts

    def release_due_resources(self, clinic_id: str, actor_id: str, *, limit: int = 200) -> dict[str, Any]:
        limit = integer(limit, "处理数量", minimum=1, maximum=1000)
        now = self.now()
        with self.db.transaction() as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "appointment:write", clinic_id=clinic_id)
            rows = sweep_due_releases(connection, clinic_id, now, limit)
            from . import audit
            for row in rows:
                audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id,
                                   patient_id=None, aggregate_type="appointment_resource", aggregate_id=row["id"],
                                   action="appointment.resource_released", occurred_at=now,
                                   payload={"appointment_id": row["appointment_id"], "resource_type": row["resource_type"],
                                            "resource_id": row["resource_id"], "release_reason": row["release_reason"]})
        return {"released": len(rows), "as_of": now}
