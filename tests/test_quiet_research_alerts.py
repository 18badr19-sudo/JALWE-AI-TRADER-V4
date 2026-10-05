import unittest
from unittest.mock import patch

import jalwe_research_watcher as watcher


class QuietResearchAlertTests(unittest.TestCase):
    def payload(self, execution=None):
        return {'symbol': 'TEST', 'jalwe': {'state': 'READY_FOR_PAPER_EXECUTION',
                'ready_for_execution': True, 'entry_price': 10, 'stop_price': 9},
                'execution': execution or {}}

    def test_changing_research_does_not_send_messages_in_quiet_mode(self):
        state = {}
        with patch.object(watcher, 'QUIET_RESEARCH_ALERTS', True), patch.object(watcher, 'send_telegram') as send:
            for score in range(300):
                payload = self.payload()
                payload['jalwe']['ai_score'] = score
                self.assertEqual(watcher.notify_if_changed(payload, state), 'RESEARCH_ALERTS_QUIET')
                watcher.notify_execution_lifecycle(payload, state)
        send.assert_not_called()

    def test_confirmed_entry_still_reaches_management_alert_path(self):
        payload = self.payload({'orchestrator_state': 'ENTRY_MANAGED', 'managed_trade_id': 't'})
        with patch.object(watcher, 'QUIET_RESEARCH_ALERTS', True), patch.object(
            watcher, '_managed_trade_snapshot', return_value=None
        ) as snapshot, patch.object(watcher, 'send_lifecycle_once') as send:
            watcher.notify_execution_lifecycle(payload, {})
        snapshot.assert_called_once_with('t')
        send.assert_not_called()  # no trigger alert; no fabricated entry without saved trade

    def test_fill_and_protection_notifications_are_not_muted(self):
        with patch.object(watcher, 'QUIET_RESEARCH_ALERTS', True), patch.object(
            watcher, 'LIFECYCLE_ALERTS_ENABLED', True
        ), patch.object(watcher, 'save_state'), patch.object(
            watcher, 'send_telegram', return_value=(True, None)
        ) as send:
            watcher.send_lifecycle_once({}, 'PROTECTIVE_STOP:confirmed', 'confirmed protection')
        send.assert_called_once_with('confirmed protection')

    def test_verbose_mode_can_still_send_research(self):
        with patch.object(watcher, 'QUIET_RESEARCH_ALERTS', False), patch.object(
            watcher, 'save_state'
        ), patch.object(watcher, 'send_telegram', return_value=(True, None)) as send:
            self.assertEqual(watcher.notify_if_changed(self.payload(), {}), 'SENT')
        send.assert_called_once()
