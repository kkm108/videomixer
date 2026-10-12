"""Onset detection + SFX snapping tests."""

import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from shutil import which

from videomixer.design import PlacedCue, resolve_cue_time, snap_time
from videomixer.config import load_style


def _write_click_wav(path: Path,
                     bursts: list[float],
                     duration: float = 4.0,
                     rate: int = 11025) -> None:
    import struct

    total = int(duration * rate)
    frames = bytearray()
    for i in range(total):
        t = i / rate
        in_burst = any(b <= t < b + 0.06 for b in bursts)
        value = int(0.9 * 32767) if in_burst else 0
        frames += struct.pack("<h", value)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(bytes(frames))


@unittest.skipUnless(which("ffmpeg"), "ffmpeg not available")
class TestOnsetDetection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_clicks_found_at_burst_positions(self):
        wav = self.root / "clicks.wav"
        _write_click_wav(wav, bursts=[1.0, 2.0, 3.0])
        from videomixer.probe import analyze_onsets

        onsets = analyze_onsets(wav)
        self.assertIsNotNone(onsets)
        self.assertEqual(len(onsets), 3)
        for expected, got in zip([1.0, 2.0, 3.0], onsets):
            self.assertLess(abs(expected - got), 0.1)

    def test_flat_tone_has_no_onsets(self):
        tone = self.root / "tone.mp3"
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "sine=frequency=220:duration=3",
             "-c:a", "libmp3lame", str(tone)],
            check=True, capture_output=True)
        from videomixer.probe import analyze_onsets

        self.assertEqual(analyze_onsets(tone), [])

    def test_undecodable_file_returns_none(self):
        junk = self.root / "junk.mp3"
        junk.write_bytes(b"not audio at all")
        from videomixer.probe import analyze_onsets

        self.assertIsNone(analyze_onsets(junk))


class TestSnapTime(unittest.TestCase):
    ONSETS = [1.02, 2.01]

    def test_snaps_to_nearest_wrapped_onset(self):
        result = snap_time(8.55, self.ONSETS, bed_len=4.0, video_len=30.0,
                           loop_bed=True, window_s=0.6)
        self.assertEqual(result, 9.02)

    def test_outside_window_returns_none(self):
        result = snap_time(8.55, self.ONSETS, bed_len=4.0, video_len=30.0,
                           loop_bed=True, window_s=0.2)
        self.assertIsNone(result)

    def test_no_loop_no_wrap(self):
        result = snap_time(8.55, [9.4], bed_len=60.0, video_len=30.0,
                           loop_bed=False, window_s=1.0)
        self.assertEqual(result, 9.4)

    def test_clamps_to_video_end(self):
        result = snap_time(29.98, [29.97], bed_len=60.0, video_len=30.0,
                           loop_bed=False, window_s=1.0)
        self.assertEqual(result, 29.95)

    def test_empty_onsets_returns_none(self):
        self.assertIsNone(snap_time(5.0, [], bed_len=10.0, video_len=30.0,
                                    loop_bed=True, window_s=1.0))

    def test_zero_bed_len_returns_none(self):
        self.assertIsNone(snap_time(5.0, [1.0], bed_len=0.0, video_len=30.0,
                                    loop_bed=True, window_s=1.0))

    def test_exact_hit_preferred_over_farther(self):
        result = snap_time(2.01, self.ONSETS, bed_len=100.0, video_len=30.0,
                           loop_bed=False, window_s=0.5)
        self.assertEqual(result, 2.01)


class TestResolveCueTime(unittest.TestCase):
    def test_negative_counts_from_end(self):
        self.assertEqual(resolve_cue_time(-1.2, 10.0), 8.8)

    def test_negative_beyond_start_clamps_to_zero(self):
        self.assertEqual(resolve_cue_time(-50.0, 10.0), 0.0)

    def test_positive_clamps_inside_video(self):
        self.assertEqual(resolve_cue_time(99.0, 10.0), 9.9)


class TestPlacedCueProvenance(unittest.TestCase):
    def test_snapped_flag_and_manifest_dict(self):
        cue = PlacedCue(path=Path("x/hit.mp3"), at_s=1.02, gain_db=-4.0,
                        requested_at_s=0.55)
        self.assertTrue(cue.snapped)
        doc = cue.to_dict()
        self.assertEqual(doc["requested_at_s"], 0.55)
        self.assertTrue(doc["snapped"])

    def test_unsnapped_cue_has_no_requested_field(self):
        cue = PlacedCue(path=Path("x/hit.mp3"), at_s=0.0, gain_db=-4.0)
        self.assertFalse(cue.snapped)
        self.assertNotIn("requested_at_s", cue.to_dict())


class TestSnapConfigParsing(unittest.TestCase):
    def test_defaults(self):
        style = load_style("x", {"sfx": [{"pattern": "*"}]})
        cue = style.sfx_cues[0]
        self.assertFalse(cue.snap)
        self.assertAlmostEqual(cue.snap_window_s, 0.5)

    def test_explicit_snap_values(self):
        style = load_style("x", {"sfx": [
            {"pattern": "*", "snap": True, "snap_window": 0.8}]})
        cue = style.sfx_cues[0]
        self.assertTrue(cue.snap)
        self.assertAlmostEqual(cue.snap_window_s, 0.8)


if __name__ == "__main__":
    unittest.main()
