import unittest
from unittest.mock import Mock, patch
from core.disk_monitor import DiskMonitor, disk_snapshot


class DiskMonitorTests(unittest.TestCase):
    def setUp(self):
        self.send = Mock(return_value=True)
        self.clock = Mock(return_value=0)
        self.monitor = DiskMonitor('/unused', self.send, clock=self.clock)

    def check(self, used):
        with patch('core.disk_monitor.disk_snapshot', return_value=(used, 100)):
            self.monitor.check()

    def test_boundaries_escalation_and_cooldown(self):
        self.check(79.9)
        self.send.assert_not_called()
        for threshold in (80, 90, 95):
            self.check(threshold)
        self.assertEqual(self.send.call_count, 3)
        self.check(100)
        self.assertEqual(self.send.call_count, 3)
        self.clock.return_value = 21600
        self.check(100)
        self.assertEqual(self.send.call_count, 4)

    def test_recovery_hysteresis_and_new_incident(self):
        self.check(80)
        self.check(79)
        self.check(80)
        self.assertEqual(self.send.call_count, 1)
        self.check(74)
        self.assertEqual(self.monitor.level, 0)
        self.check(80)
        self.assertEqual(self.send.call_count, 3)

    def test_failed_delivery_retried(self):
        self.send.return_value = False
        self.check(99)
        self.send.return_value = True
        self.check(99)
        self.assertEqual(self.send.call_count, 2)
        self.assertEqual(self.monitor.level, 3)

    def test_transport_exception_does_not_suppress_retry(self):
        self.send.side_effect = RuntimeError()
        with self.assertRaises(RuntimeError):
            self.check(95)
        self.assertEqual(self.monitor.level, 0)

    def test_disabled(self):
        self.monitor.enabled = lambda: False
        self.check(100)
        self.send.assert_not_called()

    def test_available_blocks_include_reserved_space(self):
        with patch('core.disk_monitor.os.statvfs', return_value=Mock(f_blocks=1000, f_frsize=4096, f_bavail=100)):
            self.assertEqual(disk_snapshot('/unused'), (90, 409600))

    def test_missing_path_and_zero_capacity(self):
        with patch('core.disk_monitor.os.statvfs', side_effect=FileNotFoundError):
            with self.assertRaises(OSError):
                disk_snapshot('/unused')
        with patch('core.disk_monitor.os.statvfs', return_value=Mock(f_blocks=0, f_frsize=4096, f_bavail=0)):
            with self.assertRaises(OSError):
                disk_snapshot('/unused')
