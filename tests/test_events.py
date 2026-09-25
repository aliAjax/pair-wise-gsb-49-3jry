import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, NotFound, PermissionDenied, ValidationError


CREATE_DATA = {'event_id': 'CAT-2026-01', 'attachment': 1000000.0, 'limit': 5000000.0, 'cession_pct': 0.4, 'loss_amount': 3000000.0, 'reinstatement_pct': 0.15, 'aggregate_prior': 0.0}
CLAIMS_OFFICER = Actor("cl", "claims_officer")
UW = Actor("uw", "underwriter")
# 合约层容量 = (500万 - 100万) × 0.4 = 160万


class EventLedgerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "test.db")
        self.service = build_service(self.db_path)

    def tearDown(self):
        self.temp.cleanup()

    def _bound_record(self, reference, event_id="CAT-2026-01", loss=3000000.0):
        record = self.service.create(UW, reference, dict(CREATE_DATA, event_id=event_id, loss_amount=loss))
        return self.service.act(Actor("uw-op", "underwriter"), record["id"], record["version"], "bind", {"underwriter_id": "UW-1"})

    def _submit(self, record, claim_number, event_id="CAT-2026-01", **extra):
        data = {"claim_number": claim_number, "event_id": event_id}
        data.update(extra)
        return self.service.act(CLAIMS_OFFICER, record["id"], record["version"], "submit_claim", data)

    def _summary(self, event_id="CAT-2026-01"):
        return self.service.get_event_summary(Actor("viewer", "underwriter"), event_id)

    def test_first_report_registers_and_following_claims_append(self):
        # 第一次报案携带发生时刻、预估总损失、报送人，随提交流程自动登记
        first = self._bound_record("RI-1")
        first = self._submit(first, "CLM-1", occurred_at="2026-09-20T08:30:00+00:00", estimated_total_loss=1000000.0)
        summary = self._summary()
        self.assertEqual(summary["occurred_at"], "2026-09-20T08:30:00+00:00")
        self.assertEqual(summary["estimated_total_loss"], 1000000.0)
        self.assertEqual(summary["reported_by"], "cl")
        self.assertEqual(summary["claim_count"], 1)
        self.assertEqual(summary["claims"][0]["status"], "admitted")
        # 后续赔案追加到已有事件，不再要求报案信息
        second = self._bound_record("RI-2")
        self._submit(second, "CLM-2")
        summary = self._summary()
        self.assertEqual(summary["claim_count"], 2)
        self.assertEqual([c["claim_number"] for c in summary["claims"]], ["CLM-1", "CLM-2"])

    def test_explicit_report_and_manual_append(self):
        with self.assertRaises(NotFound):
            self._summary("CAT-2026-09")
        # 先提赔案、后补登记事件
        record = self._bound_record("RI-M", event_id="CAT-2026-09")
        self._submit(record, "CLM-M", event_id="CAT-2026-09")
        event = self.service.report_event(CLAIMS_OFFICER, {
            "event_id": "CAT-2026-09", "occurred_at": "2026-09-21T00:00:00+00:00",
            "estimated_total_loss": 1200000.0, "reported_by": "reporter-1",
        })
        self.assertEqual(event["reported_by"], "reporter-1")
        claim = self.service.append_event_claim(CLAIMS_OFFICER, "CAT-2026-09", {"record_id": record["id"]})
        self.assertEqual(claim["status"], "admitted")
        with self.assertRaises(Conflict):
            self.service.append_event_claim(CLAIMS_OFFICER, "CAT-2026-09", {"record_id": record["id"]})
        # 重复登记事件被拒绝
        with self.assertRaises(Conflict):
            self.service.report_event(CLAIMS_OFFICER, {
                "event_id": "CAT-2026-09", "occurred_at": "2026-09-21T00:00:00+00:00",
                "estimated_total_loss": 1200000.0,
            })

    def test_over_capacity_blocks_calculation_until_supplement(self):
        # 预估总额200万 > 合约层容量160万，赔案留在待补证
        self.service.report_event(CLAIMS_OFFICER, {
            "event_id": "CAT-2026-02", "occurred_at": "2026-09-22T00:00:00+00:00",
            "estimated_total_loss": 2000000.0,
        })
        record = self._bound_record("RI-3", event_id="CAT-2026-02")
        record = self._submit(record, "CLM-3", event_id="CAT-2026-02")
        summary = self.service.get_event_summary(CLAIMS_OFFICER, "CAT-2026-02")
        self.assertEqual(summary["claims"][0]["status"], "pending_evidence")
        self.assertEqual(summary["pending_evidence_count"], 1)
        # 待补证不能进入核定
        with self.assertRaises(Conflict):
            self.service.act(CLAIMS_OFFICER, record["id"], record["version"], "calculate", {"approved_loss": 2800000.0})
        # 补证下调到容量内后恢复核定
        summary = self.service.supplement_event(CLAIMS_OFFICER, "CAT-2026-02", {"estimated_total_loss": 1500000.0})
        self.assertEqual(summary["claims"][0]["status"], "admitted")
        self.assertEqual(summary["pending_evidence_count"], 0)
        record = self.service.get_record(CLAIMS_OFFICER, record["id"])
        record = self.service.act(CLAIMS_OFFICER, record["id"], record["version"], "calculate", {"approved_loss": 2800000.0})
        self.assertEqual(record["state"], "calculated")
        self.assertEqual(record["payload"]["recoverable_amount"], 720000.0)

    def test_settled_claims_stay_untouched_by_supplement(self):
        self.service.report_event(CLAIMS_OFFICER, {
            "event_id": "CAT-2026-03", "occurred_at": "2026-09-23T00:00:00+00:00",
            "estimated_total_loss": 2000000.0,
        })
        record_a = self._bound_record("RI-A", event_id="CAT-2026-03")
        record_b = self._bound_record("RI-B", event_id="CAT-2026-03")
        record_a = self._submit(record_a, "CLM-A", event_id="CAT-2026-03")
        record_b = self._submit(record_b, "CLM-B", event_id="CAT-2026-03")
        # 补证到容量内，两件赔案恢复核定
        self.service.supplement_event(CLAIMS_OFFICER, "CAT-2026-03", {"estimated_total_loss": 1500000.0})
        record_a = self.service.act(CLAIMS_OFFICER, record_a["id"], record_a["version"], "calculate", {"approved_loss": 2800000.0})
        record_a = self.service.act(Actor("fin", "finance"), record_a["id"], record_a["version"], "settle", {"payment_reference": "PAY-A"})
        # 再次补证下调：已结算赔案保持原样，未结赔案不受影响
        summary = self.service.supplement_event(CLAIMS_OFFICER, "CAT-2026-03", {"estimated_total_loss": 1000000.0})
        by_number = {c["claim_number"]: c for c in summary["claims"]}
        self.assertEqual(by_number["CLM-A"]["status"], "settled")
        self.assertEqual(by_number["CLM-A"]["recoverable_amount"], 720000.0)
        self.assertEqual(by_number["CLM-B"]["status"], "admitted")
        self.assertEqual(summary["settled_total"], 720000.0)
        self.assertEqual(summary["outstanding_total"], 800000.0)
        settled_record = self.service.get_record(CLAIMS_OFFICER, record_a["id"])
        self.assertEqual(settled_record["state"], "settled")
        self.assertEqual(settled_record["payload"]["payment_reference"], "PAY-A")

    def test_restart_keeps_event_outstanding_and_claims(self):
        self.service.report_event(CLAIMS_OFFICER, {
            "event_id": "CAT-2026-04", "occurred_at": "2026-09-24T00:00:00+00:00",
            "estimated_total_loss": 1000000.0,
        })
        record = self._bound_record("RI-R", event_id="CAT-2026-04")
        record = self._submit(record, "CLM-R", event_id="CAT-2026-04")
        record = self.service.act(CLAIMS_OFFICER, record["id"], record["version"], "calculate", {"approved_loss": 2800000.0})
        self.assertEqual(record["payload"]["recoverable_amount"], 720000.0)
        # 重启：重新组装服务，直接读同一数据库
        restarted = build_service(self.db_path)
        summary = restarted.get_event_summary(CLAIMS_OFFICER, "CAT-2026-04")
        self.assertEqual(summary["claim_count"], 1)
        self.assertEqual(summary["claims"][0]["claim_number"], "CLM-R")
        self.assertEqual(summary["outstanding_total"], 720000.0)
        self.assertEqual(summary["balances"][0]["capacity"], 1600000.0)
        self.assertEqual(summary["balances"][0]["remaining"], 880000.0)
        self.assertEqual(len(restarted.list_event_summaries(CLAIMS_OFFICER)), 1)

    def test_legacy_flow_without_event_registration_unchanged(self):
        record = self._bound_record("RI-L", event_id="CAT-2026-99")
        record = self._submit(record, "CLM-L", event_id="CAT-2026-99")
        record = self.service.act(CLAIMS_OFFICER, record["id"], record["version"], "calculate", {"approved_loss": 2800000.0})
        self.assertEqual(record["state"], "calculated")
        with self.assertRaises(NotFound):
            self.service.get_event_summary(CLAIMS_OFFICER, "CAT-2026-99")

    def test_permissions_and_supplement_validation(self):
        with self.assertRaises(PermissionDenied):
            self.service.report_event(Actor("x", "finance"), {
                "event_id": "CAT-X", "occurred_at": "2026-09-25T00:00:00+00:00",
                "estimated_total_loss": 1000000.0,
            })
        with self.assertRaises(PermissionDenied):
            self.service.report_event(Actor("x", "outsider"), {
                "event_id": "CAT-X", "occurred_at": "2026-09-25T00:00:00+00:00",
                "estimated_total_loss": 1000000.0,
            })
        self.service.report_event(CLAIMS_OFFICER, {
            "event_id": "CAT-X", "occurred_at": "2026-09-25T00:00:00+00:00",
            "estimated_total_loss": 1000000.0,
        })
        with self.assertRaises(ValidationError):
            self.service.supplement_event(CLAIMS_OFFICER, "CAT-X", {"estimated_total_loss": 1000000.0})
        with self.assertRaises(ValidationError):
            self.service.supplement_event(CLAIMS_OFFICER, "CAT-X", {"estimated_total_loss": 2000000.0})
        # 未进入赔案流程的合约不能追加
        quoted = self.service.create(UW, "RI-Q", dict(CREATE_DATA, event_id="CAT-X"))
        with self.assertRaises(Conflict):
            self.service.append_event_claim(CLAIMS_OFFICER, "CAT-X", {"record_id": quoted["id"]})


if __name__ == "__main__":
    unittest.main()
