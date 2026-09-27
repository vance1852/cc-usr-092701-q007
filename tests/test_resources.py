from __future__ import annotations

import json
import tempfile
import threading
import unittest
from datetime import UTC, datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from careflow.api import create_handler
from careflow.clock import FrozenClock
from careflow.db import Database
from careflow.errors import Conflict
from careflow.service import Careflow


class ResourceCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "clinic.sqlite3")
        self.clock = FrozenClock(datetime(2026, 9, 27, 12, 0, tzinfo=UTC))
        self.app = Careflow(self.db, self.clock)
        initial = self.app.initialize_clinic("澄序门诊", "Asia/Shanghai", "诊所负责人", "LongPassphrase!2026")
        self.clinic = initial["clinic_id"]
        self.owner = initial["owner_id"]
        self.coordinator = self.app.create_staff(self.clinic, "运营协调员", "coordinator", actor_id=self.owner)["id"]
        self.clinician = self.app.create_staff(self.clinic, "临床医生", "clinician", actor_id=self.owner)["id"]
        self.patient = self.app.create_patient(self.clinic, self.coordinator, "res-001", "陈女士")
        self.room = self.app.resources.register_room(self.clinic, self.coordinator, "一号诊室", turnover_minutes=20)
        self.device = self.app.resources.register_device(self.clinic, self.coordinator, "热玛吉设备", turnover_minutes=0)

    def tearDown(self):
        self.temp.cleanup()

    def schedule(self, key, start, end, *, resources=None, staff=None):
        return self.app.create_appointment(
            self.clinic, self.coordinator, self.patient["id"], "晚间光电治疗",
            start, end, key, staff_id=staff, resource_ids=resources)

    def test_room_turnover_blocks_within_cleanup_window_but_adjacent_is_allowed(self):
        first = self.schedule("r-1", "2026-09-29T22:00:00+08:00", "2026-09-29T22:30:00+08:00",
                              resources=[self.room["id"]])
        self.assertEqual(first["state"], "held")
        with self.assertRaises(Conflict) as caught:
            self.schedule("r-2", "2026-09-29T22:35:00+08:00", "2026-09-29T23:00:00+08:00",
                          resources=[self.room["id"]])
        conflict = caught.exception.details["conflicts"][0]
        self.assertEqual(conflict["resource"]["kind"], "room")
        self.assertEqual(conflict["resource"]["id"], self.room["id"])
        # 冲突说明必须指出占用方预约与被阻塞窗口（结束 + 20 分钟清洁）。
        self.assertEqual(conflict["appointment_id"], first["id"])
        self.assertEqual(conflict["blocked_until"], "2026-09-29T14:50:00Z")
        adjacent = self.schedule("r-3", "2026-09-29T22:50:00+08:00", "2026-09-29T23:20:00+08:00",
                                 resources=[self.room["id"]])
        self.assertEqual(adjacent["state"], "held")

    def test_device_without_turnover_allows_adjacent_booking(self):
        self.schedule("d-1", "2026-09-29T20:00:00+08:00", "2026-09-29T20:40:00+08:00",
                      resources=[self.device["id"]])
        adjacent = self.schedule("d-2", "2026-09-29T20:40:00+08:00", "2026-09-29T21:20:00+08:00",
                                 resources=[self.device["id"]])
        self.assertEqual(adjacent["state"], "held")

    def test_staff_calendar_free_does_not_hide_device_conflict(self):
        with self.assertRaises(Conflict) as caught:
            self.schedule("d-a", "2026-09-29T19:00:00+08:00", "2026-09-29T19:30:00+08:00",
                          resources=[self.device["id"]], staff=self.clinician)
            self.schedule("d-b", "2026-09-29T19:10:00+08:00", "2026-09-29T19:40:00+08:00",
                          resources=[self.device["id"]])
        kinds = {item["resource"]["kind"] for item in caught.exception.details["conflicts"]}
        self.assertIn("device", kinds)

    def test_room_must_have_positive_turnover(self):
        from careflow.errors import ValidationError
        with self.assertRaises(ValidationError):
            self.app.resources.register_room(self.clinic, self.coordinator, "二号诊室", turnover_minutes=0)

    def test_confirm_rechecks_versions_and_failed_book_releases_holds(self):
        first = self.schedule("b-1", "2026-09-29T18:00:00+08:00", "2026-09-29T18:30:00+08:00",
                              resources=[self.room["id"], self.device["id"]], staff=self.clinician)
        # 设备在占位期间被停用，版本推进；医生日历本身没有变化。
        self.app.resources.disable_resource(self.clinic, self.owner, self.device["id"], 1, reason="设备送检")
        with self.assertRaises(Conflict) as caught:
            self.app.transition_appointment(self.clinic, self.coordinator, first["id"], 1, "book")
        stale = caught.exception.details["stale_resources"]
        self.assertEqual([item["resource"]["id"] for item in stale], [self.device["id"]])
        # 整次确认失败：预约取消，人/房/设备临时占位全部释放。
        with self.db.transaction(write=False) as connection:
            state = connection.execute("SELECT state FROM appointments WHERE id=?", (first["id"],)).fetchone()[0]
            open_links = connection.execute(
                "SELECT COUNT(*) FROM appointment_resources WHERE appointment_id=? AND released_at IS NULL",
                (first["id"],)).fetchone()[0]
        self.assertEqual(state, "cancelled")
        self.assertEqual(open_links, 0)
        # 同一时段、同一医生现在可以预约另一台设备与房间。
        other_device = self.app.resources.register_device(self.clinic, self.coordinator, "超声设备", turnover_minutes=0)
        replacement = self.schedule("b-2", "2026-09-29T18:00:00+08:00", "2026-09-29T18:30:00+08:00",
                                    resources=[self.room["id"], other_device["id"]], staff=self.clinician)
        self.assertEqual(replacement["state"], "held")

    def test_confirm_fails_when_resource_was_seized_concurrently(self):
        first = self.schedule("c-1", "2026-09-29T18:00:00+08:00", "2026-09-29T18:30:00+08:00",
                              resources=[self.device["id"]])
        # 模拟确认前的竞态：另一笔已确认预约以绝对时间重叠占用同一设备。
        with self.db.transaction() as connection:
            rival = "apt_rival_1"
            connection.execute(
                "INSERT INTO appointments(id,clinic_id,patient_id,kind,starts_at,ends_at,state,idempotency_key,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,'booked',?,?,?)",
                (rival, self.clinic, self.patient["id"], "光电治疗",
                 "2026-09-29T10:10:00Z", "2026-09-29T10:40:00Z", "rival-key", self.coordinator, self.clock.now().astimezone(UTC).isoformat().replace("+00:00", "Z")))
            connection.execute(
                "INSERT INTO appointment_resources(id,appointment_id,clinic_id,resource_id,blocked_from,blocked_until,resource_version,created_at) "
                "VALUES(?,?,?,?,?,?,1,?)",
                ("arl_rival", rival, self.clinic, self.device["id"],
                 "2026-09-29T10:10:00Z", "2026-09-29T10:40:00Z", "2026-09-27T12:00:00Z"))
        with self.assertRaises(Conflict) as caught:
            self.app.transition_appointment(self.clinic, self.coordinator, first["id"], 1, "book")
        self.assertEqual(caught.exception.details["conflicts"][0]["appointment_id"], rival)
        with self.db.transaction(write=False) as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM appointment_resources WHERE appointment_id=? AND released_at IS NULL",
                (first["id"],)).fetchone()[0], 0)

    def test_reschedule_is_atomic_and_conflict_keeps_original_appointment(self):
        first = self.schedule("s-1", "2026-09-29T18:00:00+08:00", "2026-09-29T18:30:00+08:00",
                              resources=[self.room["id"]], staff=self.clinician)
        moved = self.app.reschedule_appointment(
            self.clinic, self.coordinator, first["id"], 1,
            "2026-09-29T20:00:00+08:00", "2026-09-29T20:30:00+08:00", reason="患者要求晚间")
        self.assertEqual(moved["version"], 2)
        # 旧窗口已释放：其他人可占用 18:00 的房间。
        self.schedule("s-2", "2026-09-29T18:00:00+08:00", "2026-09-29T18:30:00+08:00",
                      resources=[self.room["id"]])
        # 新窗口已占用：20:00 再约同一房间（且清洁窗口内）冲突。
        with self.assertRaises(Conflict):
            self.schedule("s-3", "2026-09-29T20:00:00+08:00", "2026-09-29T20:30:00+08:00",
                          resources=[self.room["id"]])
        blocker = self.schedule("s-4", "2026-09-29T21:00:00+08:00", "2026-09-29T21:30:00+08:00",
                                resources=[self.device["id"]])
        with self.assertRaises(Conflict):
            self.app.reschedule_appointment(
                self.clinic, self.coordinator, first["id"], 2,
                "2026-09-29T21:00:00+08:00", "2026-09-29T21:30:00+08:00",
                resource_ids=[self.room["id"], self.device["id"]])
        # 冲突后原预约保持在 20:00、版本不变。
        with self.db.transaction(write=False) as connection:
            row = connection.execute("SELECT starts_at,ends_at,version FROM appointments WHERE id=?",
                                     (first["id"],)).fetchone()
        self.assertEqual(row["starts_at"], "2026-09-29T12:00:00Z")
        self.assertEqual(row["version"], 2)
        self.assertEqual(blocker["id"], blocker["id"])

    def test_cancel_releases_room_immediately(self):
        first = self.schedule("x-1", "2026-09-29T18:00:00+08:00", "2026-09-29T18:30:00+08:00",
                              resources=[self.room["id"]])
        self.app.transition_appointment(self.clinic, self.coordinator, first["id"], 1, "cancel", reason="患者取消")
        again = self.schedule("x-2", "2026-09-29T18:00:00+08:00", "2026-09-29T18:30:00+08:00",
                              resources=[self.room["id"]])
        self.assertEqual(again["state"], "held")

    def _booked(self, key, start="2026-09-29T10:00:00Z", end="2026-09-29T10:30:00Z"):
        apt = self.schedule(key, start, end, resources=[self.room["id"], self.device["id"]])
        self.app.transition_appointment(self.clinic, self.coordinator, apt["id"], 1, "book")
        return apt

    def test_no_show_frees_device_now_but_keeps_room_until_scheduled_end(self):
        apt = self._booked("n-1")
        self.clock.set(datetime(2026, 9, 29, 10, 20, tzinfo=UTC))
        self.app.transition_appointment(self.clinic, self.coordinator, apt["id"], 2, "no_show", reason="联系不上")
        room_blocked = self.app.appointment_conflicts(
            self.clinic, self.coordinator, "2026-09-29T10:20:00Z", "2026-09-29T10:25:00Z",
            resource_ids=[self.room["id"]])
        self.assertEqual(len(room_blocked["conflicts"]), 1)
        self.assertEqual(room_blocked["conflicts"][0]["appointment_state"], "no_show")
        device_free = self.app.appointment_conflicts(
            self.clinic, self.coordinator, "2026-09-29T10:20:00Z", "2026-09-29T10:25:00Z",
            resource_ids=[self.device["id"]])
        self.assertEqual(device_free["conflicts"], [])
        # 到原定结束时刻（10:30Z）房间即让出；未到诊不额外保留清洁间隔。
        self.clock.set(datetime(2026, 9, 29, 10, 31, tzinfo=UTC))
        room_free = self.app.appointment_conflicts(
            self.clinic, self.coordinator, "2026-09-29T10:31:00Z", "2026-09-29T10:40:00Z",
            resource_ids=[self.room["id"]])
        self.assertEqual(room_free["conflicts"], [])

    def test_no_show_releases_room_beyond_original_end_even_before_it_passes(self):
        # 10:20 标记未到诊后，原定结束（10:30Z）之后的时段立即可约，
        # 即便原清洁窗口（至 10:50Z）尚未完全过去。
        apt = self._booked("n-2")
        self.clock.set(datetime(2026, 9, 29, 10, 20, tzinfo=UTC))
        self.app.transition_appointment(self.clinic, self.coordinator, apt["id"], 2, "no_show", reason="爽约")
        self.clock.set(datetime(2026, 9, 29, 10, 25, tzinfo=UTC))
        after_end = self.app.appointment_conflicts(
            self.clinic, self.coordinator, "2026-09-29T10:35:00Z", "2026-09-29T10:50:00Z",
            resource_ids=[self.room["id"]])
        self.assertEqual(after_end["conflicts"], [])
        before_end = self.app.appointment_conflicts(
            self.clinic, self.coordinator, "2026-09-29T10:25:00Z", "2026-09-29T10:29:00Z",
            resource_ids=[self.room["id"]])
        self.assertEqual(len(before_end["conflicts"]), 1)

    def test_completion_keeps_room_until_turnover_window_ends(self):
        apt = self._booked("f-1")
        self.app.transition_appointment(self.clinic, self.coordinator, apt["id"], 2, "arrive")
        self.clock.set(datetime(2026, 9, 29, 10, 5, tzinfo=UTC))
        self.app.transition_appointment(self.clinic, self.coordinator, apt["id"], 3, "start")
        self.clock.set(datetime(2026, 9, 29, 10, 35, tzinfo=UTC))
        self.app.transition_appointment(self.clinic, self.coordinator, apt["id"], 4, "complete")
        room_blocked = self.app.appointment_conflicts(
            self.clinic, self.coordinator, "2026-09-29T10:40:00Z", "2026-09-29T10:45:00Z",
            resource_ids=[self.room["id"]])
        self.assertEqual(room_blocked["conflicts"][0]["blocked_until"], "2026-09-29T10:50:00Z")
        device_free = self.app.appointment_conflicts(
            self.clinic, self.coordinator, "2026-09-29T10:40:00Z", "2026-09-29T10:45:00Z",
            resource_ids=[self.device["id"]])
        self.assertEqual(device_free["conflicts"], [])
        self.clock.set(datetime(2026, 9, 29, 10, 51, tzinfo=UTC))
        room_free = self.app.appointment_conflicts(
            self.clinic, self.coordinator, "2026-09-29T10:40:00Z", "2026-09-29T10:45:00Z",
            resource_ids=[self.room["id"]])
        self.assertEqual(room_free["conflicts"], [])

    def test_conflicts_query_reports_resource_window_in_clinic_local_time(self):
        self._booked("q-1")
        report = self.app.appointment_conflicts(
            self.clinic, self.coordinator, "2026-09-29T10:10:00Z", "2026-09-29T10:20:00Z",
            staff_id=self.clinician, resource_ids=[self.room["id"], self.device["id"]])
        self.assertEqual(report["window"]["timezone"], "Asia/Shanghai")
        self.assertEqual(report["window"]["local_starts_at"], "2026-09-29T18:10:00+08:00")
        kinds = {item["resource"]["kind"] for item in report["conflicts"]}
        self.assertEqual(kinds, {"room", "device"})
        for item in report["conflicts"]:
            self.assertTrue(item["blocked_local_from"].startswith("2026-09-29T18:"))
            self.assertIn("resource", item)

    def test_diagnostics_stays_quiet_after_normal_resource_lifecycle(self):
        apt = self._booked("g-1")
        self.app.transition_appointment(self.clinic, self.coordinator, apt["id"], 2, "arrive")
        self.clock.set(datetime(2026, 9, 29, 10, 5, tzinfo=UTC))
        self.app.transition_appointment(self.clinic, self.coordinator, apt["id"], 3, "start")
        self.clock.set(datetime(2026, 9, 29, 10, 35, tzinfo=UTC))
        self.app.transition_appointment(self.clinic, self.coordinator, apt["id"], 4, "complete")
        cancelled = self.schedule("g-2", "2026-09-29T19:00:00+08:00", "2026-09-29T19:30:00+08:00",
                                  resources=[self.room["id"]])
        self.app.transition_appointment(self.clinic, self.coordinator, cancelled["id"], 1, "cancel", reason="取消")
        report = self.app.run_diagnostics(self.clinic, self.owner)
        resource_codes = {item["code"] for item in report["findings"] if item["code"].startswith("resource.")}
        self.assertEqual(resource_codes, set())

    def test_idempotent_replay_includes_resources_but_changed_set_conflicts(self):
        first = self.schedule("i-1", "2026-09-29T18:00:00+08:00", "2026-09-29T18:30:00+08:00",
                              resources=[self.room["id"]])
        replay = self.schedule("i-1", "2026-09-29T18:00:00+08:00", "2026-09-29T18:30:00+08:00",
                               resources=[self.room["id"]])
        self.assertEqual(replay["id"], first["id"])
        self.assertTrue(replay["replayed"])
        self.assertEqual([item["id"] for item in replay["resources"]], [self.room["id"]])
        with self.assertRaises(Conflict):
            self.schedule("i-1", "2026-09-29T18:00:00+08:00", "2026-09-29T18:30:00+08:00",
                          resources=[self.room["id"], self.device["id"]])

    def test_calendar_spans_local_midnight_in_clinic_timezone(self):
        self.schedule("m-1", "2026-09-29T23:30:00+08:00", "2026-09-30T00:30:00+08:00",
                      resources=[self.room["id"]])
        day29 = self.app.appointment_calendar(self.clinic, self.coordinator, "2026-09-29")
        day30 = self.app.appointment_calendar(self.clinic, self.coordinator, "2026-09-30")
        self.assertEqual([item["id"] for item in day29["appointments"]],
                         [item["id"] for item in day30["appointments"]])
        item = day29["appointments"][0]
        self.assertEqual(item["local_starts_at"], "2026-09-29T23:30:00+08:00")
        self.assertEqual(item["local_ends_at"], "2026-09-30T00:30:00+08:00")


class DstResourceCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "dst.sqlite3")
        self.clock = FrozenClock(datetime(2026, 9, 27, 12, 0, tzinfo=UTC))
        self.app = Careflow(self.db, self.clock)
        clinic = self.app.create_clinic("北美诊所", "America/New_York")
        self.clinic = clinic["id"]
        self.owner = self.app.create_staff(self.clinic, "负责人", "owner")["id"]
        self.coordinator = self.app.create_staff(self.clinic, "协调员", "coordinator", actor_id=self.owner)["id"]
        self.patient = self.app.create_patient(self.clinic, self.coordinator, "dst-001", "Amy")
        self.room_a = self.app.resources.register_room(self.clinic, self.coordinator, "Room A", turnover_minutes=15)
        self.room_b = self.app.resources.register_room(self.clinic, self.coordinator, "Room B", turnover_minutes=15)

    def tearDown(self):
        self.temp.cleanup()

    def test_fall_back_wall_time_overlap_resolved_by_absolute_instants(self):
        # 2026-11-01 北美东部 02:00 回拨到 01:00：同一墙上小时出现两次。
        early = self.app.create_appointment(
            self.clinic, self.coordinator, self.patient["id"], "Laser",
            "2026-11-01T01:45:00-04:00", "2026-11-01T02:00:00-04:00", "dst-early",
            resource_ids=[self.room_a["id"]])
        late = self.app.create_appointment(
            self.clinic, self.coordinator, self.patient["id"], "Laser",
            "2026-11-01T01:45:00-05:00", "2026-11-01T02:00:00-05:00", "dst-late",
            resource_ids=[self.room_a["id"]])
        self.assertNotEqual(early["id"], late["id"])
        calendar = self.app.appointment_calendar(self.clinic, self.coordinator, "2026-11-01")
        self.assertEqual(calendar["window"]["starts_at"], "2026-11-01T04:00:00Z")
        self.assertEqual(calendar["window"]["ends_at"], "2026-11-02T05:00:00Z")
        items = {item["id"]: item for item in calendar["appointments"]}
        self.assertEqual(items[early["id"]]["local_starts_at"], "2026-11-01T01:45:00-04:00")
        self.assertEqual(items[late["id"]]["local_starts_at"], "2026-11-01T01:45:00-05:00")
        # 绝对时刻重叠时，即便墙上时间写法不同也必须冲突。
        with self.assertRaises(Conflict):
            self.app.create_appointment(
                self.clinic, self.coordinator, self.patient["id"], "Laser",
                "2026-11-01T01:50:00-04:00", "2026-11-01T01:55:00-04:00", "dst-clash",
                resource_ids=[self.room_a["id"]])
        # 不同房间、绝对时间相邻（清洁窗口外）允许。
        neighbor = self.app.create_appointment(
            self.clinic, self.coordinator, self.patient["id"], "Laser",
            "2026-11-01T02:15:00-05:00", "2026-11-01T02:45:00-05:00", "dst-neighbor",
            resource_ids=[self.room_b["id"]])
        self.assertEqual(neighbor["state"], "held")


class ResourceHttpCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "http.sqlite3")
        self.clock = FrozenClock(datetime(2026, 9, 27, 12, 0, tzinfo=UTC))
        self.app = Careflow(self.db, self.clock)
        initial = self.app.initialize_clinic("澄序门诊", "Asia/Shanghai", "诊所负责人", "LongPassphrase!2026")
        self.clinic = initial["clinic_id"]
        self.owner = initial["owner_id"]
        login = self.app.login(self.clinic, self.owner, "LongPassphrase!2026")
        self.token = login["access_token"]
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), create_handler(self.app))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def test_resource_and_calendar_routes(self):
        request = Request(self.base + "/resources/devices",
                          data=json.dumps({"name": "冰点脱毛仪", "turnover_minutes": 0}).encode(),
                          method="POST", headers={"X-Clinic-ID": self.clinic,
                                                  "Authorization": f"Bearer {self.token}",
                                                  "Content-Type": "application/json"})
        with urlopen(request, timeout=3) as response:
            device = json.loads(response.read())
            self.assertEqual(response.status, 201)
        request = Request(self.base + "/appointments/calendar?date=2026-09-29",
                          headers={"X-Clinic-ID": self.clinic, "Authorization": f"Bearer {self.token}"})
        with urlopen(request, timeout=3) as response:
            calendar = json.loads(response.read())
        self.assertEqual(calendar["timezone"], "Asia/Shanghai")
        self.assertEqual(calendar["appointments"], [])
        request = Request(self.base + f"/appointments/conflicts?starts_at=2026-09-29T10:00:00Z&ends_at=2026-09-29T10:30:00Z&resource_ids={device['id']}",
                          headers={"X-Clinic-ID": self.clinic, "Authorization": f"Bearer {self.token}"})
        with urlopen(request, timeout=3) as response:
            report = json.loads(response.read())
        self.assertEqual(report["conflicts"], [])


if __name__ == "__main__":
    unittest.main()
