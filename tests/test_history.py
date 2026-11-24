"""SQLite 历史采样与趋势查询测试。"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

import history  # noqa: E402


class HistoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        history.DATA_DIR = self.tmp.name
        history.DB_PATH = os.path.join(self.tmp.name, "history.db")
        history._initialized = False
        self.now = int(time.time())

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def snapshot(self, ts: int, temp: float, third: int = 0) -> dict:
        return {
            "generated_ts": ts,
            "disks": [
                {
                    "id": "c0.e32.s0",
                    "controller_id": "c0",
                    "bay_label": "EID 32 · 槽位 0",
                    "model": "ST2000NX0463",
                    "serial": "TESTSN0001",
                    "health": "ok",
                    "state": "Onln",
                    "temperature_c": temp,
                    "temperature_trip_c": 60,
                    "power_on_hours": 100 + third,
                    "media_errors": third,
                    "other_errors": 0,
                    "predictive_failures": 0,
                    "smart_metrics": {"grown_defects": 0, "read_uncorrected": 0},
                    "smart": {"passed": True},
                }
            ],
        }

    def test_record_and_query(self) -> None:
        for index, temp in enumerate((30.0, 31.0, 32.5)):
            history.record_snapshot(self.snapshot(self.now - (2 - index) * 3600, temp, index))
        disks = history.disks(30)
        self.assertEqual(len(disks), 1)
        self.assertEqual(disks[0]["samples"], 3)
        self.assertEqual(disks[0]["model"], "ST2000NX0463")
        data = history.series("c0.e32.s0", "temperature_c", 30)
        self.assertEqual(len(data["points"]), 3)
        self.assertEqual(data["stats"]["latest"], 32.5)
        self.assertEqual(data["stats"]["min"], 30.0)
        self.assertEqual(data["stats"]["max"], 32.5)
        self.assertEqual(data["unit"], "°C")

    def test_missing_metric(self) -> None:
        history.record_snapshot(self.snapshot(self.now, 30.0))
        data = history.series("c0.e32.s0", "reallocated", 30)
        self.assertEqual(data["points"], [])
        self.assertEqual(data["stats"]["samples"], 0)

    def test_export_csv_has_bom(self) -> None:
        history.record_snapshot(self.snapshot(self.now, 30.0))
        body, name = history.export_csv("c0.e32.s0", "temperature_c", 30)
        self.assertTrue(body.startswith(b"\xef\xbb\xbf"))
        self.assertIn(b"30.0", body)
        self.assertTrue(name.endswith(".csv"))


if __name__ == "__main__":
    unittest.main()
