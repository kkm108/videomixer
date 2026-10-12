import json
import os
import tempfile
import unittest
from pathlib import Path

from videomixer.probe import (
    CACHE_VERSION,
    LoudnessStats,
    MediaInfo,
    ProbeCache,
)


class TestProbeCacheRoundTrip(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.media = self.root / "bgm.mp3"
        self.media.write_bytes(b"x")
        self.cache_file = self.root / "metadata" / "probe_cache.json"

    def tearDown(self):
        self.tmp.cleanup()

    def put_media(self, cache: ProbeCache):
        info = MediaInfo(path=self.media, duration=42.0, has_audio=True)
        cache.put(info)
        return info

    def test_put_get_save_load(self):
        cache = ProbeCache(self.cache_file)
        info = self.put_media(cache)
        cache.save()

        reloaded = ProbeCache(self.cache_file)
        got = reloaded.get(self.media)
        self.assertIsNotNone(got)
        self.assertEqual(got.duration, 42.0)
        self.assertTrue(got.has_audio)
        self.assertEqual(got.key, info.key)

    def test_mtime_invalidation(self):
        cache = ProbeCache(self.cache_file)
        self.put_media(cache)
        cache.save()

        stat = self.media.stat()
        os.utime(self.media, (stat.st_atime + 50, stat.st_mtime + 50))

        self.assertIsNone(ProbeCache(self.cache_file).get(self.media))

    def test_size_invalidation(self):
        cache = ProbeCache(self.cache_file)
        self.put_media(cache)
        cache.save()
        self.media.write_bytes(b"longer content")

        self.assertIsNone(ProbeCache(self.cache_file).get(self.media))

    def test_missing_cache_file_is_empty(self):
        cache = ProbeCache(self.root / "does_not_exist.json")
        self.assertIsNone(cache.get(self.media))

    def test_corrupt_cache_file_is_empty(self):
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        self.cache_file.write_text("{not json", encoding="utf-8")
        cache = ProbeCache(self.cache_file)
        self.assertEqual(cache._entries, {})


class TestLoudnessCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.media = self.root / "t.mp3"
        self.media.write_bytes(b"x")
        self.stats = LoudnessStats(input_i=-22.25, input_tp=-2.0,
                                   input_lra=3.0, input_thresh=-32.0,
                                   target_offset=0.05)

    def tearDown(self):
        self.tmp.cleanup()

    def test_loudness_round_trip(self):
        cache = ProbeCache(self.root / "c.json")
        cache.put_loudness(self.media, self.stats)
        cache.save()

        got = ProbeCache(self.root / "c.json").get_loudness(self.media)
        self.assertEqual(got.to_dict(), self.stats.to_dict())

    def test_get_loudness_without_entry_returns_none(self):
        cache = ProbeCache(self.root / "c.json")
        self.assertIsNone(cache.get_loudness(self.media))

    def test_v2_cache_migrates_and_gains_loudness(self):
        cache_file = self.root / "c.json"
        key = self.media.resolve().as_posix()
        cache_file.write_text(json.dumps({
            "version": 2,
            "entries": {key: {"mtime": self.media.stat().st_mtime,
                              "size": self.media.stat().st_size,
                              "duration": 12.0, "has_audio": True}},
        }), encoding="utf-8")

        cache = ProbeCache(cache_file)
        self.assertIsNotNone(cache.get(self.media))
        cache.put_loudness(self.media, self.stats)
        cache.save()

        doc = json.loads(cache_file.read_text(encoding="utf-8"))
        self.assertEqual(doc["version"], CACHE_VERSION)
        self.assertIn("loudness", doc["entries"][key])


if __name__ == "__main__":
    unittest.main()
