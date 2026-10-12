"""AudioDesign: a pure, serializable plan of what will be mixed, before rendering."""

from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import dataclass, field
from pathlib import Path

from videomixer.config import Style
from videomixer.library import Library
from videomixer.probe import LoudnessStats, MediaInfo


@dataclass(frozen=True)
class Movement:
    track: MediaInfo
    start_s: float
    end_s: float
    loudness: LoudnessStats | None = None
    gap_aligned: bool = False

    def to_dict(self) -> dict:
        return {
            "track": self.track.key,
            "track_duration": round(self.track.duration, 3),
            "start_s": round(self.start_s, 3),
            "end_s": round(self.end_s, 3),
            "gap_aligned": self.gap_aligned,
        }


def plan_boundaries(video_len: float,
                    movement_s: float,
                    gaps: list[tuple[float, float]],
                    window: float) -> list[float]:
    """Internal movement boundaries, snapped into voice gaps when possible.

    Boundaries are kept strictly increasing with at least 0.5s spacing and
    at least 0.5s margin from the video edges; impossible boundaries are
    dropped rather than collapsed onto each other (which would create
    zero/negative-length segments).
    """
    if movement_s <= 0 or video_len <= movement_s * 1.5:
        return []
    n = max(2, int(video_len / movement_s + 0.5))
    n = min(n, max(2, int(video_len)))
    raw = [video_len * i / n for i in range(1, n)]
    centers = sorted((s + e) / 2.0 for s, e in gaps)
    snapped: list[float] = []
    for b in raw:
        best, best_dist = b, window + 1.0
        for c in centers:
            dist = abs(c - b)
            if dist < best_dist:
                best, best_dist = c, dist
        candidate = best if best_dist <= window else b
        lo = (snapped[-1] + 0.5) if snapped else 0.5
        hi = video_len - 0.5
        candidate = min(max(candidate, lo), hi)
        if snapped and candidate <= snapped[-1]:
            continue
        snapped.append(candidate)
    return snapped


@dataclass(frozen=True)
class PlacedCue:
    path: Path
    at_s: float
    gain_db: float
    requested_at_s: float | None = None

    @property
    def snapped(self) -> bool:
        return (self.requested_at_s is not None
                and abs(self.requested_at_s - self.at_s) > 1e-6)

    def to_dict(self) -> dict:
        doc = {"path": self.path.name, "at_s": round(self.at_s, 3),
               "gain_db": self.gain_db}
        if self.requested_at_s is not None:
            doc["requested_at_s"] = round(self.requested_at_s, 3)
            doc["snapped"] = self.snapped
        return doc


def snap_time(desired: float,
              onsets: list[float],
              bed_len: float,
              video_len: float,
              loop_bed: bool,
              window_s: float) -> float | None:
    """Return the onset closest to `desired`, or None outside the window.

    When the bed loops across the video, onsets repeat every `bed_len`
    seconds, so candidates are generated over the full video duration.
    """
    if not onsets or bed_len <= 0:
        return None
    reps = 1
    if loop_bed and bed_len < video_len:
        reps = int(math.ceil(video_len / bed_len)) + 1
    best: float | None = None
    best_dist = float("inf")
    for rep in range(reps):
        offset = rep * bed_len
        for t in onsets:
            cand = t + offset
            dist = abs(cand - desired)
            if dist < best_dist:
                best, best_dist = cand, dist
    if best is None or best_dist > window_s:
        return None
    return max(0.0, min(best, max(0.0, video_len - 0.05)))


