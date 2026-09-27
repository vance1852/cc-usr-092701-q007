from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
from datetime import UTC, datetime
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from careflow.api import create_handler
from careflow.clock import FrozenClock
from careflow.db import Database
from careflow.errors import Conflict, Forbidden, NotFound, Unauthorized, ValidationError
from careflow.service import Careflow


class CareflowCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "clinic.sqlite3")
        self.clock = FrozenClock(datetime(2026, 9, 27, 12, 0, tzinfo=UTC))
        self.app = Careflow(self.db, self.clock)
        initial = self.app.initialize_clinic("澄序门诊", "Asia/Shanghai", "诊所负责人", "LongPassphrase!2026")
        self.clinic = initial["clinic_id"]
        self.owner = initial["owner_id"]
        self.clinician = self.app.create_staff(self.clinic, "临床医生", "clinician", actor_id=self.owner)["id"]
        self.nurse = self.app.create_staff(self.clinic, "护理人员", "nurse", actor_id=self.owner)["id"]
        self.coordinator = self.app.create_staff(self.clinic, "运营协调员", "coordinator", actor_id=self.owner)["id"]
        self.patient = self.app.create_patient(self.clinic, self.coordinator, "case-017", "林女士")

    def tearDown(self):
        self.temp.cleanup()

    def consent(self, purpose="weight_program", revision=1, expires_at=None):
        digest = hashlib.sha256(f"{purpose}-r{revision}".encode()).hexdigest()
        return self.app.grant_consent(self.clinic, self.clinician, self.patient["id"], purpose,
                                      revision, digest, expires_at=expires_at)

    def plan(self, kind="weight"):
        consent = self.consent("weight_program" if kind == "weight" else "aesthetic_procedure")
        return self.app.create_plan(
            self.clinic, self.clinician, self.patient["id"], kind, self.clinician,
            {"description": "按门诊约定复核", "review_interval_days": 30},
            {"screening": "reviewed", "contraindications": [], "review_required": False},
            "2026-09-27", target_date="2026-12-27", consent_id=consent["id"])

    def appointment(self, key="visit-1"):
        return self.app.create_appointment(
            self.clinic, self.coordinator, self.patient["id"], "复诊",
            "2026-09-29T10:00:00+08:00", "2026-09-29T10:30:00+08:00", key, staff_id=self.clinician)

    def test_initialization_is_atomic_and_password_change_revokes_sessions(self):
        self.assertEqual(self.app.login(self.clinic, self.owner, "LongPassphrase!2026")["role"], "owner")
        token = self.app.login(self.clinic, self.owner, "LongPassphrase!2026")["access_token"]
        with self.assertRaises(Conflict):
            self.app.initialize_clinic("另一诊所", "UTC", "第二负责人", "AnotherPassphrase!2026")
        self.app.set_password(self.clinic, self.owner, self.owner, "NewPassphrase!2026")
        with self.assertRaises(Unauthorized):
            self.app.staff_for_token(self.clinic, token)
        self.assertTrue(self.app.login(self.clinic, self.owner, "NewPassphrase!2026")["access_token"])

    def test_clinic_boundary_and_role_permissions_hide_cross_clinic_records(self):
        other = self.app.create_clinic("另一诊所", "UTC")
        outsider = self.app.create_staff(other["id"], "负责人", "owner")
        with self.assertRaises(Unauthorized):
            self.app.get_patient(self.clinic, outsider["id"], self.patient["id"])
        with self.assertRaises(Forbidden):
            self.app.grant_consent(self.clinic, self.coordinator, self.patient["id"], "weight_program", 1, "a" * 64)
        self.assertNotIn("phone_ciphertext", self.app.get_patient(self.clinic, self.coordinator, self.patient["id"]))

    def test_withdrawal_preserves_consent_history_and_pauses_dependent_plan(self):
        plan = self.plan()
        self.app.transition_plan(self.clinic, self.clinician, plan["id"], 1, "propose")
        self.app.transition_plan(self.clinic, self.clinician, plan["id"], 2, "activate")
        consent = self.app.consent_history(self.clinic, self.clinician, self.patient["id"])[0]
        result = self.app.withdraw_consent(self.clinic, self.clinician, consent["id"], "患者提出撤回")
        self.assertEqual(result["state"], "withdrawn")
        self.assertEqual(self.app.plan_history(self.clinic, self.clinician, plan["id"])[-1]["snapshot"]["state"], "paused")
        self.assertEqual(len(self.app.consent_history(self.clinic, self.clinician, self.patient["id"])), 1)
        self.assertTrue(self.app.withdraw_consent(self.clinic, self.clinician, consent["id"], "重复请求")["replayed"])

    def test_plan_requires_signed_assessment_and_versioned_consent(self):
        consent = self.consent()
        assessment = self.app.create_assessment(self.clinic, self.clinician, self.patient["id"], "weight",
                                                {"weight_kg": "72.5", "waist_cm": 83}, {"sleep": "一般"})
        with self.assertRaises(Conflict):
            self.app.create_plan(self.clinic, self.clinician, self.patient["id"], "weight", self.clinician,
                                 {"description": "目标"}, {"review_required": True}, "2026-09-27",
                                 consent_id=consent["id"], assessment_id=assessment["id"])
        self.app.sign_assessment(self.clinic, self.clinician, assessment["id"], expected_version=1)
        plan = self.app.create_plan(self.clinic, self.clinician, self.patient["id"], "weight", self.clinician,
                                    {"description": "目标"}, {"review_required": True}, "2026-09-27",
                                    consent_id=consent["id"], assessment_id=assessment["id"])
        self.assertEqual(plan["state"], "draft")
        with self.assertRaises(Conflict):
            self.app.transition_plan(self.clinic, self.clinician, plan["id"], 1, "activate")

    def test_expired_consent_is_not_used_for_new_plan(self):
        consent = self.consent(expires_at="2026-09-27T12:01:00Z")
        self.clock.set(datetime(2026, 9, 27, 12, 2, tzinfo=UTC))
        with self.assertRaises(Conflict):
            self.app.create_plan(self.clinic, self.clinician, self.patient["id"], "weight", self.clinician,
                                 {"description": "目标"}, {}, "2026-09-27", consent_id=consent["id"])

    def test_appointment_hold_is_idempotent_and_expires_at_boundary(self):
        first = self.appointment()
        replay = self.appointment()
        self.assertEqual(first["id"], replay["id"])
        self.assertTrue(replay["replayed"])
        with self.assertRaises(Conflict):
            self.app.create_appointment(self.clinic, self.coordinator, self.patient["id"], "复诊",
                                        "2026-09-29T10:00:00+08:00", "2026-09-29T10:30:00+08:00", "visit-1",
                                        staff_id=self.clinician, plan_id="different")
        self.clock.set(datetime(2026, 9, 27, 12, 10, tzinfo=UTC))
        self.assertEqual(self.app.expire_holds(self.clinic)["expired"], 1)
        with self.assertRaises(Conflict):
            self.app.transition_appointment(self.clinic, self.coordinator, first["id"], 2, "book")

    def test_staff_overlap_is_rejected_but_adjacent_time_is_allowed(self):
        self.appointment("morning")
        with self.assertRaises(Conflict):
            self.app.create_appointment(self.clinic, self.coordinator, self.patient["id"], "复诊",
                                        "2026-09-29T10:15:00+08:00", "2026-09-29T10:45:00+08:00", "overlap",
                                        staff_id=self.clinician)
        adjacent = self.app.create_appointment(self.clinic, self.coordinator, self.patient["id"], "复诊",
                                              "2026-09-29T10:30:00+08:00", "2026-09-29T11:00:00+08:00", "adjacent",
                                              staff_id=self.clinician)
        self.assertEqual(adjacent["state"], "held")

    def test_observation_correction_is_append_only_and_report_uses_effective_value(self):
        original = self.app.record_observation(self.clinic, self.clinician, self.patient["id"], "weight_kg", 73.2,
                                              "2026-09-27T08:00:00+08:00")
        correction = self.app.record_observation(self.clinic, self.clinician, self.patient["id"], "weight_kg", 72.8,
                                                 "2026-09-27T08:00:00+08:00", correction_of=original["id"])
        series = self.app.reports.weight_series(self.clinic, self.clinician, self.patient["id"])
        self.assertEqual([row["weight_kg"] for row in series["observations"]], [72.8])
        self.assertEqual(correction["correction_of"], original["id"])
        with self.assertRaises(Conflict):
            self.app.record_observation(self.clinic, self.clinician, self.patient["id"], "weight_kg", 72.1,
                                         "2026-09-27T08:00:00+08:00", correction_of=original["id"])

    def test_followup_lease_fencing_prevents_late_completion(self):
        followup = self.app.schedule_followup(self.clinic, self.clinician, self.patient["id"],
                                              "2026-09-27T11:00:00Z", "复诊反馈", "fup-1")
        first = self.app.claim_followups(self.clinic, self.nurse, lease_minutes=1)[0]
        self.clock.set(datetime(2026, 9, 27, 12, 2, tzinfo=UTC))
        second = self.app.claim_followups(self.clinic, self.coordinator, lease_minutes=3)[0]
        with self.assertRaises(Conflict):
            self.app.complete_followup(self.clinic, self.nurse, followup["id"], first["claim_token"], "迟到回写", first["version"])
        done = self.app.complete_followup(self.clinic, self.coordinator, followup["id"], second["claim_token"], "已联系", second["version"])
        self.assertEqual(done["state"], "done")

    def test_incident_history_is_versioned_and_replay_does_not_duplicate(self):
        incident = self.app.report_incident(self.clinic, self.nurse, self.patient["id"], "术后不适", "moderate",
                                            "2026-09-27T10:00:00+08:00", "患者报告局部红肿", "incident-1")
        replay = self.app.report_incident(self.clinic, self.nurse, self.patient["id"], "术后不适", "moderate",
                                          "2026-09-27T10:00:00+08:00", "患者报告局部红肿", "incident-1")
        self.assertEqual(replay["id"], incident["id"])
        self.app.transition_incident(self.clinic, self.clinician, incident["id"], "triage", "安排临床评估", 1)
        history = self.app.incident_history(self.clinic, self.clinician, incident["id"])
        self.assertEqual([item["type"] for item in history["events"]], ["reported", "triage"])

    def test_stop_flag_requires_clinician_review_and_diagnostic_reports_it(self):
        flag = self.app.clinical_flags.report(self.clinic, self.nurse, self.patient["id"], "prior_reaction", "stop", "既往材料待核实")
        report = self.app.run_diagnostics(self.clinic, self.owner)
        self.assertIn("clinical_flag.requires_review", {item["code"] for item in report["findings"]})
        with self.assertRaises(Forbidden):
            self.app.clinical_flags.review(self.clinic, self.nurse, flag["id"], 1, "confirm", "已核实")
        self.app.clinical_flags.review(self.clinic, self.clinician, flag["id"], 1, "confirm", "已复核原始材料")
        flags = self.app.clinical_flags.list_for_patient(self.clinic, self.clinician, self.patient["id"])
        self.assertEqual(flags[0]["state"], "confirmed")

    def test_encounter_requires_sections_and_amendment_preserves_signed_note(self):
        appointment = self.appointment()
        self.app.transition_appointment(self.clinic, self.coordinator, appointment["id"], 1, "book")
        self.app.transition_appointment(self.clinic, self.coordinator, appointment["id"], 2, "arrive")
        self.clock.set(datetime(2026, 9, 29, 2, 0, tzinfo=UTC))
        self.app.transition_appointment(self.clinic, self.coordinator, appointment["id"], 3, "start")
        encounter = self.app.encounter_for_appointment(self.clinic, self.clinician, appointment["id"])
        with self.assertRaises(Conflict):
            self.app.sign_encounter(self.clinic, self.clinician, encounter["id"], 1)
        for section in ("chief_complaint", "assessment", "plan"):
            self.app.add_encounter_note(self.clinic, self.clinician, encounter["id"], section, f"记录-{section}",
                                        expected_version=encounter["version"])
            encounter = self.app.encounter_for_appointment(self.clinic, self.clinician, appointment["id"])
        self.app.sign_encounter(self.clinic, self.clinician, encounter["id"], encounter["version"])
        signed = self.app.encounter_notes(self.clinic, self.clinician, encounter["id"])
        first = next(item for item in signed["notes"] if item["section"] == "assessment")
        amended = self.app.add_encounter_note(self.clinic, self.clinician, encounter["id"], "assessment", "补充记录",
                                              expected_version=signed["version"], amendment_reason="补充化验时间")
        self.assertEqual(amended["state"], "amended")
        history = self.app.encounter_notes(self.clinic, self.clinician, encounter["id"])["notes"]
        self.assertTrue(any(item["id"] == first["id"] for item in history))

    def test_stock_uses_fefo_and_quarantine_blocks_consumption(self):
        product = self.app.supplies.register_product(self.clinic, self.owner, "无菌敷料", "consumable", "片")
        later = self.app.supplies.receive_lot(self.clinic, self.owner, product["id"], "s-1", "batch-later", 8,
                                              "receive-1", expires_on="2027-06-01")
        earlier = self.app.supplies.receive_lot(self.clinic, self.owner, product["id"], "s-1", "batch-earlier", 5,
                                                "receive-2", expires_on="2027-01-01")
        appointment = self.appointment()
        reserved = self.app.supplies.reserve(self.clinic, self.coordinator, appointment["id"], product["id"], 7, "stock-reserve-1")
        self.assertEqual([row["lot_id"] for row in reserved["reservations"]], [earlier["id"], later["id"]])
        self.app.supplies.change_lot_state(self.clinic, self.owner, later["id"], "recall", "批次通知召回")
        with self.assertRaises(Conflict):
            self.app.supplies.consume_reservation(self.clinic, self.clinician, reserved["reservations"][1]["id"], expected_version=1)

    def test_stock_reservation_is_all_or_nothing_and_same_request_replays(self):
        product = self.app.supplies.register_product(self.clinic, self.owner, "一次性导管", "consumable", "支")
        self.app.supplies.receive_lot(self.clinic, self.owner, product["id"], "s-2", "lot-a", 2, "receive-a")
        appointment = self.appointment()
        with self.assertRaises(Conflict):
            self.app.supplies.reserve(self.clinic, self.coordinator, appointment["id"], product["id"], 3, "reserve-too-many")
        balance = self.app.supplies.lot_balances(self.clinic, product["id"])[0]
        self.assertEqual(balance["available_quantity"], 2)
        first = self.app.supplies.reserve(self.clinic, self.coordinator, appointment["id"], product["id"], 1, "reserve-one")
        replay = self.app.supplies.reserve(self.clinic, self.coordinator, appointment["id"], product["id"], 1, "reserve-one")
        self.assertEqual(first["reservations"], replay["reservations"])
        self.assertTrue(replay["replayed"])

    def test_milestone_defer_history_and_idempotent_creation(self):
        plan = self.plan()
        first = self.app.milestones.create(self.clinic, self.clinician, plan["id"], "review", "复核体重记录",
                                           "2026-10-10T09:00:00+08:00", "mile-1", assigned_to=self.nurse)
        again = self.app.milestones.create(self.clinic, self.clinician, plan["id"], "review", "复核体重记录",
                                           "2026-10-10T09:00:00+08:00", "mile-1", assigned_to=self.nurse)
        self.assertEqual(first["id"], again["id"])
        deferred = self.app.milestones.transition(self.clinic, self.nurse, first["id"], 1, "defer",
                                                 reason="患者改期", new_due_at="2026-10-12T09:00:00+08:00")
        self.assertEqual(deferred["state"], "pending")
        self.assertEqual(len(self.app.milestones.history(self.clinic, self.clinician, first["id"])), 2)

    def test_export_needs_consent_is_minimized_and_idempotent(self):
        with self.assertRaises(Conflict):
            self.app.exports.export(self.clinic, self.owner, self.patient["id"], ["profile"], "患者本人申请", "export-1")
        self.consent("data_export")
        first = self.app.exports.export(self.clinic, self.owner, self.patient["id"], ["profile", "observations"], "患者本人申请", "export-1")
        replay = self.app.exports.export(self.clinic, self.owner, self.patient["id"], ["observations", "profile"], "患者本人申请", "export-1")
        self.assertEqual(first["sha256"], replay["sha256"])
        self.assertTrue(replay["replayed"])
        self.assertNotIn("phone_ciphertext", json.dumps(first, ensure_ascii=False))

    def test_daily_report_uses_clinic_calendar_day_and_dst_aware_bounds(self):
        clinic = self.app.create_clinic("北美诊所", "America/New_York")
        owner = self.app.create_staff(clinic["id"], "负责人", "owner")
        self.assertEqual(self.app.reports.daily_operations(clinic["id"], owner["id"], "2026-11-01")["window"]["ends_at"],
                         "2026-11-02T05:00:00Z")

    def test_audit_hash_chain_detects_tampering(self):
        self.app.audit_history(self.clinic, self.owner)
        self.assertTrue(self.app.verify_audit(self.clinic, self.owner)["ok"])
        with self.db.transaction() as connection:
            connection.execute("UPDATE audit_events SET action='tampered' WHERE sequence=1")
        self.assertFalse(self.app.verify_audit(self.clinic, self.owner)["ok"])

    def room(self, name="治疗室一", turnover=15):
        return self.app.resources.register_room(self.clinic, self.owner, name, "treatment", turnover_minutes=turnover)

    def device(self, name="激光仪一"):
        return self.app.resources.register_device(self.clinic, self.owner, name, "laser")

    _KEEP_STAFF = object()

    def held(self, key, start, end, *, room_id=None, device_ids=None, staff=_KEEP_STAFF):
        return self.app.create_appointment(
            self.clinic, self.coordinator, self.patient["id"], "晚间嫩肤", start, end, key,
            staff_id=self.clinician if staff is self._KEEP_STAFF else staff,
            room_id=room_id, device_ids=device_ids)

    def test_room_device_and_staff_conflicts_identify_resource_and_interval(self):
        room = self.room(turnover=15)
        device = self.device()
        first = self.held("res-1", "2026-09-29T10:00:00+08:00", "2026-09-29T10:30:00+08:00",
                          room_id=room["id"], device_ids=[device["id"]])
        self.assertEqual(first["state"], "held")
        self.assertEqual({r["resource_type"] for r in first["resources"]}, {"staff", "room", "device"})
        with self.assertRaises(Conflict) as room_clash:
            self.held("res-2", "2026-09-29T10:15:00+08:00", "2026-09-29T10:45:00+08:00", room_id=room["id"], staff=None)
        conflict = room_clash.exception.details["conflicts"][0]
        self.assertEqual(conflict["resource_type"], "room")
        self.assertEqual(conflict["resource_id"], room["id"])
        self.assertEqual(conflict["occupied_from"], "2026-09-29T02:00:00Z")
        # 诊室占用包含 15 分钟清洁准备间隔。
        self.assertEqual(conflict["occupied_until"], "2026-09-29T02:45:00Z")
        with self.assertRaises(Conflict) as turnover_clash:
            self.held("res-3", "2026-09-29T10:30:00+08:00", "2026-09-29T11:00:00+08:00", room_id=room["id"], staff=None)
        self.assertEqual(turnover_clash.exception.details["conflicts"][0]["resource_type"], "room")
        after_turnover = self.held("res-4", "2026-09-29T10:45:00+08:00", "2026-09-29T11:15:00+08:00",
                                   room_id=room["id"], staff=None)
        self.assertEqual(after_turnover["state"], "held")
        with self.assertRaises(Conflict) as device_clash:
            self.held("res-5", "2026-09-29T10:15:00+08:00", "2026-09-29T10:45:00+08:00",
                      device_ids=[device["id"]], staff=None)
        self.assertEqual(device_clash.exception.details["conflicts"][0]["resource_type"], "device")
        with self.assertRaises(Conflict) as staff_clash:
            self.held("res-6", "2026-09-29T10:15:00+08:00", "2026-09-29T10:45:00+08:00")
        self.assertEqual(staff_clash.exception.details["conflicts"][0]["resource_type"], "staff")
        no_resources = self.held("res-7", "2026-09-29T10:15:00+08:00", "2026-09-29T10:45:00+08:00", staff=None)
        self.assertEqual(no_resources["resources"], [])

    def test_book_fails_and_releases_hold_when_resource_version_changes(self):
        room = self.room()
        device = self.device()
        appointment = self.held("ver-1", "2026-09-29T10:00:00+08:00", "2026-09-29T10:30:00+08:00",
                                room_id=room["id"], device_ids=[device["id"]])
        versions = appointment["resource_versions"]
        self.app.resources.update_room(self.clinic, self.owner, room["id"], 1, turnover_minutes=30)
        with self.assertRaises(Conflict) as stale:
            self.app.transition_appointment(self.clinic, self.coordinator, appointment["id"], 1, "book",
                                            expected_resource_versions=versions)
        self.assertIn("resource_type", stale.exception.details)
        cancelled = self.app.get_appointment(self.clinic, self.coordinator, appointment["id"])
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertTrue(all(r["state"] == "released" for r in cancelled["resources"]))
        # 临时占位已释放，同时段可立即再约。
        followup = self.held("ver-2", "2026-09-29T10:00:00+08:00", "2026-09-29T10:30:00+08:00",
                             room_id=room["id"], device_ids=[device["id"]])
        self.assertEqual(followup["state"], "held")
        actions = {event["action"] for event in self.app.audit_history(self.clinic, self.owner)}
        self.assertIn("appointment.book_failed", actions)

    def test_book_succeeds_with_matching_versions_and_blocks_others(self):
        room = self.room()
        appointment = self.held("ok-1", "2026-09-29T10:00:00+08:00", "2026-09-29T10:30:00+08:00", room_id=room["id"])
        booked = self.app.transition_appointment(self.clinic, self.coordinator, appointment["id"], 1, "book",
                                                 expected_resource_versions=appointment["resource_versions"])
        self.assertEqual(booked["state"], "booked")
        with self.assertRaises(Conflict):
            self.held("ok-2", "2026-09-29T10:00:00+08:00", "2026-09-29T10:30:00+08:00", room_id=room["id"], staff=None)
        with self.assertRaises(Conflict):
            self.app.transition_appointment(self.clinic, self.coordinator, appointment["id"], 2, "book",
                                            expected_resource_versions={"room:" + room["id"]: 1})

    def test_book_conflict_with_other_booking_releases_hold(self):
        room = self.room(turnover=0)
        first = self.held("race-1", "2026-09-29T10:00:00+08:00", "2026-09-29T10:30:00+08:00",
                          room_id=room["id"], staff=None)
        second = self.held("race-2", "2026-09-29T12:00:00+08:00", "2026-09-29T12:30:00+08:00",
                           room_id=room["id"], staff=None)
        self.app.transition_appointment(self.clinic, self.coordinator, second["id"], 1, "book",
                                        expected_resource_versions=second["resource_versions"])
        # 另一笔已确认事务在同一资源上把占用改写进 first 的时段（模拟提交竞态）。
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE appointment_resources SET occupied_from='2026-09-29T02:15:00Z',occupied_until='2026-09-29T02:45:00Z' "
                "WHERE appointment_id=? AND resource_type='room'", (second["id"],))
        with self.assertRaises(Conflict) as clash:
            self.app.transition_appointment(self.clinic, self.coordinator, first["id"], 1, "book",
                                            expected_resource_versions=first["resource_versions"])
        self.assertEqual(clash.exception.details["conflicts"][0]["conflicting_appointment_id"], second["id"])
        cancelled = self.app.get_appointment(self.clinic, self.coordinator, first["id"])
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertEqual(cancelled["resources"][0]["state"], "released")

    def test_reschedule_is_atomic_and_keeps_original_on_conflict(self):
        room_a = self.room("治疗室A", turnover=0)
        room_b = self.room("治疗室B", turnover=0)
        device_a, device_b = self.device("激光仪A"), self.device("激光仪B")
        first = self.held("mv-1", "2026-09-29T10:00:00+08:00", "2026-09-29T10:30:00+08:00",
                          room_id=room_a["id"], device_ids=[device_a["id"]])
        self.app.transition_appointment(self.clinic, self.coordinator, first["id"], 1, "book",
                                        expected_resource_versions=first["resource_versions"])
        second = self.held("mv-2", "2026-09-29T11:00:00+08:00", "2026-09-29T11:30:00+08:00",
                           room_id=room_b["id"], device_ids=[device_b["id"]], staff=None)
        self.app.transition_appointment(self.clinic, self.coordinator, second["id"], 1, "book",
                                        expected_resource_versions=second["resource_versions"])
        with self.assertRaises(Conflict):
            self.app.reschedule_appointment(self.clinic, self.coordinator, first["id"], 2,
                                            "2026-09-29T11:00:00+08:00", "2026-09-29T11:30:00+08:00",
                                            room_id=room_b["id"], reason="患者要求")
        kept = self.app.get_appointment(self.clinic, self.coordinator, first["id"])
        self.assertEqual((kept["state"], kept["starts_at"], kept["version"]), ("booked", "2026-09-29T02:00:00Z", 2))
        moved = self.app.reschedule_appointment(self.clinic, self.coordinator, first["id"], 2,
                                                "2026-09-29T12:00:00+08:00", "2026-09-29T12:30:00+08:00",
                                                room_id=room_b["id"], device_ids=[device_b["id"]], reason="患者要求")
        self.assertEqual((moved["state"], moved["version"]), ("booked", 3))
        active_rooms = {r["resource_id"] for r in moved["resources"]
                        if r["resource_type"] == "room" and r["state"] in ("held", "booked")}
        self.assertEqual(active_rooms, {room_b["id"]})
        # 旧时段资源已释放，新时段资源被占用。
        old_slot = self.held("mv-3", "2026-09-29T10:00:00+08:00", "2026-09-29T10:30:00+08:00",
                             room_id=room_a["id"], device_ids=[device_a["id"]], staff=None)
        self.assertEqual(old_slot["state"], "held")
        with self.assertRaises(Conflict):
            self.held("mv-4", "2026-09-29T12:00:00+08:00", "2026-09-29T12:30:00+08:00",
                      room_id=room_b["id"], staff=None)

    def test_terminal_states_follow_distinct_release_rules(self):
        room = self.room(turnover=15)
        device = self.device()
        # 取消：全部资源立即释放。
        cancelled = self.held("term-1", "2026-09-29T10:00:00+08:00", "2026-09-29T10:30:00+08:00",
                              room_id=room["id"], device_ids=[device["id"]])
        self.app.transition_appointment(self.clinic, self.coordinator, cancelled["id"], 1, "book",
                                        expected_resource_versions=cancelled["resource_versions"])
        self.app.transition_appointment(self.clinic, self.coordinator, cancelled["id"], 2, "cancel", reason="患者取消")
        reuse = self.held("term-2", "2026-09-29T10:00:00+08:00", "2026-09-29T10:30:00+08:00",
                          room_id=room["id"], device_ids=[device["id"]])
        self.assertEqual(reuse["state"], "held")
        self.app.transition_appointment(self.clinic, self.coordinator, reuse["id"], 1, "cancel", reason="清理")
        # 未到诊：人员与设备立即释放，诊室保留到原定结束（不加清洁间隔）。
        no_show = self.held("term-3", "2026-09-29T11:00:00+08:00", "2026-09-29T11:30:00+08:00",
                            room_id=room["id"], device_ids=[device["id"]])
        self.app.transition_appointment(self.clinic, self.coordinator, no_show["id"], 1, "book",
                                        expected_resource_versions=no_show["resource_versions"])
        self.app.transition_appointment(self.clinic, self.coordinator, no_show["id"], 2, "no_show", reason="患者未到")
        staff_device = self.held("term-4", "2026-09-29T11:00:00+08:00", "2026-09-29T11:30:00+08:00",
                                 device_ids=[device["id"]])
        self.assertEqual(staff_device["state"], "held")
        self.app.transition_appointment(self.clinic, self.coordinator, staff_device["id"], 1, "cancel", reason="清理")
        with self.assertRaises(Conflict):
            self.held("term-5", "2026-09-29T11:15:00+08:00", "2026-09-29T11:45:00+08:00", room_id=room["id"], staff=None)
        after_end = self.held("term-6", "2026-09-29T11:30:00+08:00", "2026-09-29T12:00:00+08:00",
                              room_id=room["id"], staff=None)
        self.assertEqual(after_end["state"], "held")
        self.app.transition_appointment(self.clinic, self.coordinator, after_end["id"], 1, "cancel", reason="清理")
        # 完成服务：人员与设备立即释放，诊室保留到清洁准备间隔结束。
        done = self.held("term-7", "2026-09-29T14:00:00+08:00", "2026-09-29T14:30:00+08:00",
                         room_id=room["id"], device_ids=[device["id"]])
        self.app.transition_appointment(self.clinic, self.coordinator, done["id"], 1, "book",
                                        expected_resource_versions=done["resource_versions"])
        self.app.transition_appointment(self.clinic, self.coordinator, done["id"], 2, "arrive")
        self.app.transition_appointment(self.clinic, self.coordinator, done["id"], 3, "start")
        self.clock.set(datetime(2026, 9, 29, 6, 34, tzinfo=UTC))  # 本地 14:34，服务刚结束
        completed = self.app.transition_appointment(self.clinic, self.coordinator, done["id"], 4, "complete")
        room_release = next(r for r in completed["released_resources"] if r["resource_type"] == "room")
        self.assertEqual(room_release["release_due_at"], "2026-09-29T06:45:00Z")
        staff_again = self.held("term-8", "2026-09-29T14:35:00+08:00", "2026-09-29T15:05:00+08:00",
                                device_ids=[device["id"]])
        self.assertEqual(staff_again["state"], "held")
        self.app.transition_appointment(self.clinic, self.coordinator, staff_again["id"], 1, "cancel", reason="清理")
        with self.assertRaises(Conflict) as cleaning:
            self.held("term-9", "2026-09-29T14:40:00+08:00", "2026-09-29T15:10:00+08:00", room_id=room["id"], staff=None)
        self.assertEqual(cleaning.exception.details["conflicts"][0]["occupied_until"], "2026-09-29T06:45:00Z")
        self.clock.set(datetime(2026, 9, 29, 6, 46, tzinfo=UTC))
        swept = self.app.resources.release_due_resources(self.clinic, self.coordinator)
        # 未到诊诊室（11:30 到期）与完成诊室（清洁间隔 14:45 到期）均已到期。
        self.assertEqual(swept["released"], 2)
        after_cleaning = self.held("term-10", "2026-09-29T14:50:00+08:00", "2026-09-29T15:20:00+08:00",
                                   room_id=room["id"], staff=None)
        self.assertEqual(after_cleaning["state"], "held")

    def test_expired_hold_releases_room_and_device(self):
        room = self.room()
        device = self.device()
        self.held("exp-1", "2026-09-29T10:00:00+08:00", "2026-09-29T10:30:00+08:00",
                  room_id=room["id"], device_ids=[device["id"]])
        self.clock.set(datetime(2026, 9, 27, 12, 11, tzinfo=UTC))
        result = self.app.expire_holds(self.clinic)
        self.assertEqual((result["expired"], result["released_resources"]), (1, 3))
        again = self.held("exp-2", "2026-09-29T10:00:00+08:00", "2026-09-29T10:30:00+08:00",
                          room_id=room["id"], device_ids=[device["id"]])
        self.assertEqual(again["state"], "held")

    def test_calendar_and_overlap_follow_clinic_timezone_across_dst(self):
        clinic = self.app.create_clinic("北美诊所", "America/New_York")
        owner = self.app.create_staff(clinic["id"], "负责人", "owner")
        clinician = self.app.create_staff(clinic["id"], "医生", "clinician", actor_id=owner["id"])["id"]
        coordinator = self.app.create_staff(clinic["id"], "协调员", "coordinator", actor_id=owner["id"])["id"]
        patient = self.app.create_patient(clinic["id"], coordinator, "ny-1", "王女士")
        # 跨当地午夜的预约同时出现在两天的日历里，并带有跨午夜标注。
        self.app.create_appointment(clinic["id"], coordinator, patient["id"], "晚间护理",
                                    "2026-10-31T23:30:00-04:00", "2026-11-01T00:30:00-04:00", "ny-midnight")
        oct31 = self.app.resources.calendar(clinic["id"], coordinator, "2026-10-31")
        self.assertEqual(len(oct31["appointments"]), 1)
        midnight = oct31["appointments"][0]
        self.assertTrue(midnight["crosses_local_midnight"])
        self.assertEqual((midnight["local_date"], midnight["local_end_date"]), ("2026-10-31", "2026-11-01"))
        self.assertEqual(len(self.app.resources.calendar(clinic["id"], coordinator, "2026-11-01")["appointments"]), 1)
        # 秋令时切换：00:30 EDT 到 01:30 EST 实际跨越 2 小时并标注切换。
        self.app.create_appointment(clinic["id"], coordinator, patient["id"], "通宵观察",
                                    "2026-11-01T00:30:00-04:00", "2026-11-01T01:30:00-05:00", "ny-dst")
        nov1 = self.app.resources.calendar(clinic["id"], coordinator, "2026-11-01")
        dst = next(a for a in nov1["appointments"] if a["kind"] == "通宵观察")
        self.assertTrue(dst["spans_dst_transition"])
        self.assertEqual((dst["utc_offset_start"], dst["utc_offset_end"]), ("-0400", "-0500"))
        # 重叠判断基于真实时间线：重复小时里两个“01 点档”并不冲突。
        early = self.app.create_appointment(clinic["id"], coordinator, patient["id"], "复诊",
                                            "2026-11-01T01:00:00-04:00", "2026-11-01T01:45:00-04:00", "ny-early",
                                            staff_id=clinician)
        self.assertEqual(early["state"], "held")
        late = self.app.create_appointment(clinic["id"], coordinator, patient["id"], "复诊",
                                           "2026-11-01T01:15:00-05:00", "2026-11-01T01:50:00-05:00", "ny-late",
                                           staff_id=clinician)
        self.assertEqual(late["state"], "held")
        with self.assertRaises(Conflict):
            self.app.create_appointment(clinic["id"], coordinator, patient["id"], "复诊",
                                        "2026-11-01T01:10:00-04:00", "2026-11-01T01:40:00-04:00", "ny-clash",
                                        staff_id=clinician)

    def test_resource_schedule_reports_conflicting_resource_and_window(self):
        room = self.room(turnover=0)
        first = self.held("sch-1", "2026-09-29T10:00:00+08:00", "2026-09-29T10:30:00+08:00", room_id=room["id"])
        second = self.held("sch-2", "2026-09-29T12:00:00+08:00", "2026-09-29T12:30:00+08:00",
                           room_id=room["id"], staff=None)
        for appointment in (first, second):
            self.app.transition_appointment(self.clinic, self.coordinator, appointment["id"], 1, "book",
                                            expected_resource_versions=appointment["resource_versions"])
        # 模拟异常数据：第二条占用被改写为与第一条重叠。
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE appointment_resources SET occupied_from='2026-09-29T02:15:00Z',occupied_until='2026-09-29T02:45:00Z' "
                "WHERE appointment_id=? AND resource_type='room'", (second["id"],))
        schedule = self.app.resources.resource_schedule(self.clinic, self.coordinator, "2026-09-29")
        self.assertEqual(len(schedule["conflicts"]), 1)
        conflict = schedule["conflicts"][0]
        self.assertEqual(conflict["resource_type"], "room")
        self.assertEqual(conflict["resource_id"], room["id"])
        self.assertEqual((conflict["overlap_from"], conflict["overlap_until"]),
                         ("2026-09-29T02:15:00Z", "2026-09-29T02:30:00Z"))
        self.assertEqual(conflict["appointment_ids"], sorted([first["id"], second["id"]]))
        report = self.app.run_diagnostics(self.clinic, self.owner)
        self.assertIn("appointment.room_overlap", {item["code"] for item in report["findings"]})

    def test_inactive_resource_is_rejected_and_blocks_booking(self):
        room = self.room()
        appointment = self.held("off-1", "2026-09-29T10:00:00+08:00", "2026-09-29T10:30:00+08:00", room_id=room["id"])
        self.app.resources.update_room(self.clinic, self.owner, room["id"], 1, active=False)
        with self.assertRaises(ValidationError):
            self.held("off-2", "2026-09-29T11:00:00+08:00", "2026-09-29T11:30:00+08:00", room_id=room["id"], staff=None)
        with self.assertRaises(Conflict):
            self.app.transition_appointment(self.clinic, self.coordinator, appointment["id"], 1, "book",
                                            expected_resource_versions=appointment["resource_versions"])
        self.assertEqual(self.app.get_appointment(self.clinic, self.coordinator, appointment["id"])["state"], "cancelled")

    def test_http_login_patient_creation_and_validation_error(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), create_handler(self.app))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            request = Request(base + "/auth/token", data=json.dumps({"staff_id": self.owner,
                            "password": "LongPassphrase!2026"}).encode(), method="POST",
                              headers={"X-Clinic-ID": self.clinic, "Content-Type": "application/json"})
            with urlopen(request, timeout=3) as response:
                token = json.loads(response.read())["access_token"]
                self.assertEqual(response.status, 201)
            request = Request(base + "/patients", data=json.dumps({"external_ref": "http-1", "name": "周女士"}).encode(),
                              method="POST", headers={"X-Clinic-ID": self.clinic, "Authorization": f"Bearer {token}",
                                                       "Content-Type": "application/json"})
            with urlopen(request, timeout=3) as response:
                patient = json.loads(response.read())
                self.assertEqual(response.status, 201)
            request = Request(base + f"/patients/{patient['id']}", headers={"X-Clinic-ID": self.clinic,
                              "Authorization": "Bearer invalid"})
            with self.assertRaises(HTTPError) as error:
                urlopen(request, timeout=3)
            self.assertEqual(error.exception.code, 401)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
