from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from trading.recovery_engine import RecoveryEngine


class RecoveryBrokerRetryTests(unittest.TestCase):
    def _engine(self) -> RecoveryEngine:
        return object.__new__(RecoveryEngine)

    def test_transient_broker_read_retries_then_succeeds(self):
        engine = self._engine()
        operation = Mock(
            side_effect=[
                RuntimeError("500 Server Error: Internal Server Error"),
                RuntimeError('{"code":50010000,"message":"internal server error occurred"}'),
                ["ok"],
            ]
        )

        with patch("trading.recovery_engine.time.sleep", return_value=None) as sleeper:
            result = engine._broker_read_with_retry(
                operation,
                label="unit-test",
            )

        self.assertEqual(result, ["ok"])
        self.assertEqual(operation.call_count, 3)
        self.assertEqual(sleeper.call_count, 2)

    def test_non_transient_broker_read_fails_immediately(self):
        engine = self._engine()
        operation = Mock(side_effect=RuntimeError("authentication failed"))

        with patch("trading.recovery_engine.time.sleep", return_value=None) as sleeper:
            with self.assertRaisesRegex(RuntimeError, "authentication failed"):
                engine._broker_read_with_retry(
                    operation,
                    label="unit-test",
                )

        self.assertEqual(operation.call_count, 1)
        sleeper.assert_not_called()


if __name__ == "__main__":
    unittest.main()
