import unittest
import urllib.error
from unittest.mock import patch

import railway_controller as controller
from railway_controller import TelegramPollHealth


class TelegramPollHealthTests(unittest.TestCase):
    def test_short_poll_failure_does_not_send_error_alert(self):
        class EndLoop(Exception):
            pass

        def telegram(method, *args, **kwargs):
            if method == 'getWebhookInfo':
                return {'result': {'url': ''}}
            if method == 'getMe':
                return {'result': {'username': 'test'}}
            raise TimeoutError('The read operation timed out')

        with patch.object(controller, 'alpaca_ready', return_value=True), \
             patch.object(controller, 'TELEGRAM_TOKEN', 'test-token'), \
             patch.object(controller, 'TELEGRAM_CHAT_ID', 'test-chat'), \
             patch.object(controller.signal, 'signal'), \
             patch.object(controller, 'update_persistence_probe', return_value={}), \
             patch.object(controller, 'AUTOSTART', False), \
             patch.object(controller, 'tg_call', side_effect=telegram), \
             patch.object(controller, 'send_message'), \
             patch.object(controller, 'status_text', return_value='ok'), \
             patch.object(controller, 'monitor_children'), \
             patch.object(controller, 'poll_filled_order_alerts'), \
             patch.object(controller, 'maybe_send_scheduled_reports'), \
             patch.object(controller, 'maybe_run_learning_cycle'), \
             patch.object(controller, 'notify_error') as alert, \
             patch.object(controller.time, 'sleep', side_effect=EndLoop):
            with self.assertRaises(EndLoop):
                controller.main()
            alert.assert_not_called()

    def test_short_outage_then_recovery_resets_clock(self):
        health = TelegramPollHealth(90)
        self.assertEqual(health.failure_age(100), 0)
        self.assertEqual(health.failure_age(160), 60)
        self.assertLess(health.failure_age(160), health.alert_after_seconds)
        health.success()
        self.assertEqual(health.failure_age(200), 0)

    def test_continuous_transport_failure_becomes_alertable(self):
        health = TelegramPollHealth(90)
        health.failure_age(100)
        self.assertEqual(health.failure_age(190), health.alert_after_seconds)
        for error in (TimeoutError('The read operation timed out'),
                      urllib.error.URLError('timed out'),
                      RuntimeError('Telegram getUpdates: Bad Gateway'),
                      RuntimeError('Telegram getUpdates: HTTP 503')):
            with self.subTest(error=str(error)):
                self.assertTrue(health.is_transient(error))

    def test_bad_credentials_and_handler_error_are_not_transient(self):
        for error in (RuntimeError('Telegram getUpdates: Unauthorized'),
                      ValueError('Invalid controller command')):
            self.assertFalse(TelegramPollHealth.is_transient(error))


if __name__ == '__main__':
    unittest.main()
