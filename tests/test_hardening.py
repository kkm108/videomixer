"""Regression tests for the production-hardening pass (v2.7.0)."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from videomixer import cli
from videomixer.config import Recipe, Style, load_recipe, validate_recipe
from videomixer.design import AudioDesign, Movement, plan_boundaries
from videomixer.library import (
    MAX_WATCH_ATTEMPTS,
    UsageLedger,
    WatchState,
)
from videomixer.locks import LockTimeout, workspace_lock
from videomixer.probe import MediaInfo, ProbeCache

try:
    from . import helpers as H
except ImportError:
    import helpers as H


def write_recipe(text: str) -> Path:
    tmp = tempfile.mkdtemp()
    path = Path(tmp) / "recipe.toml"
    path.write_text(text, encoding="utf-8")
    return path


class TestRecipeValidation(unittest.TestCase):
    def load(self, text: str):
        return load_recipe(write_recipe(text))

    def test_versions_must_be_positive(self):
        with self.assertRaisesRegex(ValueError, "versions_per_video"):
            self.load("[run]\nversions_per_video = 0\n[style.a]\n")

    def test_negative_tolerance_rejected(self):
        with self.assertRaisesRegex(ValueError, "tolerance"):
            self.load("[run]\ntolerance = -1\n[style.a]\n")

    def test_negative_movement_rejected(self):
        with self.assertRaisesRegex(ValueError, "movement"):
            self.load("[style.a]\nmovement = -5\n")

    def test_zero_crossfade_rejected(self):
        with self.assertRaisesRegex(ValueError, "crossfade"):
            self.load("[style.a]\ncrossfade = 0\n")

    def test_loudness_out_of_range_rejected(self):
        with self.assertRaisesRegex(ValueError, "loudness_lufs"):
            self.load("[style.a]\nloudness_lufs = -3\n")

    def test_empty_bitrate_rejected(self):
        with self.assertRaisesRegex(ValueError, "audio_bitrate"):
            self.load('[run]\naudio_bitrate = ""\n[style.a]\n')

    def test_negative_fade_rejected(self):
        with self.assertRaisesRegex(ValueError, "fade_in"):
            self.load("[style.a]\nfade_in = -1\n")

    def test_valid_recipe_passes(self):
        recipe = self.load("[run]\nversions_per_video = 1\n"
                           "[style.a]\nmovement = 30\ncrossfade = 2\n")
        self.assertEqual(recipe.versions_per_video, 1)

    def test_validate_recipe_direct(self):
        with self.assertRaises(ValueError):
            validate_recipe(Recipe(versions_per_video=0,
                                   styles=(Style(name="x"),)))


class TestBoundaryPathologies(unittest.TestCase):
    def test_tiny_movement_does_not_collapse(self):
        bounds = plan_boundaries(100.0, 1.0, [], 1.0)
        self.assertEqual(len(bounds), len(set(bounds)))
        self.assertEqual(bounds, sorted(bounds))
        self.assertTrue(all(b > 0 for b in bounds))
        self.assertTrue(all(b < 100.0 for b in bounds))

    def test_movement_below_half_second_still_ordered(self):
        bounds = plan_boundaries(50.0, 0.1, [], 0.2)
        self.assertEqual(bounds, sorted(bounds))
        self.assertEqual(len(bounds), len(set(bounds)))
        gaps_ok = all(b - a >= 0.5 for a, b in zip(bounds, bounds[1:]))
        self.assertTrue(gaps_ok)

    def test_two_boundaries_snapping_to_one_gap_stay_ordered(self):
        bounds = plan_boundaries(30.0, 3.0, [(14.0, 15.0)], 2.0)
        self.assertEqual(bounds, sorted(bounds))
        self.assertEqual(len(bounds), len(set(bounds)))

    def test_zero_movement_returns_empty(self):
        self.assertEqual(plan_boundaries(60.0, 0.0, [], 1.0), [])


class TestWatchRetries(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.video = self.root / "clip.mp4"
        self.video.write_bytes(b"x" * 10)
        self.state_path = self.root / "state.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_failed_file_retried_then_capped(self):
        state = WatchState.load(self.state_path)
        state.mark_failed(self.video)
        for _ in range(MAX_WATCH_ATTEMPTS - 1):
            self.assertEqual(state.pending([self.video]), [self.video])
            state.mark_failed(self.video)
        self.assertEqual(state.pending([self.video]), [])

    def test_success_clears_retry_state(self):
        state = WatchState.load(self.state_path)
        state.mark_failed(self.video)
        state.mark_success(self.video)
        self.assertEqual(state.pending([self.video]), [])

    def test_changed_file_resets_attempts(self):
        state = WatchState.load(self.state_path)
        for _ in range(MAX_WATCH_ATTEMPTS):
            state.mark_failed(self.video)
        self.assertEqual(state.pending([self.video]), [])

        self.video.write_bytes(b"y" * 30)
        self.assertEqual(state.pending([self.video]), [self.video])

    def test_legacy_entry_without_status_treated_as_done(self):
        sig = WatchState._signature(self.video)
        state = WatchState(self.state_path,
                           seen={self.video.resolve().as_posix(): sig})
        self.assertEqual(state.pending([self.video]), [])

    def test_state_round_trip_preserves_status(self):
        state = WatchState.load(self.state_path)
        state.mark_failed(self.video)
        state.save()
        reloaded = WatchState.load(self.state_path)
        entry = reloaded.seen[self.video.resolve().as_posix()]
        self.assertEqual(entry["status"], "failed")
        self.assertEqual(entry["attempts"], 1)


class TestWorkspaceLock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lock = Path(self.tmp.name) / "x.lock"

    def tearDown(self):
        self.tmp.cleanup()

    def test_acquire_and_release(self):
        with workspace_lock(self.lock):
            self.assertTrue(self.lock.exists())
        self.assertFalse(self.lock.exists())

    def test_contention_raises_timeout(self):
        os.mkdir(self.lock)
        try:
            with self.assertRaises(LockTimeout):
                with workspace_lock(self.lock, timeout_s=0.2, stale_s=60):
                    pass
        finally:
            os.rmdir(self.lock)

    def test_stale_lock_is_stolen(self):
        os.mkdir(self.lock)
        old = self.lock.stat().st_mtime - 3600
        os.utime(self.lock, (old, old))
        with workspace_lock(self.lock, timeout_s=1, stale_s=60):
            self.assertTrue(self.lock.exists())


class TestConcurrentMerges(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_usage_ledger_merges_two_writers(self):
        path = self.root / "usage.json"
        writer_a = UsageLedger.load(path)
        writer_b = UsageLedger.load(path)
        writer_a.record(["track_x"])
        writer_b.record(["track_y"])
        reloaded = UsageLedger.load(path)
        self.assertEqual(reloaded.count("track_x"), 1)
        self.assertEqual(reloaded.count("track_y"), 1)

    def test_probe_cache_merges_two_writers(self):
        path = self.root / "probe_cache.json"
        file_a = self.root / "a.mp3"
        file_b = self.root / "b.mp3"
        file_a.write_bytes(b"a")
        file_b.write_bytes(b"b")

        cache_a = ProbeCache(path)
        cache_b = ProbeCache(path)
        cache_a.put(MediaInfo(path=file_a, duration=10.0, has_audio=True))
        cache_a.save()
        cache_b.put(MediaInfo(path=file_b, duration=20.0, has_audio=True))
        cache_b.save()

        reloaded = ProbeCache(path)
        self.assertIsNotNone(reloaded.get(file_a))
        self.assertIsNotNone(reloaded.get(file_b))

    def test_cache_reset_removes_disk_file(self):
        path = self.root / "probe_cache.json"
        media = self.root / "a.mp3"
        media.write_bytes(b"a")
        cache = ProbeCache(path)
        cache.put(MediaInfo(path=media, duration=1.0, has_audio=True))
        cache.save()
        cache.reset()
        self.assertFalse(path.exists())
        self.assertIsNone(cache.get(media))


class TestCLIHardening(unittest.TestCase):
    def test_require_tools_reports_missing(self):
        with mock.patch("videomixer.cli.shutil.which",
                        side_effect=lambda name: None):
            buf = StringIO()
            with redirect_stderr(buf):
                rc = cli.require_tools()
            self.assertEqual(rc, 2)
            self.assertIn("ffmpeg", buf.getvalue())

    def test_require_tools_passes_when_present(self):
        with mock.patch("videomixer.cli.shutil.which",
                        side_effect=lambda name: f"/usr/bin/{name}"):
            self.assertIsNone(cli.require_tools())

    def test_bad_recipe_path_is_reported_not_traced(self):
        buf = StringIO()
        with redirect_stderr(buf):
            rc = cli.main(["run", "--root", tempfile.mkdtemp(),
                           "--recipe", "does-not-exist.toml"])
        self.assertEqual(rc, 1)
        self.assertIn("recipe not found", buf.getvalue())

    def test_invalid_recipe_values_reported(self):
        path = write_recipe("[style.a]\nmovement = -1\n")
        buf = StringIO()
        with redirect_stderr(buf):
            rc = cli.main(["run", "--root", tempfile.mkdtemp(),
                           "--recipe", str(path)])
        self.assertEqual(rc, 1)
        self.assertIn("invalid recipe", buf.getvalue())

    def test_list_videos_warns_on_undecodable_input(self):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        (root / "inputs").mkdir()
        (root / "inputs" / "junk.mp4").write_bytes(b"not a video")
        cache = ProbeCache(root / "cache.json")
        buf = StringIO()
        with redirect_stderr(buf):
            videos = cli.list_videos(root, cache)
        tmp.cleanup()
        self.assertEqual(videos, [])
        self.assertIn("cannot decode", buf.getvalue())

    def test_execute_batch_isolates_exceptions_per_item(self):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        (root / "outputs").mkdir()
        design = H.make_design()
        with mock.patch("videomixer.cli.render_command",
                        side_effect=OSError("boom")):
            buf = StringIO()
            with redirect_stderr(buf):
                failures = cli.execute_batch(
                    root, [design], UsageLedger(root / "usage.json"),
                    [], mock.Mock(), "r.toml", jobs=1, seed=1)
        tmp.cleanup()
        self.assertEqual(failures, 1)
        self.assertIn("OSError: boom", buf.getvalue())


class TestNormalizationMode(unittest.TestCase):
    def test_mixed_when_only_some_movements_measured(self):
        design = H.make_design(stats=None)
        design.style = Style(name="s", loudness_lufs=-19.0)
        beds = [H.make_media("library/a.mp3", 9.0),
                H.make_media("library/b.mp3", 9.0)]
        design.movements = [
            Movement(track=beds[0], start_s=0.0, end_s=15.0,
                     loudness=H.STATS),
            Movement(track=beds[1], start_s=15.0, end_s=30.0, loudness=None),
        ]
        self.assertEqual(design.normalization_mode, "mixed")

    def test_linear_when_all_measured(self):
        design = H.make_design(stats=None)
        design.style = Style(name="s", loudness_lufs=-19.0)
        beds = [H.make_media("library/a.mp3", 9.0),
                H.make_media("library/b.mp3", 9.0)]
        design.movements = [
            Movement(track=beds[0], start_s=0.0, end_s=15.0,
                     loudness=H.STATS),
            Movement(track=beds[1], start_s=15.0, end_s=30.0,
                     loudness=H.STATS),
        ]
        self.assertEqual(design.normalization_mode, "linear")


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not available")
class TestEmptyInputsMessage(unittest.TestCase):
    def test_run_with_no_inputs_exits_cleanly(self):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        for d in ("inputs", "library", "sfx"):
            (root / d).mkdir()
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "sine=frequency=220:duration=3",
             "-c:a", "libmp3lame", str(root / "library" / "bed.mp3")],
            check=True, capture_output=True)
        recipe = write_recipe("[run]\nversions_per_video = 1\n"
                              "[style.flat]\nbgm_gain_db = -12\n")
        buf = StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["run", "--root", str(root),
                           "--recipe", str(recipe)])
        tmp.cleanup()
        self.assertEqual(rc, 0)
        self.assertIn("No input videos", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
