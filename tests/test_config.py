import math
import tempfile
import tomllib
import unittest
from pathlib import Path

from videomixer.config import (
    DEFAULT_RECIPE_TOML,
    Style,
    _linear_to_db,
    load_recipe,
    load_style,
)

try:
    from . import helpers
except ImportError:
    import helpers


class TestLinearConversion(unittest.TestCase):
    def test_linear_to_db_known_values(self):
        self.assertAlmostEqual(_linear_to_db(0.05), -26.0206, places=3)
        self.assertAlmostEqual(_linear_to_db(1.0), 0.0, places=6)
        self.assertEqual(_linear_to_db(0.0), -120.0)


class TestShippedRecipe(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.recipe = load_recipe(helpers.REPO_ROOT / "recipes" / "default.toml")

    def test_run_section(self):
        self.assertEqual(self.recipe.versions_per_video, 2)
        self.assertAlmostEqual(self.recipe.tolerance_s, 1.5)
        self.assertIsNone(self.recipe.seed)
        self.assertEqual(self.recipe.audio_bitrate, "192k")

    def test_styles_parsed_in_order(self):
        self.assertEqual(self.recipe.style_names(),
                         ["balanced", "impact", "cinematic", "suite"])

    def test_balanced_no_duck(self):
        style = self.recipe.styles[0]
        self.assertFalse(style.duck)
        self.assertAlmostEqual(style.loudness_lufs, -19.0)
        self.assertEqual(style.sfx_cues, ())

    def test_impact_duck_threshold_converted_to_db(self):
        style = self.recipe.styles[1]
        self.assertTrue(style.duck)
        self.assertAlmostEqual(style.duck_threshold_db,
                               _linear_to_db(0.05), places=3)
        self.assertEqual(len(style.sfx_cues), 1)
        cue = style.sfx_cues[0]
        self.assertEqual(cue.pattern, "*")
        self.assertEqual(cue.at, 0.0)

    def test_cinematic_negative_outro_cue(self):
        cue = self.recipe.styles[2].sfx_cues[0]
        self.assertEqual(cue.pattern, "impact_*")
        self.assertEqual(cue.at, -1.2)


class TestRecipeParsing(unittest.TestCase):
    def write_and_load(self, text: str) -> object:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "recipe.toml"
            path.write_text(text, encoding="utf-8")
            return load_recipe(path)

    def test_default_embedded_recipe_parses(self):
        recipe = self.write_and_load(DEFAULT_RECIPE_TOML)
        self.assertEqual(recipe.style_names(), ["balanced", "impact"])

    def test_overrides(self):
        recipe = self.write_and_load(
            '[run]\nversions_per_video = 5\ntolerance = 0.5\nseed = 7\n'
            'audio_bitrate = "128k"\n\n[style.x]\n'
        )
        self.assertEqual(recipe.versions_per_video, 5)
        self.assertEqual(recipe.tolerance_s, 0.5)
        self.assertEqual(recipe.seed, 7)
        self.assertEqual(recipe.audio_bitrate, "128k")

    def test_duck_bool_shorthand(self):
        raw = {"duck": True}
        style = load_style("x", raw)
        self.assertTrue(style.duck)

    def test_missing_style_section_is_rejected(self):
        with self.assertRaises(ValueError):
            self.write_and_load("[run]\nversions_per_video = 1\n")

    def test_invalid_toml_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.toml"
            path.write_text("[run\nversions=1", encoding="utf-8")
            with self.assertRaises(tomllib.TOMLDecodeError):
                load_recipe(path)


class TestStyleDefaults(unittest.TestCase):
    def test_minimal_style_uses_sane_defaults(self):
        style = load_style("bare", {})
        self.assertIsInstance(style, Style)
        self.assertAlmostEqual(style.bgm_gain_db, -16.0)
        self.assertFalse(style.duck)
        self.assertEqual(style.loop_bgm, True)
        self.assertEqual(style.limiter, True)

    def test_loudness_none_stays_none(self):
        style = load_style("bare", {})
        self.assertIsNone(style.loudness_lufs)

    def test_finite_conversion_for_zero_threshold(self):
        self.assertTrue(math.isfinite(_linear_to_db(1e-6)))


if __name__ == "__main__":
    unittest.main()
