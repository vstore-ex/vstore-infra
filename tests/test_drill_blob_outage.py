import unittest

from drill_blob_outage import analyse

MIN = 60
MS = 60_000


class AnalyseTests(unittest.TestCase):
    def test_block_deleted_before_upload_counts_as_lost(self):
        blocks = {
            "A": {"min": 0, "max": 12 * MS, "seen": 18 * MIN, "gone": None},
            "B": {"min": 12 * MS, "max": 24 * MS, "seen": 30 * MIN, "gone": 72 * MIN},
            "C": {"min": 24 * MS, "max": 36 * MS, "seen": 42 * MIN, "gone": None},
        }
        result = analyse(blocks, shipped={"A", "C"}, outage_start=20 * MIN, outage_end=80 * MIN)
        self.assertEqual(result["lost_blocks"], 1)
        self.assertEqual(result["lost_ranges_ms"], [[12 * MS, 24 * MS]])
        self.assertEqual(result["lost_data_seconds"], 12 * MIN)
        self.assertEqual(result["tolerance_seconds"], 52 * MIN)
        self.assertIsNone(result["tolerance_lower_bound_seconds"])

    def test_no_loss_reports_only_a_lower_bound(self):
        blocks = {"A": {"min": 0, "max": 12 * MS, "seen": 18 * MIN, "gone": 60 * MIN}}
        result = analyse(blocks, shipped={"A"}, outage_start=20 * MIN, outage_end=50 * MIN)
        self.assertEqual(result["lost_blocks"], 0)
        self.assertIsNone(result["tolerance_seconds"])
        self.assertEqual(result["tolerance_lower_bound_seconds"], 30 * MIN)


if __name__ == "__main__":
    unittest.main()
