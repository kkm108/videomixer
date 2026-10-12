"""Declarative recipe and style definitions loaded from TOML."""

from __future__ import annotations

import math
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class SfxCueSpec:
    pattern: str = "*"
    at: float = 0.0
    gain_db: float = -6.0
    snap: bool = False
    snap_window_s: float = 0.5


@dataclass(frozen=True)
class Style:
    name: str
    bgm_gain_db: float = -16.0
    loudness_lufs: float | None = None
    duck: bool = False
    duck_threshold_db: float = -26.0
    duck_ratio: float = 6.0
    duck_attack_ms: int = 15
    duck_release_ms: int = 450
    fade_in_s: float = 0.8
    fade_out_s: float = 1.5
    loop_bgm: bool = True
    limiter: bool = True
    sfx_cues: tuple[SfxCueSpec, ...] = ()
    movement_s: float | None = None
    crossfade_s: float = 2.0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "bgm_gain_db": self.bgm_gain_db,
            "loudness_lufs": self.loudness_lufs,
            "duck": self.duck,
            "duck_threshold_db": self.duck_threshold_db,
            "duck_ratio": self.duck_ratio,
            "duck_attack_ms": self.duck_attack_ms,
            "duck_release_ms": self.duck_release_ms,
            "fade_in_s": self.fade_in_s,
            "fade_out_s": self.fade_out_s,
            "loop_bgm": self.loop_bgm,
            "limiter": self.limiter,
            "sfx_cues": [vars(c) for c in self.sfx_cues],
            "movement_s": self.movement_s,
            "crossfade_s": self.crossfade_s,
        }


@dataclass(frozen=True)
class Recipe:
    versions_per_video: int = 2
    tolerance_s: float = 1.5
    seed: int | None = None
    audio_bitrate: str = "192k"
    styles: tuple[Style, ...] = field(default_factory=tuple)

    def style_names(self) -> list[str]:
        return [s.name for s in self.styles]


def _linear_to_db(value: float) -> float:
    return 20.0 * math.log10(value) if value > 0 else -120.0


def load_style(name: str, raw: dict) -> Style:
    duck_raw = raw.get("duck", {})
    if isinstance(duck_raw, bool):
        duck_cfg = {"enabled": duck_raw}
    else:
        duck_cfg = dict(duck_raw)
        duck_cfg.setdefault("enabled", False)
    cues = tuple(
        SfxCueSpec(
            pattern=str(c.get("pattern", "*")),
            at=float(c.get("at", 0.0)),
            gain_db=float(c.get("gain_db", -6.0)),
            snap=bool(c.get("snap", False)),
            snap_window_s=float(c.get("snap_window", 0.5)),
        )
        for c in raw.get("sfx", [])
    )
    return Style(
        name=name,
        bgm_gain_db=float(raw.get("bgm_gain_db", -16.0)),
        loudness_lufs=(float(raw["loudness_lufs"]) if raw.get("loudness_lufs") is not None else None),
        duck=bool(duck_cfg.get("enabled", False)),
        duck_threshold_db=_linear_to_db(float(duck_cfg.get("threshold", 0.05))),
        duck_ratio=float(duck_cfg.get("ratio", 6.0)),
        duck_attack_ms=int(duck_cfg.get("attack_ms", 15)),
        duck_release_ms=int(duck_cfg.get("release_ms", 450)),
        fade_in_s=float(raw.get("fade_in", 0.8)),
        fade_out_s=float(raw.get("fade_out", 1.5)),
        loop_bgm=bool(raw.get("loop_bgm", True)),
        limiter=bool(raw.get("limiter", True)),
        sfx_cues=cues,
        movement_s=(float(raw["movement"]) if raw.get("movement") is not None else None),
        crossfade_s=float(raw.get("crossfade", 2.0)),
    )


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def validate_style(style: Style) -> None:
    _require(style.name.strip() != "", "style name must not be empty")
    _require(math.isfinite(style.bgm_gain_db),
             f"style '{style.name}': bgm_gain_db must be finite")
    if style.loudness_lufs is not None:
        _require(-70.0 <= style.loudness_lufs <= -5.0,
                 f"style '{style.name}': loudness_lufs must be between "
                 f"-70 and -5 (got {style.loudness_lufs})")
    _require(style.fade_in_s >= 0,
             f"style '{style.name}': fade_in must be >= 0")
    _require(style.fade_out_s >= 0,
             f"style '{style.name}': fade_out must be >= 0")
    _require(style.duck_ratio > 0,
             f"style '{style.name}': duck ratio must be > 0")
    _require(style.duck_attack_ms >= 1 and style.duck_release_ms >= 1,
             f"style '{style.name}': duck attack/release must be >= 1 ms")
    if style.movement_s is not None:
        _require(style.movement_s > 0,
                 f"style '{style.name}': movement must be > 0 seconds "
                 f"(omit the key to disable)")
    _require(style.crossfade_s > 0,
             f"style '{style.name}': crossfade must be > 0 seconds")
    for cue in style.sfx_cues:
        _require(cue.snap_window_s >= 0,
                 f"style '{style.name}': sfx snap_window must be >= 0")
        _require(abs(cue.at) <= 3600.0,
                 f"style '{style.name}': sfx 'at' offset must be within "
                 f"±3600 seconds")


def validate_recipe(recipe: Recipe) -> None:
    _require(recipe.versions_per_video >= 1,
             f"run.versions_per_video must be >= 1 "
             f"(got {recipe.versions_per_video})")
    _require(recipe.tolerance_s >= 0,
             f"run.tolerance must be >= 0 (got {recipe.tolerance_s})")
    _require(recipe.audio_bitrate.strip() != "",
             "run.audio_bitrate must not be empty")
    _require(len(recipe.styles) >= 1,
             "recipe defines no [style.*] sections")
    for style in recipe.styles:
        validate_style(style)


def load_recipe(path: Path) -> Recipe:
    with open(path, "rb") as fh:
        doc = tomllib.load(fh)
    run = doc.get("run", {})
    seed_raw = run.get("seed")
    styles = tuple(
        load_style(name, raw)
        for name, raw in doc.get("style", {}).items()
    )
    recipe = Recipe(
        versions_per_video=int(run.get("versions_per_video", 2)),
        tolerance_s=float(run.get("tolerance", 1.5)),
        seed=(int(seed_raw) if seed_raw is not None else None),
        audio_bitrate=str(run.get("audio_bitrate", "192k")),
        styles=styles,
    )
    validate_recipe(recipe)
    return recipe


DEFAULT_RECIPE_TOML = """\
[run]
versions_per_video = 2
tolerance = 1.5
audio_bitrate = "192k"

[style.balanced]
bgm_gain_db = -16.0
loudness_lufs = -19.0
fade_in = 0.8
fade_out = 1.5

[style.balanced.duck]
enabled = false

[style.impact]
bgm_gain_db = -13.0
loudness_lufs = -17.0
fade_in = 0.05
fade_out = 1.2

[style.impact.duck]
enabled = true
threshold = 0.05
ratio = 6.0
attack_ms = 15
release_ms = 450

[[style.impact.sfx]]
pattern = "*"
at = 0.0
gain_db = -4.0
"""
