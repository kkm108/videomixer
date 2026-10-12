import unittest

from videomixer.render import build_filter_graph, build_inputs, input_layout
from videomixer.config import Style

try:
    from . import helpers
except ImportError:
    import helpers


class TestInputLayout(unittest.TestCase):
    def test_voiced_single_sfx(self):
        design = helpers.make_design(voiced=True,
                                     sfx=[helpers.make_cue()])
        self.assertEqual(input_layout(design),
                         {"video": 0, "bgm0": 1, "sfx0": 2})

    def test_silent_two_sfx_gets_silence_slot(self):
        design = helpers.make_design(voiced=False,
                                     sfx=[helpers.make_cue(), helpers.make_cue(1.0)])
        self.assertEqual(input_layout(design),
                         {"video": 0, "bgm0": 1, "sfx0": 2, "sfx1": 3,
                          "silence": 4})


class TestGraphCompilation(unittest.TestCase):
    def graph(self, **kwargs) -> str:
        return build_filter_graph(helpers.make_design(**kwargs))

    def test_plain_voiced_no_duck(self):
        style = Style(name="balanced", loudness_lufs=-19.0)
        g = self.graph(style=style, stats=None)
        self.assertIn("[0:a]aformat", g)
        self.assertNotIn("asplit", g)
        self.assertIn("anull[music]", g)
        self.assertNotIn("sidechaincompress", g)
        self.assertIn("[voice][music]amix=inputs=2:normalize=0", g)
        self.assertIn("loudnorm=I=-19:TP=-1.5:LRA=11,", g)
        self.assertNotIn("linear=true", g)

    def test_duck_and_sfx_chain(self):
        style = Style(name="impact", loudness_lufs=-17.0, duck=True)
        design = helpers.make_design(style=style,
                                     sfx=[helpers.make_cue(at=0.25)])
        g = build_filter_graph(design)
        self.assertIn("asplit=2[voice][duckkey]", g)
        self.assertIn("sidechaincompress=", g)
        self.assertIn("adelay=delays=250:all=1", g)
        self.assertIn("apad,atrim", g)
        self.assertIn("[bgm][sfx0]amix=inputs=2:normalize=0:duration=longest[musicpre]", g)

    def test_silent_video_voice_comes_from_anullsrc_input(self):
        design = helpers.make_design(voiced=False,
                                     sfx=[helpers.make_cue()])
        g = build_filter_graph(design)
        self.assertIn("[3:a]aformat", g)
        self.assertNotIn("asplit", g)
        self.assertNotIn("sidechaincompress", g)
        inputs = build_inputs(design)
        self.assertIn("-f", inputs)
        self.assertIn("lavfi", inputs)
        self.assertIn("anullsrc=r=48000:cl=stereo", inputs)

    def test_linear_two_pass_params_embedded(self):
        g = self.graph()
        self.assertIn("measured_I=-22.25", g)
        self.assertIn("measured_TP=-2.00", g)
        self.assertIn("measured_thresh=-32.00", g)
        self.assertIn("offset=0.05", g)
        self.assertIn("linear=true", g)

    def test_no_loudnorm_when_style_has_no_target(self):
        style = Style(name="raw", loudness_lufs=None)
        g = self.graph(style=style)
        self.assertNotIn("loudnorm", g)

    def test_fades_clamped_for_short_videos(self):
        style = Style(name="x", fade_in_s=10.0, fade_out_s=10.0,
                      loudness_lufs=None)
        g = self.graph(video_dur=6.0, style=style)
        self.assertIn("afade=t=in:st=0:d=2.000", g)
        self.assertIn("afade=t=out:st=4.000:d=2.000", g)

    def test_limiter_only_when_enabled(self):
        self.assertIn("alimiter", self.graph())
        style = Style(name="x", limiter=False, loudness_lufs=None)
        self.assertNotIn("alimiter", self.graph(style=style))


class TestBuildInputs(unittest.TestCase):
    def test_loop_engaged_for_short_track(self):
        design = helpers.make_design(video_dur=30.0, track_dur=15.0)
        self.assertIn("-stream_loop", build_inputs(design))
        self.assertIn("-1", build_inputs(design))

    def test_no_loop_for_longer_track(self):
        design = helpers.make_design(video_dur=30.0, track_dur=60.0)
        self.assertNotIn("-stream_loop", build_inputs(design))

    def test_loop_disabled_by_style(self):
        style = Style(name="x", loop_bgm=False)
        design = helpers.make_design(video_dur=30.0, track_dur=15.0,
                                     style=style)
        self.assertNotIn("-stream_loop", build_inputs(design))

    def test_sfx_files_are_plain_inputs(self):
        design = helpers.make_design(sfx=[helpers.make_cue()])
        inputs = [str(x) for x in build_inputs(design)]
        self.assertIn("hit.mp3", " ".join(inputs))


class TestFingerprint(unittest.TestCase):
    def test_seed_excluded_from_fingerprint(self):
        a = helpers.make_design()
        b = helpers.make_design()
        b.seed = 999
        self.assertEqual(a.fingerprint(), b.fingerprint())

    def test_different_gain_different_fingerprint(self):
        a = helpers.make_design()
        b = helpers.make_design(style=helpers.make_style(bgm_gain_db=-20.0))
        self.assertNotEqual(a.fingerprint(), b.fingerprint())

    def test_fingerprint_is_short_hex(self):
        fp = helpers.make_design().fingerprint()
        self.assertEqual(len(fp), 16)
        int(fp, 16)


if __name__ == "__main__":
    unittest.main()
