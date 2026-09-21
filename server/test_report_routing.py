import tempfile
import unittest

import report_routing as report


class TerraNativeAccounting(unittest.TestCase):
    def test_terra_counts_as_native(self):
        entry = {
            "_at": report.datetime.datetime(2026, 9, 21, 12, 0, 0),
            "model": report.TERRA,
            "tier": report.TERRA,
            "gate": "apply",
            "speed": "default",
            "total_ms": 100,
            "jev_ms": 20,
            "attempts": [],
        }
        stats = {"lines": 1, "unparsable": 0, "undated": 0, "out_of_window": 0}
        with tempfile.TemporaryDirectory() as tmp:
            rep = report.summarize(
                [entry],
                7,
                stats,
                "memory",
                backtest_path=tmp + "/missing.json",
            )
        self.assertEqual(rep["served"]["native_turns"], 1)
        self.assertEqual(rep["served"]["models"][report.TERRA]["turns"], 1)


if __name__ == "__main__":
    unittest.main()
