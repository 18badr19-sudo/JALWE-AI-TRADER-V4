from __future__ import annotations

import json
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch


from core import persistence_probe


class PersistenceProbeTests(
    unittest.TestCase
):
    def test_probe_id_survives_and_boot_count_increments(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(
                temp_dir
            )

            first = persistence_probe.record_persistence_boot(
                root
            )

            second = persistence_probe.record_persistence_boot(
                root
            )

            self.assertEqual(
                first[
                    "probe_id"
                ],
                second[
                    "probe_id"
                ],
            )

            self.assertEqual(
                first[
                    "boot_count"
                ],
                1,
            )

            self.assertEqual(
                second[
                    "boot_count"
                ],
                2,
            )

            marker_path = (
                root
                / persistence_probe
                .PROBE_FILENAME
            )

            saved = json.loads(
                marker_path.read_text(
                    encoding="utf-8"
                )
            )

            self.assertEqual(
                saved[
                    "boot_count"
                ],
                2,
            )

    def test_status_confirms_writeability(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(
                temp_dir
            )

            persistence_probe.record_persistence_boot(
                root
            )

            status = persistence_probe.persistence_status(
                root
            )

            self.assertTrue(
                status[
                    "marker_exists"
                ]
            )

            self.assertTrue(
                status[
                    "write_test_ok"
                ]
            )

            self.assertEqual(
                status[
                    "boot_count"
                ],
                1,
            )


if __name__ == "__main__":
    unittest.main()
