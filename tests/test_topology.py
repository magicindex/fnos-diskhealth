"""背板 / 扩展器解析与统一拓扑测试。"""

from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

import collector  # noqa: E402
import raid  # noqa: E402


class TopologyTest(unittest.TestCase):
    def test_enclosure_parse(self) -> None:
        data = {
            "Enclosure /c0/e32 ": {
                "Information": {
                    "Device ID": 32,
                    "Position": "1",
                    "Connector Name": "00 & 00 x8",
                    "Enclosure Type": "SES",
                    "Status": "OK",
                    "Device Type": "Backplane",
                },
                "Inquiry Data": {
                    "Vendor Identification": "DP",
                    "Product Identification": "BP12G+EXP",
                    "Product Revision Level": "1.03",
                },
                "Properties": [{"EID": 32, "Slots": 10, "PD": 8, "PS": 0, "Fans": 0, "TSs": 0, "Alms": 0}],
            }
        }
        items = raid._parse_enclosures(data, "c0")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["product"], "BP12G+EXP")
        self.assertTrue(items[0]["ses"])
        self.assertTrue(items[0]["expander"])
        self.assertEqual(items[0]["slots"], 10)

    def test_build_topology(self) -> None:
        controller = {
            "id": "c0",
            "model": "PERC",
            "kind": "raid",
            "health": "ok",
            "enclosures": [
                {
                    "id": "c0/e32",
                    "product": "BP12G+EXP",
                    "drive_ids": ["c0.e32.s0"],
                    "drive_count": 1,
                }
            ],
            "expanders": [],
            "vds": [],
            "details": {},
        }
        disk = {"id": "c0.e32.s0", "controller_id": "c0", "bay": "32:0", "health": "ok", "model": "ST2000"}
        topology = collector._build_topology([controller], [disk])
        self.assertEqual(topology["enclosures"], 1)
        self.assertEqual(topology["controllers"][0]["enclosures"][0]["drive_count"], 1)
        self.assertEqual(topology["controllers"][0]["enclosures"][0]["drives"][0]["id"], "c0.e32.s0")


if __name__ == "__main__":
    unittest.main()
