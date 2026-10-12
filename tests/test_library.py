import tempfile
import os
import random
import unittest
from pathlib import Path

from videomixer.library import Library, UsageLedger, WatchState, select_tracks

try:
    from . import helpers
except ImportError:
    import helpers


def make_tracks(*durations):
    return [helpers.make_media(f"library/t{i}.mp3", d)
            for i, d in enumerate(durations)]


class TestSelectTracks(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ledger_path = Path(self._tmp.name) / "usage.json"

    def tearDown(self):
        self._tmp.cleanup()

    def _ledger(self) -> UsageLedger:
        return UsageLedger(self.ledger_path)

    def test_exact_match_wins(self):
        tracks = make_tracks(10, 15, 30, 45, 60)
        rng = random.Random(0)
        picked = select_tracks(tracks, 30.0, 1, self._ledger(), rng, 1.5)
        self.assertEqual(picked[0].duration, 30.0)

    def test_within_tolerance_treated_as_exact(self):
        tracks = make_tracks(29.4, 45)
        rng = random.Random(0)
        picked = select_tracks(tracks, 30.0, 1, self._ledger(), rng, 1.5)
        self.assertEqual(picked[0].duration, 29.4)

    def test_no_exact_match_prefers_closest(self):
        tracks = make_tracks(10, 31, 60)
        rng = random.Random(0)
        picked = select_tracks(tracks, 30.0, 2, self._ledger(), rng, 1.5)
        self.assertEqual([t.duration for t in picked], [31, 10])

    def test_usage_penalty_rotates_tracks(self):
        tracks = make_tracks(30, 31)
        rng = random.Random(0)

        first = select_tracks(tracks, 30.0, 1, self._ledger(), rng, 1.5)[0]
        ledger = self._ledger()
        ledger.record([first.key])

        second = select_tracks(tracks, 30.0, 1, ledger, rng, 1.5)[0]
        self.assertNotEqual(first.key, second.key)

    def test_heavily_used_track_only_returned_when_forced(self):
        tracks = make_tracks(30, 31)
        ledger = self._ledger()
        ledger.record([tracks[1].key] * 5)
        rng = random.Random(0)
        picked = select_tracks(tracks, 30.0, 1, ledger, rng, 1.5)
        self.assertEqual(picked[0].key, tracks[0].key)

    def test_count_exceeding_library_returns_all_distinct(self):
        tracks = make_tracks(10, 20, 30)
        rng = random.Random(0)
        picked = select_tracks(tracks, 25.0, 10, self._ledger(), rng, 1.5)
        self.assertEqual(len(picked), 3)
        self.assertEqual(len({t.key for t in picked}), 3)

    def test_same_seed_same_selection(self):
        tracks = make_tracks(10, 21, 22, 35, 44)
        a = select_tracks(tracks, 30.0, 3, self._ledger(),
                          random.Random(99), 1.5)
        b = select_tracks(tracks, 30.0, 3, self._ledger(),
                          random.Random(99), 1.5)
        self.assertEqual([t.key for t in a], [t.key for t in b])


class TestUsageLedger(unittest.TestCase):
    def test_missing_file_loads_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = UsageLedger.load(Path(tmp) / "usage.json")
            self.assertEqual(ledger.uses, {})

    def test_record_persists_and_reloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "usage.json"
            ledger = UsageLedger.load(path)
            ledger.record(["a", "a", "b"])
            reloaded = UsageLedger.load(path)
            self.assertEqual(reloaded.count("a"), 2)
            self.assertEqual(reloaded.count("b"), 1)


class TestLibraryScan(unittest.TestCase):
    def test_scan_finds_audio_and_ignores_others(self):
        from videomixer.probe import ProbeCache

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.mp3").write_bytes(b"")
            (root / "nested").mkdir()
            (root / "nested" / "b.wav").write_bytes(b"")
            (root / "note.txt").write_text("nope")

            library = Library(root, ProbeCache(root / "cache.json"))

            class FakeProbe:
                calls = 0

                def __call__(self, f, cache):
                    FakeProbe.calls += 1
                    return helpers.make_media(f.name, float(FakeProbe.calls))

            import videomixer.library as lib_mod
            original = lib_mod.probe_with_cache
            lib_mod.probe_with_cache = FakeProbe()
            try:
                found = library.scan()
            finally:
                lib_mod.probe_with_cache = original

            self.assertEqual({t.path.name for t in found}, {"a.mp3", "b.wav"})

    def test_sfx_pool_filters_by_pattern(self):
        from videomixer.probe import ProbeCache

        with tempfile.TemporaryDirectory() as tmp:
            sfx_dir = Path(tmp)
            for name in ("impact_1.mp3", "whoosh_a.mp3"):
                (sfx_dir / name).write_bytes(b"")

            library = Library(sfx_dir, ProbeCache(sfx_dir / "c.json"))

            class FakeProbe:
                def __call__(self, f, cache):
                    return helpers.make_media(f.name, 0.5)

            import videomixer.library as lib_mod
            original = lib_mod.probe_with_cache
            lib_mod.probe_with_cache = FakeProbe()
            try:
                pool = library.sfx_pool(sfx_dir, "impact_*")
            finally:
                lib_mod.probe_with_cache = original

            self.assertEqual([p.path.name for p in pool], ["impact_1.mp3"])


class TestWatchState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state_file = self.root / "watch_state.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, name: str, size: int) -> Path:
        p = self.root / name
        p.write_bytes(b"x" * size)
        return p

    def test_new_files_are_pending_then_marked(self):
        video = self._write("clip.mp4", 10)
        state = WatchState(self.state_file)
        self.assertEqual(state.pending([video]), [video])
        state.mark(video)
        self.assertEqual(state.pending([video]), [])

    def test_modified_file_becomes_pending_again(self):
        video = self._write("clip.mp4", 10)
        state = WatchState(self.state_file)
        state.mark(video)

        video.write_bytes(b"y" * 25)
        self.assertEqual(state.pending([video]), [video])

    def test_touch_without_change_not_pending(self):
        import os as _os

        video = self._write("clip.mp4", 10)
        state = WatchState(self.state_file)
        state.mark(video)

        stat = video.stat()
        _os.utime(video, (stat.st_atime + 100, stat.st_mtime))
        self.assertEqual(state.pending([video]), [])

    def test_save_load_round_trip(self):
        video = self._write("clip.mp4", 10)
        state = WatchState(self.state_file)
        state.mark(video)
        state.save()

        reloaded = WatchState.load(self.state_file)
        self.assertEqual(reloaded.pending([video]), [])

    def test_missing_file_is_skipped_safely(self):
        ghost = self.root / "ghost.mp4"
        state = WatchState(self.state_file)
        self.assertEqual(state.pending([ghost]), [])
        state.mark(ghost)


if __name__ == "__main__":
    unittest.main()
