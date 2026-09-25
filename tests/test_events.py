import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied


# 单合约容量 = (5,000,000 - 1,000,000) * 0.4 = 1,600,000
CONTRACT = {'event_id': 'CAT-2026-01', 'attachment': 1000000.0, 'limit': 5000000.0, 'cession_pct': 0.4, 'loss_amount': 3000000.0, 'reinstatement_pct': 0.15, 'aggregate_prior': 0.0}
REPORT = {'event_id': 'CAT-2026-01', 'occurred_at': '2026-09-20T08:30', 'estimated_total_loss': 1000000.0}
UW = Actor('uw-1', 'underwriter')
CLAIMS = Actor('clm-1', 'claims_officer')
FINANCE = Actor('fin-1', 'finance')


class EventLedgerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / 'test.db')
        self.service = build_service(self.db_path)

    def tearDown(self):
        self.temp.cleanup()

    def _submit_claim(self, reference, loss_amount=3000000.0):
        data = dict(CONTRACT, loss_amount=loss_amount)
        record = self.service.create(UW, reference, data)
        record = self.service.act(UW, record['id'], record['version'], 'bind', {'underwriter_id': 'UW-8'})
        record = self.service.act(CLAIMS, record['id'], record['version'], 'submit_claim', {'claim_number': 'CLM-' + reference, 'event_id': 'CAT-2026-01'})
        return record

    def _event_pk(self):
        return self.service.list_events(CLAIMS)[0]['id']

    def test_first_report_registers_and_duplicate_rejected(self):
        event = self.service.report_event(CLAIMS, REPORT)
        self.assertEqual(event['occurred_at'], '2026-09-20T08:30')
        self.assertEqual(event['estimated_total_loss'], 1000000.0)
        self.assertEqual(event['reported_by'], 'clm-1')
        entries = self.service.get_event(CLAIMS, event['id'])['entries']
        self.assertEqual(entries[0]['kind'], 'report')
        with self.assertRaises(Conflict):
            self.service.report_event(CLAIMS, REPORT)

    def test_claims_append_to_existing_event(self):
        self.service.report_event(CLAIMS, REPORT)
        self._submit_claim('RI-1')
        self._submit_claim('RI-2')
        detail = self.service.get_event(CLAIMS, self._event_pk())
        self.assertEqual(detail['summary']['claim_count'], 2)
        self.assertEqual({c['claim_number'] for c in detail['claims']}, {'CLM-RI-1', 'CLM-RI-2'})

    def test_estimate_above_capacity_blocks_calculate_until_supplemented(self):
        self.service.report_event(CLAIMS, dict(REPORT, estimated_total_loss=2000000.0))
        record = self._submit_claim('RI-1')
        with self.assertRaises(Conflict):
            self.service.act(CLAIMS, record['id'], record['version'], 'calculate', {'approved_loss': 1500000.0})
        event_pk = self._event_pk()
        updated = self.service.supplement_event(CLAIMS, event_pk, 1, {'estimated_total_loss': 1500000.0, 'note': '补证下调'})
        self.assertEqual(updated['status'], 'normal')
        record = self.service.act(CLAIMS, record['id'], record['version'], 'calculate', {'approved_loss': 1500000.0})
        self.assertEqual(record['state'], 'calculated')

    def test_settled_claims_untouched_by_supplement(self):
        self.service.report_event(CLAIMS, REPORT)
        first = self._submit_claim('RI-1')
        first = self.service.act(CLAIMS, first['id'], first['version'], 'calculate', {'approved_loss': 2800000.0})
        first = self.service.act(FINANCE, first['id'], first['version'], 'settle', {'payment_reference': 'PAY-1'})
        event_pk = self._event_pk()
        # 两合约合计容量 3,200,000，补证上调到 3,500,000 后进入待补证
        second = self._submit_claim('RI-2')
        updated = self.service.supplement_event(CLAIMS, event_pk, 1, {'estimated_total_loss': 3500000.0, 'note': '初估不足'})
        self.assertEqual(updated['status'], 'pending_supplementation')
        with self.assertRaises(Conflict):
            self.service.act(CLAIMS, second['id'], second['version'], 'calculate', {'approved_loss': 1500000.0})
        updated = self.service.supplement_event(CLAIMS, event_pk, 2, {'estimated_total_loss': 3000000.0, 'note': '补证下调到容量内'})
        self.assertEqual(updated['status'], 'normal')
        second = self.service.act(CLAIMS, second['id'], second['version'], 'calculate', {'approved_loss': 1500000.0})
        self.assertEqual(second['state'], 'calculated')
        settled = self.service.get_record(CLAIMS, first['id'])
        self.assertEqual(settled['state'], 'settled')
        self.assertEqual(settled['payload']['recoverable_amount'], 720000.0)
        self.assertEqual(settled['payload']['payment_reference'], 'PAY-1')

    def test_restart_preserves_event_ledger(self):
        self.service.report_event(CLAIMS, REPORT)
        record = self._submit_claim('RI-1')
        self.service.act(CLAIMS, record['id'], record['version'], 'calculate', {'approved_loss': 2800000.0})
        restarted = build_service(self.db_path)
        events = restarted.list_events(CLAIMS)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['summary']['outstanding_amount'], 720000.0)
        detail = restarted.get_event(CLAIMS, events[0]['id'])
        self.assertEqual([c['claim_number'] for c in detail['claims']], ['CLM-RI-1'])
        self.assertEqual(detail['balance']['layer_capacity'], 1600000.0)

    def test_permission_and_version_conflict(self):
        with self.assertRaises(PermissionDenied):
            self.service.report_event(UW, REPORT)
        event = self.service.report_event(CLAIMS, REPORT)
        with self.assertRaises(PermissionDenied):
            self.service.supplement_event(FINANCE, event['id'], 1, {'estimated_total_loss': 900000.0})
        with self.assertRaises(Conflict):
            self.service.supplement_event(CLAIMS, event['id'], 99, {'estimated_total_loss': 900000.0})


if __name__ == '__main__':
    unittest.main()
