"""Analytics report math and formatting tests (no ffmpeg required)."""

import json
import unittest

from videomixer.stats import bucketize, build_report, format_text

try:
    from . import helpers
except ImportError:
    import helpers


def make_report(**overrides):
    kwargs = dict(
        workspace="C:/ws",
        tolerance=1.5,
        tracks=[helpers.make_media("library/a.mp3", 10.0),
                helpers.make_media("library/b.mp3", 30.0),
                helpers.make_media("library/c.mp3", 200.0)],
        videos=[helpers.make_media("inputs/v1.mp4", 10.5, True),
                helpers.make_media("inputs/v2.mp4", 45.0, False)],
        uses={"C:/ws/library/a.mp3": 3, "C:/ws/other/deleted.mp3": 1},
        outputs=4,
        manifests=4,
        loudness_ready=2,
        onsets_ready=1,
        stale_entries=1,
    )
    kwargs.update(overrides)
    return build_report(**kwargs)


class TestBucketize(unittest.TestCase):
    def test_distribution_across_all_buckets(self):
        result = bucketize([5, 14.9, 20, 45, 61, 119.9, 200])
        by_label = {b["label"]: b["count"] for b in result}
        self.assertEqual(by_label["<15s"], 2)
        self.assertEqual(by_label["15-30s"], 1)
        self.assertEqual(by_label["30-60s"], 1)
        self.assertEqual(by_label["60-120s"], 2)
        self.assertEqual(by_label["2min+"], 1)

    def test_boundary_values_land_correctly(self):
        by_label = {b["label"]: b["count"]
                    for b in bucketize([15.0, 30.0, 60.0, 120.0])}
        self.assertEqual(by_label["<15s"], 0)
        self.assertEqual(by_label["15-30s"], 1)
        self.assertEqual(by_label["30-60s"], 1)
        self.assertEqual(by_label["60-120s"], 1)
        self.assertEqual(by_label["2min+"], 1)

    def test_empty_is_zeroed_not_crashing(self):
        result = bucketize([])
        self.assertEqual(sum(b["count"] for b in result), 0)


class TestBuildReport(unittest.TestCase):
    def test_coverage_flags_gap_and_counts(self):
        report = make_report()
        cov = {c["video"]: c for c in report["coverage"]}
        v1 = cov["v1.mp4"]
        self.assertEqual(v1["within_tolerance"], 1)
        self.assertFalse(v1["gap"])
        v2 = cov["v2.mp4"]
        self.assertEqual(v2["within_tolerance"], 0)
        self.assertTrue(v2["gap"])
        self.assertEqual(v2["longer"], 1)

    def test_usage_heat_sorted_and_orphaned_filtered(self):
        usage = make_report()["usage"]
        self.assertEqual([e["track"] for e in usage["top"]], ["a.mp3"])
        self.assertEqual(usage["orphan_ledger_entries"], 1)
        self.assertEqual(usage["used_tracks"], 1)
        self.assertEqual(usage["cold_tracks"], 2)

    def test_cache_readiness_counts(self):
        cache = make_report()["cache"]
        self.assertEqual(cache["loudness_ready"], 2)
        self.assertEqual(cache["onsets_ready"], 1)
        self.assertEqual(cache["stale_entries"], 1)


class TestFormatText(unittest.TestCase):
    def setUp(self):
        self.text = format_text(make_report())

    def test_gap_marker_present(self):
        self.assertIn("GAP", self.text)

    def test_heat_line_lists_top_track(self):
        self.assertIn("a.mp3 x3", self.text)

    def test_cold_pool_and_prewarm_hint(self):
        self.assertIn("cold pool: 2/3", self.text)
        self.assertIn("index --loudness --onsets", self.text)

    def test_ascii_only_output(self):
        self.text.encode("cp1252")

    def test_json_serializable(self):
        json.dumps(make_report())

    def test_empty_workspace_renders(self):
        text = format_text(make_report(tracks=[], videos=[], uses={},
                                       loudness_ready=0, onsets_ready=0))
        self.assertIn("library: 0 tracks", text)
        self.assertIn("(no input videos)", text)
        self.assertIn("nothing rendered yet", text)


if __name__ == "__main__":
    unittest.main()
