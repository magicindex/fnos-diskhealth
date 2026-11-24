"""解析、健康评级与导出的回归测试（fixtures 来自真实设备输出）。"""

from __future__ import annotations

import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
FIXTURES = os.path.join(ROOT, "tests", "fixtures")

import export  # noqa: E402
import health  # noqa: E402
import raid  # noqa: E402
import smart  # noqa: E402


def load(name: str) -> dict:
    with open(os.path.join(FIXTURES, name), "r", encoding="utf-8") as fh:
        return json.load(fh)


def response(name: str) -> dict:
    return load(name)["Controllers"][0]["Response Data"]


class PercTest(unittest.TestCase):
    def setUp(self) -> None:
        self.ctrl = raid._parse_controller(
            0,
            response("perccli_info.json"),
            response("perccli_pd.json"),
            response("perccli_vd.json"),
            {"Ports": 8, "Hlth": "Opt"},
        )

    def test_controller(self) -> None:
        self.assertEqual(self.ctrl["model"], "PERC H710P Mini")
        self.assertEqual(self.ctrl["firmware"], "3.131.05-8148")
        self.assertEqual(self.ctrl["pci"], "0000:02:00.0")
        self.assertEqual(self.ctrl["health"], "ok")

    def test_physical_disks(self) -> None:
        self.assertEqual(len(self.ctrl["pds"]), 8)
        bays = [pd["bay"] for pd in self.ctrl["pds"]]
        self.assertEqual(bays, ["32:0", "32:1", "32:2", "32:5", "32:6", "32:7", "32:8", "32:9"])
        first = self.ctrl["pds"][0]
        self.assertEqual(first["model"], "ST2000NX0463")
        self.assertEqual(first["serial"], "TESTSN0001")
        self.assertEqual(first["temperature_c"], 30)
        self.assertFalse(first["smart_alert"])
        self.assertEqual(first["vd_id"], "c0/v0")

    def test_virtual_disks(self) -> None:
        self.assertEqual(len(self.ctrl["vds"]), 2)
        vd0, vd1 = self.ctrl["vds"]
        self.assertEqual(vd0["level"], "RAID5")
        self.assertEqual(vd0["size"], "3.637 TB")
        self.assertEqual(vd0["members"], ["32:0", "32:1", "32:2"])
        self.assertEqual(vd0["os_device"], "/dev/sda")
        self.assertEqual(vd1["members"], ["32:5", "32:6", "32:7", "32:8", "32:9"])
        self.assertEqual(vd1["health"], "ok")


class SmartTest(unittest.TestCase):
    def test_sas(self) -> None:
        info = smart._parse(0, load("smart_sas.json"), "")
        self.assertTrue(info["available"])
        self.assertTrue(info["passed"])
        self.assertEqual(info["temperature_c"], 30)
        self.assertEqual(info["power_on_hours"], 63511)
        self.assertEqual(info["scsi"]["grown_defects"], 0)
        self.assertEqual(info["protocol"], "SCSI")
        self.assertEqual(info["protocol_name"], "SAS (SPL-4)")
        self.assertEqual(info["vendor"], "SEAGATE")
        self.assertEqual(info["product"], "ST2000NX0463")
        self.assertEqual(info["temperature_trip_c"], 60)
        self.assertEqual(info["sas"]["read"]["corrected"], 2494118993)
        self.assertEqual(info["sas"]["grown_defects"], 0)
        self.assertEqual(info["sas"]["manufactured"], "2018-W05")
        metrics = smart.metrics(info)
        self.assertEqual(metrics["grown_defects"], 0)

    def test_sata(self) -> None:
        info = smart._parse(0, load("smart_sata.json"), "")
        self.assertTrue(info["passed"])
        self.assertEqual(info["protocol"], "ATA")
        self.assertGreater(len(info["attributes"]), 10)
        metrics = smart.metrics(info)
        self.assertIsNotNone(metrics["reallocated"])

    def test_nvme(self) -> None:
        info = smart._parse(0, load("smart_nvme.json"), "")
        self.assertTrue(info["passed"])
        self.assertEqual(info["protocol"], "NVMe")
        self.assertIsNotNone(info["nvme"]["percentage_used"])

    def test_standby(self) -> None:
        info = smart._parse(2, {"power_mode": "STANDBY", "smartctl": {}}, "")
        self.assertFalse(info["available"])
        self.assertIn("休眠", info["error"])


