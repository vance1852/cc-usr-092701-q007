"""诊室、治疗设备等可预约资源的登记、占用与冲突检测。

时间一律以 UTC 持久化，重叠判断只比较绝对时刻，因此跨诊所当地午夜或
夏令时切换的预约不会因本地墙上时间重复或缺失而误判。日历展示所需的
本地时间由 :func:`local_view` 按诊所时区另行换算。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from . import audit
from .errors import Conflict, NotFound, ValidationError
from .ids import new_id
from .security import authorize, principal_for
from .validation import parsed_timestamp, require_match, text, timestamp


class ResourceService:
    def __init__(self, database, clock):
        self.db = database
        self.clock = clock

    def now(self) -> str:
        return timestamp(self.clock.now())

    def clinic_zone(self, connection, clinic_id: str) -> ZoneInfo:
        row = connection.execute("SELECT timezone FROM clinics WHERE id=?", (clinic_id,)).fetchone()
        if row is None:
            raise NotFound("诊所不存在")
        return ZoneInfo(row["timezone"])

    def register_room(self, clinic_id: str, actor_id: str, name: str, *,
                      turnover_minutes: int = 15) -> dict[str, Any]:
        return self._register(clinic_id, actor_id, "room", name, turnover_minutes, default_minutes=15)

    def register_device(self, clinic_id: str, actor_id: str, name: str, *,
                        turnover_minutes: int = 0) -> dict[str, Any]:
        return self._register(clinic_id, actor_id, "device", name, turnover_minutes, default_minutes=0)

    def _register(self, clinic_id: str, actor_id: str, kind: str, name: str,
                  turnover_minutes: int, *, default_minutes: int) -> dict[str, Any]:
        name = text(name, "资源名称", maximum=120)
        if turnover_minutes is None:
            turnover_minutes = default_minutes
        if not isinstance(turnover_minutes, int) or isinstance(turnover_minutes, bool) \
                or not 0 <= turnover_minutes <= 1440:
            raise ValidationError("清洁准备时间必须为 0 至 1440 分钟的整数")
        if kind == "room" and turnover_minutes < 1:
            raise ValidationError("诊室必须设置清洁准备间隔")
        resource_id = new_id("rm" if kind == "room" else "dev")
        now = self.now()
        with self.db.transaction() as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "appointment:write", clinic_id=clinic_id)
            clinic = connection.execute("SELECT id FROM clinics WHERE id=?", (clinic_id,)).fetchone()
            if clinic is None:
                raise NotFound("诊所不存在")
            duplicate = connection.execute(
                "SELECT id FROM clinic_resources WHERE clinic_id=? AND name=?", (clinic_id, name)).fetchone()
            if duplicate:
                raise Conflict("资源名称在诊所内已存在")
            connection.execute(
                "INSERT INTO clinic_resources(id,clinic_id,kind,name,turnover_minutes,state,created_at) "
                "VALUES(?,?,?,?,?,'active',?)",
                (resource_id, clinic_id, kind, name, turnover_minutes, now))
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=None,
                               aggregate_type="resource", aggregate_id=resource_id,
                               action=f"resource.{kind}_registered", occurred_at=now,
                               payload={"name": name, "turnover_minutes": turnover_minutes})
        return {"id": resource_id, "clinic_id": clinic_id, "kind": kind, "name": name,
                "turnover_minutes": turnover_minutes, "state": "active", "version": 1}

    def list_resources(self, clinic_id: str, actor_id: str, *, kind: str | None = None) -> list[dict[str, Any]]:
        with self.db.transaction(write=False) as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "appointment:read", clinic_id=clinic_id)
            sql = "SELECT * FROM clinic_resources WHERE clinic_id=?"
            params: list[Any] = [clinic_id]
            if kind:
                if kind not in {"room", "device"}:
                    raise ValidationError("资源类型无效")
                sql += " AND kind=?"
                params.append(kind)
            sql += " ORDER BY kind,name,id"
            rows = connection.execute(sql, params).fetchall()
            return [dict(row) for row in rows]

    def disable_resource(self, clinic_id: str, actor_id: str, resource_id: str,
                         expected_version: int, *, reason: str) -> dict[str, Any]:
        """停用资源会推进版本；所有引用旧版本的待确认占位在确认时必然失败。"""
        reason = text(reason, "停用原因", maximum=600)
        now = self.now()
        with self.db.transaction() as connection:
            authorize(principal_for(connection, actor_id, clinic_id), "appointment:write", clinic_id=clinic_id)
            row = connection.execute("SELECT * FROM clinic_resources WHERE id=? AND clinic_id=?",
                                     (resource_id, clinic_id)).fetchone()
            if row is None:
                raise NotFound("资源不存在")
            require_match(row["version"], expected_version, "资源")
            if row["state"] != "active":
                raise Conflict("资源已停用")
            connection.execute("UPDATE clinic_resources SET state='disabled',version=version+1 WHERE id=?", (resource_id,))
            audit.append_event(connection, clinic_id=clinic_id, actor_id=actor_id, patient_id=None,
                               aggregate_type="resource", aggregate_id=resource_id,
                               action="resource.disabled", occurred_at=now,
                               payload={"reason": reason, "previous_version": expected_version})
        return {"id": resource_id, "state": "disabled", "version": expected_version + 1}

    def require_resources(self, connection, clinic_id: str, resource_ids: Iterable[str]):
        ids = list(resource_ids)
        if len(ids) != len(set(ids)):
            raise ValidationError("同一资源不能重复指定")
        if len(ids) > 20:
            raise ValidationError("单次预约最多指定 20 项资源")
        if not ids:
            return {}
        found = {}
        for resource_id in ids:
            if not isinstance(resource_id, str) or not resource_id:
                raise ValidationError("资源编号无效")
            row = connection.execute(
                "SELECT * FROM clinic_resources WHERE id=? AND clinic_id=?", (resource_id, clinic_id)).fetchone()
            if row is None:
                raise NotFound("诊室或设备不存在")
            if row["state"] != "active":
                raise Conflict("诊室或设备已停用，不能预约", details={"resource_id": resource_id})
            found[resource_id] = row
        return found

    @staticmethod
    def occupancy_window(row, starts: str, ends: str) -> tuple[str, str]:
        """资源占用窗口为服务时段加清洁/恢复准备，按各资源自身配置计算。"""
        blocked_from = starts
        blocked_until = timestamp(parsed_timestamp(ends) + timedelta(minutes=row["turnover_minutes"]))
        return blocked_from, blocked_until

    def find_conflicts(self, connection, clinic_id: str, starts: str, ends: str,
                       resources: dict, *, staff_id: str | None = None,
                       now: str | None = None,
                       exclude_appointment_id: str | None = None) -> list[dict[str, Any]]:
        """返回人员与全部资源的冲突明细，调用方据此一次性告知排班员。"""
        now = now or self.now()
        conflicts: list[dict[str, Any]] = []
        if staff_id:
            row = connection.execute(
                "SELECT a.id,a.staff_id,a.starts_at,a.ends_at,a.state,p.display_name AS staff_name "
                "FROM appointments a JOIN staff p ON p.id=a.staff_id "
                "WHERE a.clinic_id=? AND a.staff_id=? "
                "AND a.state IN ('held','booked','arrived','in_service') "
                "AND a.starts_at<? AND a.ends_at>? "
                "AND (a.hold_expires_at IS NULL OR a.hold_expires_at>?) "
                "AND (? IS NULL OR a.id!=?) "
                "ORDER BY a.starts_at,a.id LIMIT 1",
                (clinic_id, staff_id, ends, starts, now,
                 exclude_appointment_id, exclude_appointment_id)).fetchone()
            if row:
                conflicts.append(self._conflict_dict(
                    kind="staff", resource_id=staff_id, name=row["staff_name"],
                    blocked_from=row["starts_at"], blocked_until=row["ends_at"],
                    appointment_id=row["id"], appointment_state=row["state"],
                    requested_from=starts, requested_to=ends))
        for resource_id, resource in resources.items():
            blocked_from, blocked_until = self.occupancy_window(resource, starts, ends)
            # 占用链接在 released_at 之前一直阻塞；过期但未清理的占位除外。
            row = connection.execute(
                "SELECT ar.appointment_id,ar.blocked_from,ar.blocked_until,a.state AS appointment_state,"
                "r.kind AS resource_kind,r.name AS resource_name "
                "FROM appointment_resources ar "
                "JOIN appointments a ON a.id=ar.appointment_id "
                "JOIN clinic_resources r ON r.id=ar.resource_id "
                "WHERE ar.clinic_id=? AND ar.resource_id=? "
                "AND (ar.released_at IS NULL OR ar.released_at>?) "
                "AND ar.blocked_from<? AND ar.blocked_until>? "
                "AND (a.state!='held' OR a.hold_expires_at IS NULL OR a.hold_expires_at>?) "
                "AND (? IS NULL OR ar.appointment_id!=?) "
                "ORDER BY ar.blocked_from,ar.appointment_id LIMIT 1",
                (clinic_id, resource_id, now, blocked_until, blocked_from, now,
                 exclude_appointment_id, exclude_appointment_id)).fetchone()
            if row:
                conflicts.append(self._conflict_dict(
                    kind=row["resource_kind"], resource_id=resource_id, name=row["resource_name"],
                    blocked_from=row["blocked_from"], blocked_until=row["blocked_until"],
                    appointment_id=row["appointment_id"], appointment_state=row["appointment_state"],
                    requested_from=blocked_from, requested_to=blocked_until))
        conflicts.sort(key=lambda item: (item["resource"]["kind"], item["blocked_from"], item["appointment_id"]))
        return conflicts

    def _conflict_dict(self, *, kind: str, resource_id: str, name: str,
                       blocked_from: str, blocked_until: str, appointment_id: str,
                       appointment_state: str, requested_from: str, requested_to: str) -> dict[str, Any]:
        return {
            "resource": {"kind": kind, "id": resource_id, "name": name},
            "appointment_id": appointment_id,
            "appointment_state": appointment_state,
            "blocked_from": blocked_from,
            "blocked_until": blocked_until,
            "requested_from": requested_from,
            "requested_to": requested_to,
        }

    def hold_resources(self, connection, clinic_id: str, appointment_id: str, starts: str, ends: str,
                       resources: dict, now: str) -> list[str]:
        ids = []
        for resource_id, resource in resources.items():
            blocked_from, blocked_until = self.occupancy_window(resource, starts, ends)
            link_id = new_id("arl")
            connection.execute(
                "INSERT INTO appointment_resources(id,appointment_id,clinic_id,resource_id,"
                "blocked_from,blocked_until,resource_version,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (link_id, appointment_id, clinic_id, resource_id, blocked_from, blocked_until,
                 resource["version"], now))
            ids.append(link_id)
        return ids

    def release_resources(self, connection, appointment_id: str, now: str, reason: str) -> int:
        rowcount = connection.execute(
            "UPDATE appointment_resources SET released_at=?,release_reason=?,version=version+1 "
            "WHERE appointment_id=? AND (released_at IS NULL OR released_at>?)",
            (now, reason, appointment_id, now)).rowcount
        return rowcount

    def release_after_service(self, connection, appointment_id: str, now: str, reason: str,
                              *, room_until: str) -> dict[str, int]:
        """未到诊与完成服务的差异化释放：设备立即归还；房间按调用方给定的时点保留。

        - 未到诊：room_until 取预约原定结束时刻（为迟到到达保留缓冲）。
        - 完成服务：room_until 取服务结束加清洁准备的 blocked_until。
        房间行的 released_at 被安排为 max(now, room_until)，到点后无需后台
        任务即自动停止阻塞新预约。
        """
        rows = connection.execute(
            "SELECT ar.id,ar.blocked_until,r.kind FROM appointment_resources ar "
            "JOIN clinic_resources r ON r.id=ar.resource_id "
            "WHERE ar.appointment_id=? AND (ar.released_at IS NULL OR ar.released_at>?)",
            (appointment_id, now)).fetchall()
        immediate = 0
        rooms_held = 0
        for row in rows:
            if row["kind"] == "room":
                released_at = max(now, room_until)
                rooms_held += 1
            else:
                released_at = now
                immediate += 1
            # 占用窗口同步收缩到释放时刻，避免释放之后的时段被误判占用。
            blocked_until = min(row["blocked_until"], released_at)
            connection.execute(
                "UPDATE appointment_resources SET blocked_until=?,released_at=?,release_reason=?,version=version+1 WHERE id=?",
                (blocked_until, released_at, reason, row["id"]))
        return {"immediate": immediate, "rooms_held": rooms_held, "room_until": max(now, room_until)}

    def local_view(self, connection, clinic_id: str, value: str) -> dict[str, str]:
        """日历展示用：同一绝对时刻同时给出 UTC 与诊所本地墙上时间。"""
        zone = self.clinic_zone(connection, clinic_id)
        local = parsed_timestamp(value).astimezone(zone)
        return {"utc": value, "local": local.isoformat(timespec="seconds"),
                "timezone": zone.key, "local_date": local.date().isoformat()}
