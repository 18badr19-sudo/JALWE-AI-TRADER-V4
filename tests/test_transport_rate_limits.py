import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from core.telegram_transport import open_telegram, TelegramRateLimit
from self_update.telegram_review import ReviewUI


def telegram_limit(delay):
    return HTTPError('https://private.invalid/token', 429, 'private reason', {},
                     io.BytesIO(json.dumps({'parameters': {'retry_after': delay}}).encode()))


class TelegramRateLimitTests(unittest.TestCase):
    def test_confirmed_rejection_waits_before_retrying_same_request(self):
        request, response, sleep = object(), object(), Mock()
        opener = Mock(side_effect=[telegram_limit(5), response])
        self.assertIs(open_telegram(request, timeout=15, opener=opener, sleep=sleep), response)
        sleep.assert_called_once_with(5)
        self.assertEqual(opener.call_args_list[0], opener.call_args_list[1])

    def test_second_rejection_is_not_retried_forever(self):
        opener, sleep = Mock(side_effect=[telegram_limit(5), telegram_limit(9)]), Mock()
        with self.assertRaises(TelegramRateLimit) as caught:
            open_telegram(object(), timeout=15, opener=opener, sleep=sleep)
        self.assertEqual(caught.exception.retry_after, 9)
        self.assertEqual(opener.call_count, 2)
        sleep.assert_called_once_with(5)

    def test_long_wait_is_returned_without_blocking_monitoring(self):
        opener, sleep = Mock(side_effect=telegram_limit(120)), Mock()
        with self.assertRaises(TelegramRateLimit) as caught:
            open_telegram(object(), timeout=15, opener=opener, sleep=sleep)
        self.assertEqual(caught.exception.retry_after, 120)
        sleep.assert_not_called()
        opener.assert_called_once()

    def test_ambiguous_timeout_and_other_http_errors_never_resend(self):
        for error in (TimeoutError('unknown delivery'), HTTPError('private', 503, 'bad gateway', {}, None),
                      HTTPError('private', 401, 'unauthorized', {}, None)):
            opener, sleep = Mock(side_effect=error), Mock()
            with self.subTest(error=type(error).__name__), self.assertRaises(type(error)):
                open_telegram(object(), timeout=15, opener=opener, sleep=sleep)
            opener.assert_called_once()
            sleep.assert_not_called()

    def test_invalid_provider_delay_uses_bounded_fallback(self):
        for delay in (None, -2, True, float('nan'), float('inf'), 'private'):
            opener, sleep = Mock(side_effect=[telegram_limit(delay), object()]), Mock()
            open_telegram(object(), timeout=15, opener=opener, sleep=sleep)
            sleep.assert_called_once_with(5)

    def test_both_senders_use_confirmed_rate_limit_retry(self):
        import railway_controller as controller
        import jalwe_research_watcher as watcher
        response = Mock(status=200)
        response.read.return_value = b'{"ok":true,"result":{}}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch.object(controller, 'TELEGRAM_TOKEN', 'test'), \
             patch('urllib.request.urlopen', side_effect=[telegram_limit(5), response]) as opener, \
             patch('core.telegram_transport.time.sleep') as sleep:
            self.assertTrue(controller.tg_call('sendMessage', {'text':'test'})['ok'])
            self.assertEqual(opener.call_count, 2)
            sleep.assert_called_once_with(5)
        with patch.object(watcher, 'TELEGRAM_ENABLED', True), \
             patch('urllib.request.urlopen', side_effect=[telegram_limit(5), response]) as opener, \
             patch('core.telegram_transport.time.sleep') as sleep:
            self.assertEqual(watcher.send_telegram('test'), (True, None))
            self.assertEqual(opener.call_count, 2)

    def test_poll_cooldown_keeps_monitoring_without_recursive_alert(self):
        import railway_controller as controller
        class EndLoop(Exception):
            pass
        def telegram(method, *args, **kwargs):
            if method == 'getWebhookInfo': return {'result': {'url': ''}}
            if method == 'getMe': return {'result': {'username': 'test'}}
            if method == 'getUpdates': raise TelegramRateLimit(120)
            raise AssertionError(method)
        with ExitStack() as stack:
            for name, value in [('TELEGRAM_TOKEN','test'),('TELEGRAM_CHAT_ID','test'),('AUTOSTART',False)]:
                stack.enter_context(patch.object(controller,name,value))
            for name in ('poll_filled_order_alerts','maybe_send_scheduled_reports',
                         'maybe_run_learning_cycle','poll_code_review_requests','send_message'):
                stack.enter_context(patch.object(controller,name))
            stack.enter_context(patch.object(controller,'alpaca_ready',return_value=True))
            stack.enter_context(patch.object(controller,'update_persistence_probe',return_value={}))
            stack.enter_context(patch.object(controller,'status_text',return_value='ok'))
            stack.enter_context(patch.object(controller.signal,'signal'))
            stack.enter_context(patch.object(controller.time,'monotonic',return_value=1000))
            stack.enter_context(patch.object(controller.time,'sleep'))
            monitor = stack.enter_context(patch.object(controller,'monitor_children',side_effect=[None,None,EndLoop]))
            calls = stack.enter_context(patch.object(controller,'tg_call',side_effect=telegram))
            alert = stack.enter_context(patch.object(controller,'notify_error'))
            with self.assertRaises(EndLoop): controller.main()
            self.assertEqual(monitor.call_count,3)
            self.assertEqual(sum(call.args[0]=='getUpdates' for call in calls.call_args_list),1)
            alert.assert_not_called()


class ReviewRateLimitTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.now = 10000.
        self.api = Mock(token='test')
        self.ui = ReviewUI(Path(folder.name)/'review.db', '123', api=self.api, clock=lambda:self.now)

    def test_github_reset_prevents_requests_until_reset_and_recovers(self):
        error = HTTPError('private', 403, 'private', {'X-RateLimit-Remaining':'0',
                          'X-RateLimit-Reset':'14000'}, None)
        self.api.request.side_effect = [error, []]
        with self.assertRaises(HTTPError): self.ui.poll(Mock())
        self.now = 13000
        self.ui.poll(Mock())
        self.assertEqual(self.api.request.call_count, 1)
        self.now = 14000
        self.ui.poll(Mock())
        self.assertEqual(self.api.request.call_count, 2)

    def test_secondary_retry_after_and_no_header_fallback(self):
        for headers, expected in (({'Retry-After':'1800'},11800), ({},13600)):
            self.ui.last_poll = 0
            self.ui.next_poll_at = 0
            self.api.request.side_effect = HTTPError('private',429,'private',headers,None)
            with self.assertRaises(HTTPError): self.ui.poll(Mock())
            self.assertEqual(self.ui.next_poll_at, expected)

    def test_unauthenticated_reads_are_paced_without_enabling_approval(self):
        self.api.token = ''
        self.api.request.return_value = []
        self.ui.poll(Mock())
        self.now += 300
        self.ui.poll(Mock())
        self.assertEqual(self.api.request.call_count, 1)
        self.now += 600
        self.ui.poll(Mock())
        self.assertEqual(self.api.request.call_count, 2)
