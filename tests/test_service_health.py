import unittest
from unittest.mock import MagicMock, patch
from service_health import read_apex_health, apex_status_text

class ServiceHealthTests(unittest.TestCase):
    def read(self, row):
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value.fetchone.return_value = row
        with patch('service_health._connect', return_value=conn):
            result = read_apex_health('test-db')
        conn.close.assert_called_once()
        return result

    def test_fresh_waiting_and_error_states(self):
        health = self.read(('WAITING', 30, 900))
        self.assertEqual(health['state'], 'FRESH')
        self.assertIn('🟢', apex_status_text(health))
        self.assertIn('⚠️', apex_status_text(self.read(('ERROR', 5, 10))))

    def test_stale_heartbeat_is_not_reported_running(self):
        health = self.read(('SCANNING', 121, 200))
        self.assertEqual(health['state'], 'STALE')
        self.assertNotIn('🟢', apex_status_text(health))

    def test_missing_and_failed_reads_are_unknown(self):
        self.assertEqual(self.read(None)['state'], 'UNKNOWN')
        with patch('service_health._connect', side_effect=RuntimeError('offline')):
            self.assertEqual(read_apex_health('test-db')['state'], 'UNKNOWN')