@dataclass
class AudioDesign:
    video: MediaInfo
    track: MediaInfo
    style: Style
    seed: int | None
    sfx_cues: list[PlacedCue] = field(default_factory=list)
    track_loudness: LoudnessStats | None = None
    movements: list["Movement"] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return self.video.duration

    @property
    def duck_active(self) -> bool:
        return self.style.duck and self.video.has_audio

    @property
    def multi_movement(self) -> bool:
        return len(self.movements) >= 2

    @property
    def normalization_mode(self) -> str:
        if self.style.loudness_lufs is None:
            return "off"
        if self.multi_movement:
            measured = sum(1 for m in self.movements if m.loudness is not None)
            if measured == len(self.movements):
                return "linear"
            return "dynamic" if measured == 0 else "mixed"
        return "linear" if self.track_loudness is not None else "dynamic"

    def all_track_keys(self) -> list[str]:
        keys = [self.track.key]
        keys.extend(m.track.key for m in self.movements)
        seen: dict[str, None] = {}
        for k in keys:
            seen.setdefault(k)
        return list(seen)

    def fingerprint_payload(self) -> str:
        doc = self.to_dict()
        doc.pop("seed", None)
        return json.dumps(doc, sort_keys=True, separators=(",", ":"))

    def fingerprint(self) -> str:
        return hashlib.sha256(
            self.fingerprint_payload().encode("utf-8")
        ).hexdigest()[:16]

    def to_dict(self) -> dict:
        doc = {
            "video": self.video.key,
            "video_duration": round(self.video.duration, 3),
            "has_voice": self.video.has_audio,
            "track": self.track.key,
            "track_duration": round(self.track.duration, 3),
            "style": self.style.to_dict(),
            "sfx_cues": [c.to_dict() for c in self.sfx_cues],
            "duck_active": self.duck_active,
            "loudness_target": self.style.loudness_lufs,
            "track_loudness": (self.track_loudness.to_dict()
                               if self.track_loudness else None),
            "normalization_mode": self.normalization_mode,
            "seed": self.seed,
        }
        if self.movements:
            doc["movements"] = [m.to_dict() for m in self.movements]
        return doc


def resolve_cue_time(spec_at: float, video_len: float) -> float:
    if spec_at < 0:
        return max(0.0, video_len + spec_at)
    return min(spec_at, max(0.0, video_len - 0.1))


def build_movements(video, style, library, tracks, session, rng,
                    tolerance_s) -> list["Movement"]:
    from videomixer.library import select_tracks

    gaps: list[tuple[float, float]] = []
    if video.has_audio:
        gaps, _ = library.ensure_voice_gaps(video)
    window = max(style.crossfade_s, 1.0)
    boundaries = plan_boundaries(video.duration, style.movement_s or 1e9,
                                 gaps or [], window)
    if not boundaries:
        return []
    count = len(boundaries) + 1
    picked = select_tracks(tracks, count=count,
                           target_duration=style.movement_s,
                           ledger=session, rng=rng, tolerance_s=tolerance_s)
    if len(picked) < count:
        return []
    edges = [0.0] + boundaries + [video.duration]
    gap_centers = [(s + e) / 2.0 for s, e in (gaps or [])]
    movements: list[Movement] = []
    for i, mv_track in enumerate(picked):
        start, end = edges[i], edges[i + 1]
        aligned = any(abs(c - end) <= 0.05 for c in gap_centers)
        stats, _ = library.ensure_loudness(mv_track)
        movements.append(Movement(track=mv_track, start_s=start, end_s=end,
                                  loudness=stats, gap_aligned=aligned))
    return movements


def build_design(
    video: MediaInfo,
    track: MediaInfo,
    style: Style,
    library: Library,
    sfx_dir: Path,
    rng: random.Random,
    tracks=None,
    session=None,
    tolerance_s: float = 1.5,
) -> AudioDesign:
    design = AudioDesign(video=video, track=track, style=style, seed=None)
    if style.movement_s and tracks is not None and session is not None:
        design.movements = build_movements(video, style, library, tracks,
                                           session, rng, tolerance_s)
    bed = design.movements[0].track if design.movements else track
    for spec in style.sfx_cues:
        pool = library.sfx_pool(sfx_dir, spec.pattern)
        if not pool:
            continue
        chosen = rng.choice(pool)
        desired = resolve_cue_time(spec.at, design.duration)
        requested: float | None = None
        at = desired
        if spec.snap:
            onsets, _ = library.ensure_onsets(bed)
            loop_bed = style.loop_bgm and bed.duration < design.duration - 0.5
            snapped = snap_time(desired, onsets or [], bed.duration,
                                design.duration, loop_bed, spec.snap_window_s)
            if snapped is not None:
                at = snapped
                requested = desired
        design.sfx_cues.append(
            PlacedCue(path=chosen.path, at_s=float(at), gain_db=spec.gain_db,
                      requested_at_s=requested)
        )
    return design