def disk(**overrides) -> dict:
    base = {
        "id": "c0.e32.s0",
        "kind": "raid",
        "state": "Onln",
        "state_label": "在线",
        "media": "HDD",
        "interface": "SAS",
        "temperature_c": 30,
        "media_errors": 0,
        "other_errors": 0,
        "predictive_failures": 0,
        "smart_alert": False,
        "smart": {"available": True, "passed": True, "attributes": [], "scsi": {}, "nvme": {}},
        "smart_metrics": {},
    }
    base.update(overrides)
    return base


class HealthTest(unittest.TestCase):
    def test_ok(self) -> None:
        verdict = health.evaluate(disk())
        self.assertEqual(verdict["health"], "ok")
        self.assertIn("运行正常", verdict["reasons"])

    def test_media_errors_critical(self) -> None:
        verdict = health.evaluate(disk(media_errors=3))
        self.assertEqual(verdict["health"], "critical")

    def test_smart_failed_critical(self) -> None:
        verdict = health.evaluate(disk(smart={"available": True, "passed": False, "attributes": [], "scsi": {}, "nvme": {}}))
        self.assertEqual(verdict["health"], "critical")

    def test_hot_disk(self) -> None:
        self.assertEqual(health.evaluate(disk(temperature_c=52))["health"], "warning")
        self.assertEqual(health.evaluate(disk(temperature_c=60))["health"], "critical")

    def test_unknown_without_smart(self) -> None:
        verdict = health.evaluate(
            disk(kind="direct", smart={"available": False, "error": "SMART 读取失败", "attributes": [], "scsi": {}, "nvme": {}})
        )
        self.assertEqual(verdict["health"], "unknown")

    def test_rebuilding(self) -> None:
        verdict = health.evaluate(disk(state="Rbld", state_label="重建中"))
        self.assertEqual(verdict["health"], "warning")


class ExportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshot = {
            "schema": 1,
            "generated_at": "2026-11-23T17:00:00+08:00",
            "host": "FnOSF10",
            "summary": {"disks": 1},
            "controllers": [],
            "disks": [
                {
                    **disk(),
                    "controller_id": "c0",
                    "controller_name": "PERC H710P Mini",
                    "bay": "32:0",
                    "bay_label": "EID 32 · 槽位 0",
                    "model": "ST2000NX0463",
                    "serial": "TESTSN0001",
                    "size": "1.818 TB",
                    "firmware": "NT31",
                    "power_on_hours": 63510,
                    "health": "ok",
                    "health_label": "正常",
                    "reasons": ["运行正常"],
                    "role": "阵列成员 · RAID5",
                }
            ],
            "warnings": [],
        }

    def test_csv(self) -> None:
        body, mime, name = export.export(self.snapshot, [], "csv")
        self.assertTrue(mime.startswith("text/csv"))
        self.assertTrue(name.endswith(".csv"))
        text = body.decode("utf-8-sig")
        self.assertIn("控制器,类型,盘位", text)
        self.assertIn("PERC H710P Mini", text)
        self.assertEqual(len(text.strip().splitlines()), 2)

    def test_json(self) -> None:
        body, mime, _ = export.export(self.snapshot, [], "json")
        payload = json.loads(body)
        self.assertEqual(payload["summary"]["total"], 1)
        self.assertEqual(payload["disks"][0]["serial"], "TESTSN0001")

    def test_txt(self) -> None:
        body, _, _ = export.export(self.snapshot, [], "txt")
        text = body.decode("utf-8")
        self.assertIn("RAID 阵列健康 / HARDWARE RAID・ARRAY MONITOR 日志", text)
        self.assertIn("ST2000NX0463", text)

    def test_filter(self) -> None:
        body, _, _ = export.export(self.snapshot, [], "csv", controller="c1")
        text = body.decode("utf-8-sig")
        self.assertEqual(len(text.strip().splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
