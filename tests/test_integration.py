"""End-to-end integration test. Requires ffmpeg; skipped otherwise."""

import json
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from videomixer import __version__
from videomixer.cli import main

RECIPE = """\
[run]
versions_per_video = 1
tolerance = 2.0
seed = 11

[style.flat]
bgm_gain_db = -12.0
fade_in = 0.1
fade_out = 0.3
"""


def ffmpeg_ok(cmd) -> bool:
    return subprocess.run(cmd, capture_output=True).returncode == 0


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not available")
class TestEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.ws = Path(cls.tmp.name)
        cls.recipe = cls.ws / "recipe.toml"
        cls.recipe.write_text(RECIPE, encoding="utf-8")
        for d in ("inputs", "library", "sfx"):
            (cls.ws / d).mkdir()
        cls._run(["-f", "lavfi", "-i", "testsrc2=duration=3:size=320x240:rate=15",
                  "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                  "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                  "-c:a", "aac", str(cls.ws / "inputs" / "clip.mp4")])
        cls._run(["-f", "lavfi", "-i", "sine=frequency=220:duration=4",
                  "-c:a", "libmp3lame", str(cls.ws / "library" / "bed.mp3")])

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def _run(cls, args):
        return subprocess.run(["ffmpeg", "-y", "-hide_banner",
                               "-loglevel", "error", *args],
                              capture_output=True)

    def _cli(self, *extra) -> str:
        buf = StringIO()
        with redirect_stdout(buf):
            rc = main(["run", "--root", str(self.ws),
                       "--recipe", str(self.recipe), *extra])
        self.assertEqual(rc, 0)
        return buf.getvalue()

    def _outputs(self):
        return sorted((self.ws / "outputs").glob("*.mp4"))

    def test_a_first_run_renders_and_manifests(self):
        out = self._cli("--seed", "11")
        self.assertIn("ok ", out)
        videos = self._outputs()
        self.assertEqual(len(videos), 1)

        manifest = videos[0].with_suffix("")
        manifest = manifest.with_suffix(manifest.suffix + ".mix.json")
        doc = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(doc["mixer_version"], __version__)
        self.assertIn("fingerprint", doc)
        self.assertEqual(doc["design"]["style"]["name"], "flat")

        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_streams", str(videos[0])],
            capture_output=True, text=True, check=True)
        streams = json.loads(probe.stdout)["streams"]
        codecs = {s["codec_type"]: s["codec_name"] for s in streams}
        self.assertEqual(codecs["video"], "h264")
        self.assertEqual(codecs["audio"], "aac")

    def test_b_rerun_same_seed_skips_everything(self):
        before = self._outputs()
        out = self._cli("--seed", "11")
        self.assertIn("skip ", out)
        self.assertIn("identical design", out)
        self.assertEqual(self._outputs(), before)
        self.assertNotIn("ok ", out)

    def test_c_different_seed_same_design_also_skips(self):
        before = self._outputs()
        out = self._cli("--seed", "777")
        self.assertIn("skip ", out)
        self.assertEqual(self._outputs(), before)

    def test_d_changed_design_re_renders(self):
        self.recipe.write_text(RECIPE.replace("bgm_gain_db = -12.0",
                                              "bgm_gain_db = -9.0"),
                               encoding="utf-8")
        out = self._cli("--seed", "11")
        self.assertIn("ok ", out)
        self.assertNotIn("skip ", out)
        self.assertEqual(len(self._outputs()), 2)


