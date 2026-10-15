import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import railway_controller as controller


class ReviewPollDiagnosticTests(unittest.TestCase):
    def test_rate_limit_diagnostic_excludes_exception_url_body_and_credential(self):
        error = HTTPError('https://private.example/secret-path', 403, 'secret-reason',
                          {'X-RateLimit-Remaining':'0'}, None)
        ui = NS(api=NS(token='secret-credential'), poll=Mock(side_effect=error))
        with patch.object(controller, 'code_review_ui', return_value=ui), \
             self.assertLogs(controller.__name__, level='WARNING') as logs:
            controller.poll_code_review_requests()
        result = '\n'.join(logs.output)
        self.assertIn('http_status=403 category=RATE_LIMIT credential_configured=True', result)
        self.assertNotIn('secret-', result)
        self.assertNotIn('private.example', result)

    def test_unconfigured_authentication_and_non_http_failures_are_distinguished(self):
        for error, expected in (
            (HTTPError('https://private.example',401,'secret-reason',{},None),'http_status=401 category=AUTHENTICATION'),
            (TimeoutError('secret-timeout'),'http_status=None category=UNAVAILABLE'),
        ):
            ui = NS(api=NS(token=''), poll=Mock(side_effect=error))
            with self.subTest(error=type(error).__name__), \
                 patch.object(controller, 'code_review_ui', return_value=ui), \
                 self.assertLogs(controller.__name__, level='WARNING') as logs:
                controller.poll_code_review_requests()
            self.assertIn(expected+' credential_configured=False', '\n'.join(logs.output))
            self.assertNotIn('secret-', '\n'.join(logs.output))
