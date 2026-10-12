"""Media library scanning and scored, repetition-aware track selection."""

from __future__ import annotations

import fnmatch
import json
import random
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from videomixer.locks import workspace_lock
from videomixer.probe import (
    LoudnessStats,
    MediaInfo,
    ProbeCache,
    analyze_onsets,
    analyze_voice_gaps,
    measure_loudness,
    probe_with_cache,
)

AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".opus"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}
MAX_WATCH_ATTEMPTS = 3


def _read_usage_counts(path: Path) -> dict[str, int]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {k: int(v.get("uses", 0)) for k, v in raw.items()}
    except (OSError, ValueError, AttributeError):
        return {}


@dataclass
class UsageLedger:
    path: Path
    uses: dict[str, int] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "UsageLedger":
        return cls(path=path, uses=_read_usage_counts(path))

    def count(self, key: str) -> int:
        return self.uses.get(key, 0)

    def record(self, keys: list[str]) -> None:
        """Increment usage counts, merging with concurrent writers.

        Re-reads the on-disk ledger inside an inter-process lock, applies
        this call's delta, then atomically replaces the file. Concurrent
        `run`/`watch` processes therefore cannot silently clobber each
        other's counts.
        """
        delta = Counter(keys)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.parent / (self.path.name + ".lock")
        with workspace_lock(lock_path, timeout_s=10.0, stale_s=30.0):
            merged = _read_usage_counts(self.path)
            for key, n in delta.items():
                merged[key] = merged.get(key, 0) + n
            self.uses = dict(merged)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(
                json.dumps({k: {"uses": v} for k, v in sorted(merged.items())},
                           indent=2),
                encoding="utf-8")
            tmp.replace(self.path)


class Library:
    def __init__(self, root: Path, cache: ProbeCache):
        self.root = root
        self.cache = cache
        self.tracks: list[MediaInfo] = []

    def scan(self, force: bool = False) -> list[MediaInfo]:
        if force:
            self.cache.reset()
        self.tracks = []
        for f in sorted(self.root.rglob("*")):
            if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS:
                info = probe_with_cache(f, self.cache)
                if info is not None:
                    self.tracks.append(info)
        return self.tracks

    def sfx_pool(self, sfx_dir: Path, pattern: str = "*") -> list[MediaInfo]:
        pool: list[MediaInfo] = []
        for f in sorted(sfx_dir.rglob("*")):
            if not f.is_file() or f.suffix.lower() not in AUDIO_EXTENSIONS:
                continue
            if not fnmatch.fnmatch(f.name.lower(), pattern.lower()):
                continue
            info = probe_with_cache(f, self.cache)
            if info is not None:
                pool.append(info)
        return pool

    def ensure_loudness(self, track: MediaInfo) -> tuple[LoudnessStats | None, bool]:
        cached = self.cache.get_loudness(track.path)
        if cached is not None:
            return cached, False
        stats = measure_loudness(track.path)
        if stats is not None:
            self.cache.put_loudness(track.path, stats)
            self.cache.save()
        return stats, stats is not None

    def ensure_onsets(self, track: MediaInfo) -> tuple[list[float] | None, bool]:
        cached = self.cache.get_onsets(track.path)
        if cached is not None:
            return cached, False
        onsets = analyze_onsets(track.path)
        if onsets is not None:
            self.cache.put_onsets(track.path, onsets)
            self.cache.save()
        return onsets, onsets is not None

    def ensure_voice_gaps(self, media: MediaInfo) -> tuple[list[tuple[float, float]] | None, bool]:
        cached = self.cache.get_voice_gaps(media.path)
        if cached is not None:
            return cached, False
        gaps = analyze_voice_gaps(media.path)
        if gaps is not None:
            self.cache.put_voice_gaps(media.path, gaps)
            self.cache.save()
        return gaps, gaps is not None


class WatchState:
    """Remembers which input files (by mtime+size) have been processed.

    Failed renders are retained with an attempt counter and retried up to
    MAX_WATCH_ATTEMPTS times per file revision, then left alone (touch the
    file to reset). Entries written by older versions (no status field)
    are treated as successfully processed.
    """

    def __init__(self, path: Path, seen: dict[str, dict] | None = None):
        self.path = path
        self.seen: dict[str, dict] = seen or {}

    @classmethod
    def load(cls, path: Path) -> "WatchState":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return cls(path=path, seen=dict(raw))
        except (OSError, ValueError, AttributeError):
            return cls(path=path)

    @staticmethod
    def _signature(video: Path) -> dict | None:
        try:
            stat = video.stat()
        except OSError:
            return None
        return {"mtime": stat.st_mtime, "size": stat.st_size}

    @staticmethod
    def _same_revision(entry: dict, sig: dict) -> bool:
        return (entry.get("mtime") == sig["mtime"]
                and entry.get("size") == sig["size"])

    def pending(self, videos: list[Path]) -> list[Path]:
        result = []
        for video in videos:
            sig = self._signature(video)
            if sig is None:
                continue
            key = video.resolve().as_posix()
            entry = self.seen.get(key)
            if not isinstance(entry, dict):
                result.append(video)
                continue
            if not self._same_revision(entry, sig):
                result.append(video)
                continue
            if (entry.get("status", "done") == "failed"
                    and int(entry.get("attempts", 0)) < MAX_WATCH_ATTEMPTS):
                result.append(video)
        return result

    @property
    def max_attempts(self) -> int:
        return MAX_WATCH_ATTEMPTS

    def mark_success(self, video: Path) -> None:
        sig = self._signature(video)
        if sig is not None:
            self.seen[video.resolve().as_posix()] = {
                **sig, "status": "done", "attempts": 0}

    def mark_failed(self, video: Path) -> None:
        sig = self._signature(video)
        if sig is None:
            return
        key = video.resolve().as_posix()
        prev = self.seen.get(key)
        attempts = 1
        if isinstance(prev, dict) and self._same_revision(prev, sig):
            attempts = int(prev.get("attempts", 0)) + 1
        self.seen[key] = {**sig, "status": "failed", "attempts": attempts}

    def mark(self, video: Path) -> None:
        self.mark_success(video)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.seen, indent=2, sort_keys=True),
                       encoding="utf-8")
        tmp.replace(self.path)


def select_tracks(
    tracks: list[MediaInfo],
    target_duration: float,
    count: int,
    ledger: UsageLedger,
    rng: random.Random,
    tolerance_s: float,
) -> list[MediaInfo]:
    scored = []
    for t in tracks:
        diff = abs(t.duration - target_duration)
        effective_diff = 0.0 if diff <= tolerance_s else diff
        score = (ledger.count(t.key), effective_diff, rng.random())
        scored.append((score, t))
    scored.sort(key=lambda item: item[0])
    return [track for _, track in scored[:count]]
