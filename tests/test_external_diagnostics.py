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


if __name__ == '__main__':
    unittest.main()
