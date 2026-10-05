import json
import unittest
from unittest.mock import Mock, patch

import railway_controller as controller


class ExtendedExitStatusTests(unittest.TestCase):
    def status(self, payload):
        db = Mock()
        db.connection.return_value.__enter__ = Mock(return_value=db)
        db.connection.return_value.__exit__ = Mock(return_value=False)
        db.execute.return_value.fetchone.return_value = (
            {'metadata_json': json.dumps(payload)} if payload is not None else None
        )
        with patch.object(controller, 'database', db), patch.dict('os.environ', {'JALWE_EXTENDED_EXIT_ENABLED': 'true'}):
            return controller.extended_exit_status_text()

    def test_subscription_failure_is_visible(self):
        self.assertIn('صلاحية بيانات SIP غير متاحة', self.status(
            {'status': 'UNAVAILABLE', 'reason': 'SUBSCRIPTION_REQUIRED'}))

    def test_missing_diagnostic_does_not_claim_available(self):
        self.assertIn('غير مؤكدة', self.status(None))

    def test_provider_failure_does_not_claim_subscription_failure(self):
        self.assertIn('تعذر الوصول', self.status({'status': 'UNAVAILABLE', 'reason': 'PROVIDER_ERROR'}))

    def test_access_success_still_requires_fresh_quote(self):
        self.assertIn('مشروط بسعر حديث', self.status({'status': 'AVAILABLE'}))
