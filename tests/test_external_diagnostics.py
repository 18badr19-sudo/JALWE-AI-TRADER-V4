import unittest
from unittest.mock import Mock, patch
from datetime import datetime, timezone
from types import SimpleNamespace

import railway_controller as controller


class ExternalDiagnosticsTests(unittest.TestCase):
    def test_external_mode_never_reads_old_local_apex_file(self):
        with patch.object(controller, 'MANAGE_APEX_CHILD', False), \
             patch.object(controller, 'APEX_DIAGNOSTICS_FILE') as old_file, \
             patch.object(controller, 'shared_apex_diagnostics_lines', return_value=['shared reports']), \
             patch.object(controller.database, 'connection', side_effect=RuntimeError('test db')):
            text = controller.no_trade_diagnostics_text()
        old_file.exists.assert_not_called()
        old_file.read_text.assert_not_called()
        self.assertIn('shared reports', text)

    def test_shared_report_lists_timestamp_and_freshness(self):
        report = SimpleNamespace(symbol='AUDC', created_at=datetime.now(timezone.utc).isoformat(),
                                 metadata={'verdict': 'WATCH'})
        bridge = Mock()
        bridge.list_recent_research.return_value = [report]
        bridge.get_age_minutes.return_value = 1
        with patch('intelligence.external_research_bridge.get_external_research_bridge', return_value=bridge), \
             patch('service_health.read_apex_health', return_value={'state': 'UNKNOWN'}):
            text = '\n'.join(controller.shared_apex_diagnostics_lines())
        self.assertIn('AUDC', text)
        self.assertIn('NY', text)
        self.assertIn('30 دقيقة: 1', text)

    def test_rejections_command_routes_default_and_date(self):
        with patch.object(controller, 'rejection_audit_text', return_value='audit') as audit:
            self.assertEqual(controller.handle('/rejections'), 'audit')
            audit.assert_called_with(None)
            self.assertEqual(controller.handle('/rejections 2026-10-05'), 'audit')
            audit.assert_called_with('2026-10-05')
            controller.handle('/rejections extra extra')
            self.assertEqual(audit.call_count, 2)

    def test_rejections_invalid_date_and_db_error_are_honest(self):
        memory = Mock()
        with patch('decision_outcome_memory.get_decision_outcome_memory', return_value=memory):
            memory.rejection_audit.side_effect = ValueError('bad date')
            self.assertIn('YYYY-MM-DD', controller.rejection_audit_text('bad'))
            memory.rejection_audit.side_effect = RuntimeError('db unavailable')
            self.assertIn('تعذر', controller.rejection_audit_text())

    def test_opportunity_evidence_freezes_threshold_without_changing_analysis(self):
        from intelligence.decision_engine import DecisionEngine
        analysis = SimpleNamespace(score=66., approved=False, grade='REJECT',
                                   candidates=[], reasons=['reason'], warnings=['warning'])
        snapshot = DecisionEngine._opportunity_evidence(analysis, 70.)
        self.assertEqual(snapshot['minimum_score'], 70.)
        self.assertEqual(snapshot['score'], 66.)
        self.assertFalse(snapshot['approved'])
        self.assertEqual(analysis.score, 66.)
        self.assertEqual(DecisionEngine._opportunity_evidence(analysis, None), {})



if __name__ == '__main__':
    unittest.main()
