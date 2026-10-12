"""Multi-movement (scene-aware BGM rotation) tests."""

import unittest

from videomixer.config import Style, load_style
from videomixer.design import Movement, build_design, plan_boundaries
from videomixer.render import build_filter_graph, build_inputs, input_layout

try:
    from . import helpers as H
except ImportError:
    import helpers


class TestPlanBoundaries(unittest.TestCase):
    def test_short_video_gets_no_boundaries(self):
        self.assertEqual(plan_boundaries(10.0, 12.0, [], 2.0), [])
        self.assertEqual(plan_boundaries(18.0, 12.0, [], 2.0), [])

    def test_uniform_boundaries_without_gaps(self):
        self.assertEqual(plan_boundaries(30.0, 9.0, [], 1.0), [10.0, 20.0])
        bounds = plan_boundaries(60.0, 15.0, [], 1.0)
        self.assertEqual(bounds, [15.0, 30.0, 45.0])

    def test_boundary_snaps_to_gap_center(self):
        bounds = plan_boundaries(30.0, 12.0, [(9.0, 11.5)], 2.0)
        self.assertEqual(bounds, [10.25, 20.0])

    def test_out_of_window_stays_uniform(self):
        bounds = plan_boundaries(30.0, 12.0, [(5.0, 6.0)], 2.0)
        self.assertEqual(bounds, [10.0, 20.0])

    def test_boundaries_stay_ordered_and_inside(self):
        gaps = [(9.5, 11.0), (28.0, 32.0)]
        bounds = plan_boundaries(40.0, 12.0, gaps, 3.0)
        self.assertTrue(all(b > 1.0 for b in bounds))
        self.assertTrue(all(b < 39.0 for b in bounds))
        self.assertEqual(bounds, sorted(bounds))


class TestMovementGraph(unittest.TestCase):
    def design(self):
        style = Style(name="suite", loudness_lufs=None,
                      movement_s=10.0, crossfade_s=1.5)
        beds = [H.make_media(f"library/bed{i}.mp3", 9.0 + i)
                for i in range(3)]
        movements = [
            Movement(track=beds[0], start_s=0.0, end_s=10.0),
            Movement(track=beds[1], start_s=10.0, end_s=20.0),
            Movement(track=beds[2], start_s=20.0, end_s=30.0),
        ]
        return H.make_design(video_dur=30.0, voiced=False, track_dur=9.0,
                             style=style, stats=None) , movements

    def test_layout_indices_for_three_beds(self):
        d, movements = self.design()
        d.movements = movements
        layout = input_layout(d)
        self.assertEqual(layout["bgm0"], 1)
        self.assertEqual(layout["bgm1"], 2)
        self.assertEqual(layout["bgm2"], 3)

    def test_graph_has_acrossfade_chain_and_segment_trims(self):
        d, movements = self.design()
        d.movements = movements
        g = build_filter_graph(d)
        self.assertIn("[seg0][seg1]acrossfade=d=1.500:c1=tri:c2=tri[mv1]", g)
        self.assertIn("[mv1][seg2]acrossfade=d=1.500:c1=tri:c2=tri[mvfinal]", g)
        self.assertIn("atrim=start=10.000:end=21.500", g)
        self.assertIn("atrim=start=20.000:end=30.000", g)
        self.assertIn("[mvfinal]afade=t=in:st=0:d=0.800,"
                      "afade=t=out:st=28.500:d=1.500[musicbus]", g)
        self.assertNotIn("loudnorm", g)

    def test_global_fades_applied_on_bus(self):
        style = Style(name="x", loudness_lufs=None, fade_in_s=1.0,
                      fade_out_s=2.0, movement_s=10.0, crossfade_s=1.5)
        d, movements = self.design()
        d.movements = movements
        d.style = style
        g = build_filter_graph(d)
        self.assertIn("[mvfinal]afade=t=in:st=0:d=1.000,"
                      "afade=t=out:st=28.000:d=2.000[musicbus]", g)

    def test_single_bed_path_unchanged(self):
        d = H.make_design(voiced=True, stats=None)
        g = build_filter_graph(d)
        self.assertIn("[1:a]aformat", g)
        self.assertNotIn("acrossfade", g)

    def test_inputs_include_each_bed(self):
        d, movements = self.design()
        d.movements = movements
        joined = " ".join(build_inputs(d))
        self.assertIn("bed0.mp3", joined)
        self.assertIn("bed1.mp3", joined)
        self.assertIn("bed2.mp3", joined)


class TestMovementConfig(unittest.TestCase):
    def test_defaults_off(self):
        style = load_style("x", {})
        self.assertIsNone(style.movement_s)
        self.assertAlmostEqual(style.crossfade_s, 2.0)

    def test_explicit_values(self):
        style = load_style("x", {"movement": 25, "crossfade": 3.5})
        self.assertAlmostEqual(style.movement_s, 25.0)
        self.assertAlmostEqual(style.crossfade_s, 3.5)

    def test_movement_in_style_dict(self):
        style = Style(name="y", movement_s=20.0)
        self.assertEqual(style.to_dict()["movement_s"], 20.0)


class TestFingerprintIncludesMovements(unittest.TestCase):
    def test_different_movements_different_fingerprint(self):
        style = Style(name="s", loudness_lufs=None, movement_s=10.0)
        beds = [H.make_media("library/b0.mp3", 9.0),
                H.make_media("library/b1.mp3", 9.0)]
        a = H.make_design(style=style, stats=None)
        b = H.make_design(style=style, stats=None)
        a.movements = [Movement(track=beds[0], start_s=0.0, end_s=15.0),
                       Movement(track=beds[1], start_s=15.0, end_s=30.0)]
        b.movements = [Movement(track=beds[1], start_s=0.0, end_s=15.0),
                       Movement(track=beds[0], start_s=15.0, end_s=30.0)]
        self.assertNotEqual(a.fingerprint(), b.fingerprint())


if __name__ == "__main__":
    unittest.main()
