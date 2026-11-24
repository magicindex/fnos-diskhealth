"""AI 配置、状态、脱敏与报告导出测试。"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

import ai  # noqa: E402


class AITest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        ai.DATA_DIR = self.tmp.name
        ai.CONFIG_PATH = os.path.join(self.tmp.name, "ai_config.json")
        ai.REPORT_DIR = os.path.join(self.tmp.name, "ai_reports")
        ai.REPORT_INDEX = os.path.join(self.tmp.name, "ai_reports.jsonl")
        self.config = ai.save_config(
            {
                "provider": "deepseek",
                "base_url": "https://api.deepseek.com/v1",
                "model": "deepseek-chat",
                "api_key": "sk-secret",
                "redact_serials": True,
            }
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def snapshot(self) -> dict:
        return {
            "host": "FnOSF10",
            "generated_at": "2026-11-23T22:00:00+08:00",
            "summary": {"disks": 1, "ok": 0, "warning": 1, "critical": 0, "unknown": 0},
            "controllers": [],
            "disks": [
                {
                    "id": "c0.e32.s0",
                    "controller_name": "PERC H710P Mini",
                    "bay": "32:0",
                    "bay_label": "EID 32 · 槽位 0",
                    "health": "warning",
                    "reasons": ["温度偏高"],
                    "model": "ST2000NX0463",
                    "serial": "TESTSN0001",
                    "interface": "SAS",
                    "protocol_detail": "SAS (SPL-4)",
                    "temperature_c": 52,
                    "temperature_trip_c": 60,
                    "smart": {"passed": True, "sas": {"read": {"corrected": 1, "uncorrected": 0}}},
                }
            ],
        }

    def test_public_config_hides_key(self) -> None:
        self.assertNotIn("api_key", self.config)
        self.assertTrue(self.config["has_key"])
        status = ai.status()
        self.assertTrue(status["configured"])
        self.assertNotIn("base_url", status)

    def test_provider_change_clears_old_key(self) -> None:
        ai.save_config({"provider": "openai", "api_key": "", "base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"})
        self.assertFalse(ai.public_config()["has_key"])

    def test_dataset_redacts_serial(self) -> None:
        dataset, selected = ai.build_dataset(self.snapshot(), "all", None, True)
        self.assertEqual(len(selected), 1)
        self.assertEqual(dataset["disks"][0]["serial"], "TEST***")

    def test_analyze_and_export(self) -> None:
        fake = {"content": "## 总体结论\n正常", "usage": {"total_tokens": 10}, "model": "deepseek-chat"}
        with mock.patch.object(ai, "chat", return_value=fake):
            report = ai.analyze(self.snapshot(), ai.load_config(), "issues", None, user={"uid": 1000, "username": "admin"})
        self.assertEqual(report["uid"], "1000")
        self.assertEqual(report["content"], "## 总体结论\n正常")
        self.assertTrue(ai.get_report(report["id"]))
        body, mime, name = ai.export_report(report, "md")
        self.assertIn(b"RAID", body)
        self.assertTrue(mime.startswith("text/markdown"))
        self.assertTrue(name.endswith(".md"))


if __name__ == "__main__":
    unittest.main()
