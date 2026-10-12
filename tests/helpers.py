"""Shared builders for fast, ffmpeg-free unit tests."""

from __future__ import annotations

from pathlib import Path

from videomixer.config import SfxCueSpec, Style
from videomixer.design import AudioDesign, PlacedCue
from videomixer.probe import LoudnessStats, MediaInfo

REPO_ROOT = Path(__file__).resolve().parents[1]


def make_media(name: str = "library/bgm.mp3",
               duration: float = 30.0,
               has_audio: bool = True) -> MediaInfo:
    return MediaInfo(path=Path("C:/ws") / name,
                     duration=duration, has_audio=has_audio)


def make_style(name: str = "balanced", **overrides) -> Style:
    return Style(name=name, loudness_lufs=-19.0, **overrides)


STATS = LoudnessStats(input_i=-22.25, input_tp=-2.0, input_lra=3.0,
                      input_thresh=-32.0, target_offset=0.05)


def make_design(video_dur: float = 30.0,
                voiced: bool = True,
                track_dur: float = 45.0,
                style: Style | None = None,
                sfx: list[PlacedCue] | None = None,
                stats: LoudnessStats | None = STATS) -> AudioDesign:
    return AudioDesign(
        video=make_media("inputs/clip.mp4", video_dur, voiced),
        track=make_media("library/bgm.mp3", track_dur),
        style=style or make_style(),
        seed=42,
        sfx_cues=sfx or [],
        track_loudness=stats,
    )


def make_cue(at: float = 0.0, gain_db: float = -4.0) -> PlacedCue:
    return PlacedCue(path=Path("C:/ws/sfx/hit.mp3"), at_s=at, gain_db=gain_db)


def cue_spec(pattern: str = "*", at: float = 0.0, gain_db: float = -6.0) -> SfxCueSpec:
    return SfxCueSpec(pattern=pattern, at=at, gain_db=gain_db)