SNAP_RECIPE = """\
[run]
versions_per_video = 1

[style.snappy]
bgm_gain_db = -12.0
fade_in = 0.05
fade_out = 0.3

[[style.snappy.sfx]]
pattern = "*"
at = 0.55
gain_db = -6.0
snap = true
snap_window = 0.6
"""


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not available")
class TestSnapToEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.ws = Path(cls.tmp.name)
        cls.recipe = cls.ws / "recipe.toml"
        cls.recipe.write_text(SNAP_RECIPE, encoding="utf-8")
        for d in ("inputs", "library", "sfx"):
            (cls.ws / d).mkdir()
        cls._ff(["-f", "lavfi", "-i", "testsrc2=duration=3:size=320x240:rate=15",
                 "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                 "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                 "-c:a", "aac", str(cls.ws / "inputs" / "clip.mp4")])
        cls._ff(["-f", "lavfi",
                 "-i", "aevalsrc=if(lt(mod(t\\,1)\\,0.08)\\,sin(2*PI*t*440)*0.9\\,0)"
                       ":d=4:s=44100",
                 "-c:a", "libmp3lame", str(cls.ws / "library" / "clicks.mp3")])
        cls._ff(["-f", "lavfi", "-i", "sine=frequency=800:duration=0.5",
                 "-af", "afade=t=out:st=0.2:d=0.3",
                 "-c:a", "libmp3lame", str(cls.ws / "sfx" / "hit.mp3")])

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def _ff(cls, args):
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                        *args], capture_output=True, check=True)

    def _cli(self, *extra) -> str:
        buf = StringIO()
        with redirect_stdout(buf):
            rc = main(["run", "--root", str(self.ws),
                       "--recipe", str(self.recipe), *extra])
        self.assertEqual(rc, 0)
        return buf.getvalue()

    def test_a_cue_snaps_onto_transient_and_records_provenance(self):
        out = self._cli("--seed", "3")
        self.assertIn("onsets:", out)
        manifest = next((self.ws / "outputs").glob("*.mix.json"))
        doc = json.loads(manifest.read_text(encoding="utf-8"))
        cue = doc["design"]["sfx_cues"][0]
        self.assertEqual(cue["requested_at_s"], 0.55)
        self.assertTrue(cue["snapped"])
        self.assertGreater(cue["at_s"], 0.7)
        self.assertLess(cue["at_s"], 1.35)

    def test_b_rerun_uses_cached_onsets_and_skips(self):
        before = sorted(p.name for p in (self.ws / "outputs").glob("*.mp4"))
        out = self._cli("--seed", "3")
        self.assertNotIn("onsets:", out)
        self.assertIn("skip ", out)
        after = sorted(p.name for p in (self.ws / "outputs").glob("*.mp4"))
        self.assertEqual(before, after)


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not available")
class TestParallelAndWatch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.ws = Path(cls.tmp.name)
        cls.recipe = cls.ws / "recipe.toml"
        cls.recipe.write_text(
            "[run]\nversions_per_video = 1\n\n[style.flat]\nbgm_gain_db = -12.0\n",
            encoding="utf-8")
        for d in ("inputs", "library", "sfx"):
            (cls.ws / d).mkdir()
        cls._ff(["-f", "lavfi", "-i", "sine=frequency=220:duration=4",
                 "-c:a", "libmp3lame", str(cls.ws / "library" / "bed.mp3")])

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def _ff(cls, args):
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                        *args], capture_output=True, check=True)

    def _clip(self, name: str, duration: int) -> Path:
        path = self.ws / "inputs" / name
        self._ff(["-f", "lavfi", "-i",
                  f"testsrc2=duration={duration}:size=320x240:rate=15",
                  "-c:v", "libx264", "-preset", "ultrafast",
                  "-pix_fmt", "yuv420p", str(path)])
        return path

    def _cli(self, *extra) -> str:
        buf = StringIO()
        with redirect_stdout(buf):
            rc = main(["run", "--root", str(self.ws),
                       "--recipe", str(self.recipe), *extra])
        self.assertEqual(rc, 0)
        return buf.getvalue()

    def _tick(self) -> int:
        from videomixer.cli import _tick
        from videomixer.config import load_recipe
        from videomixer.library import WatchState

        recipe = load_recipe(self.recipe)
        state = WatchState.load(self.ws / "metadata" / "watch_state.json")
        return _tick(self.ws, recipe, recipe.styles, 5, 2, state,
                     self.recipe.name)

    def test_a_parallel_run_renders_all_and_rerun_skips(self):
        self._clip("a.mp4", 3)
        self._clip("b.mp4", 3)
        out = self._cli("--jobs", "2", "--seed", "1")
        self.assertEqual(out.count("ok "), 2)
        self.assertEqual(len(list((self.ws / "outputs").glob("*.mp4"))), 2)

        rerun = self._cli("--jobs", "2", "--seed", "1")
        self.assertNotIn("ok ", rerun)
        self.assertEqual(rerun.count("skip "), 2)

    def _age(self, path: Path, seconds: float = 10.0) -> None:
        import os as _os
        stat = path.stat()
        _os.utime(path, (stat.st_atime - seconds, stat.st_mtime - seconds))

    def test_b_watch_tick_processes_new_changed_then_idempotent(self):
        from videomixer.cli import _tick
        from videomixer.config import load_recipe
        from videomixer.library import WatchState

        ws2 = self.ws.parent / f"{self.ws.name}_watch"
        for d in ("inputs", "library"):
            (ws2 / d).mkdir(parents=True, exist_ok=True)
        shutil.copy(self.ws / "library" / "bed.mp3", ws2 / "library" / "bed.mp3")
        recipe2 = ws2 / "recipe.toml"
        recipe2.write_text(self.recipe.read_text(encoding="utf-8"),
                           encoding="utf-8")

        recipe = load_recipe(recipe2)
        state_path = ws2 / "metadata" / "watch_state.json"

        state = WatchState.load(state_path)
        self.assertEqual(_tick(ws2, recipe, recipe.styles, 5, 1, state,
                               recipe2.name), 0)

        self._ff(["-f", "lavfi", "-i",
                  "testsrc2=duration=3:size=320x240:rate=15",
                  "-c:v", "libx264", "-preset", "ultrafast",
                  "-pix_fmt", "yuv420p", str(ws2 / "inputs" / "w1.mp4")])
        self._age(ws2 / "inputs" / "w1.mp4")

        self.assertEqual(_tick(ws2, recipe, recipe.styles, 5, 1, state,
                               recipe2.name), 1)
        manifests = list((ws2 / "outputs").glob("*.mix.json"))
        self.assertEqual(len(manifests), 1)
        state.save()

        state2 = WatchState.load(state_path)
        self.assertEqual(_tick(ws2, recipe, recipe.styles, 5, 1, state2,
                               recipe2.name), 0)
        self.assertEqual(len(list((ws2 / "outputs").glob("*.mp4"))), 1)

        self._ff(["-f", "lavfi", "-i",
                  "testsrc2=duration=5:size=320x240:rate=15",
                  "-c:v", "libx264", "-preset", "ultrafast",
                  "-pix_fmt", "yuv420p", str(ws2 / "inputs" / "w1.mp4")])
        self._age(ws2 / "inputs" / "w1.mp4")
        state3 = WatchState.load(state_path)
        self.assertEqual(_tick(ws2, recipe, recipe.styles, 5, 1, state3,
                               recipe2.name), 1)
        self.assertEqual(len(list((ws2 / "outputs").glob("*.mp4"))), 2)

    def test_c_stats_reports_workspace_text_and_json(self):
        buf = StringIO()
        with redirect_stdout(buf):
            rc = main(["stats", "--root", str(self.ws),
                       "--recipe", str(self.recipe)])
        self.assertEqual(rc, 0)
        text = buf.getvalue()
        self.assertIn("library: 1 tracks", text)
        self.assertIn("coverage (tolerance +/-1.5s)", text)
        self.assertIn("a.mp4", text)

        buf = StringIO()
        with redirect_stdout(buf):
            rc = main(["stats", "--root", str(self.ws),
                       "--recipe", str(self.recipe), "--json"])
        self.assertEqual(rc, 0)
        doc = json.loads(buf.getvalue())
        self.assertEqual(doc["counts"]["videos"], 2)
        self.assertIn("usage", doc)


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not available")
class TestMovementsEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.ws = Path(cls.tmp.name)
        cls.recipe = cls.ws / "recipe.toml"
        cls.recipe.write_text(
            "[run]\nversions_per_video = 1\n\n[style.suite]\n"
            "bgm_gain_db = -14.0\nmovement = 10\ncrossfade = 1.5\n",
            encoding="utf-8")
        for d in ("inputs", "library", "sfx"):
            (cls.ws / d).mkdir()
        cls._ff(["-f", "lavfi", "-i",
                 "testsrc2=duration=30:size=320x240:rate=15",
                 "-c:v", "libx264", "-preset", "ultrafast",
                 "-pix_fmt", "yuv420p",
                 str(cls.ws / "inputs" / "long.mp4")])
        for i, (freq, dur) in enumerate(((220, 9), (330, 11), (440, 8))):
            cls._ff(["-f", "lavfi", "-i", f"sine=frequency={freq}:duration={dur}",
                     "-c:a", "libmp3lame",
                     str(cls.ws / "library" / f"bed{i}.mp3")])

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def _ff(cls, args):
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                        *args], capture_output=True, check=True)

    def _cli(self, *extra) -> str:
        buf = StringIO()
        with redirect_stdout(buf):
            rc = main(["run", "--root", str(self.ws),
                       "--recipe", str(self.recipe), *extra])
        self.assertEqual(rc, 0)
        return buf.getvalue()

    def _manifest(self):
        import json as _json
        manifest = next((self.ws / "outputs").glob("*.mix.json"))
        return _json.loads(manifest.read_text(encoding="utf-8"))

    def test_a_three_movements_rendered_with_exact_duration(self):
        out = self._cli("--seed", "6")
        self.assertIn("movements:", out)
        doc = self._manifest()
        movements = doc["design"]["movements"]
        self.assertEqual(len(movements), 3)
        self.assertEqual(movements[0]["start_s"], 0.0)
        self.assertEqual(movements[2]["end_s"], 30.0)
        tracks = {m["track"] for m in movements}
        self.assertEqual(len(tracks), 3)

        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", doc["output"]],
            capture_output=True, text=True, check=True)
        self.assertAlmostEqual(float(probe.stdout.strip()), 30.0, delta=0.5)

    def test_b_rerun_skips_identical_movement_design(self):
        before = sorted(p.name for p in (self.ws / "outputs").glob("*.mp4"))
        out = self._cli("--seed", "6")
        self.assertNotIn("ok ", out)
        self.assertIn("skip ", out)
        after = sorted(p.name for p in (self.ws / "outputs").glob("*.mp4"))
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
