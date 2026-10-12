"""ffprobe/ffmpeg measurement wrappers with an incremental, portable metadata cache."""

from __future__ import annotations

import json
import math
import subprocess
from dataclasses import dataclass
from pathlib import Path

CACHE_VERSION = 5

GAP_NOISE_DB = -35
GAP_MIN_DURATION_S = 0.35

ONSET_SAMPLE_RATE = 11025
ONSET_HOP = 256
ONSET_MIN_GAP_S = 0.12
ONSET_MAX_COUNT = 256
ONSET_MIN_JUMP = 0.35


@dataclass(frozen=True)
class LoudnessStats:
    input_i: float
    input_tp: float
    input_lra: float
    input_thresh: float
    target_offset: float

    def to_dict(self) -> dict:
        return {
            "input_i": round(self.input_i, 2),
            "input_tp": round(self.input_tp, 2),
            "input_lra": round(self.input_lra, 2),
            "input_thresh": round(self.input_thresh, 2),
            "target_offset": round(self.target_offset, 2),
        }

    @classmethod
    def from_dict(cls, doc: dict) -> "LoudnessStats":
        return cls(
            input_i=float(doc["input_i"]),
            input_tp=float(doc["input_tp"]),
            input_lra=float(doc["input_lra"]),
            input_thresh=float(doc["input_thresh"]),
            target_offset=float(doc.get("target_offset", 0.0)),
        )


def _finite(value, default: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def measure_loudness(path: Path) -> LoudnessStats | None:
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats",
        "-i", str(path),
        "-af", "loudnorm=print_format=json",
        "-f", "null", "-",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except OSError:
        return None
    stderr = result.stderr
    start = stderr.find("{")
    end = stderr.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        doc = json.loads(stderr[start:end + 1])
        input_i = _finite(doc.get("input_i"), None)
        if input_i is None:
            return None
        return LoudnessStats(
            input_i=input_i,
            input_tp=_finite(doc.get("input_tp"), -1.5),
            input_lra=max(0.0, _finite(doc.get("input_lra"), 0.0)),
            input_thresh=_finite(doc.get("input_thresh"), -70.0),
            target_offset=_finite(doc.get("target_offset"), 0.0),
        )
    except json.JSONDecodeError:
        return None


def analyze_onsets(path: Path,
                   max_onsets: int = ONSET_MAX_COUNT) -> list[float] | None:
    """Detect percussive onsets via an RMS-envelope novelty function.

    Returns onset times in seconds (sorted), or None when the file cannot
    be decoded. An empty list means "decoded fine, no transients found".
    """
    import array

    hop = ONSET_HOP
    sr = ONSET_SAMPLE_RATE
    bytes_per_hop = hop * 4
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats",
        "-i", str(path),
        "-ac", "1", "-ar", str(sr),
        "-f", "f32le", "-",
    ]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL)
    except OSError:
        return None

    energies: list[float] = []
    eps = 1e-9
    try:
        stream = proc.stdout
        while True:
            frame_bytes = stream.read(bytes_per_hop)
            if not frame_bytes:
                break
            if len(frame_bytes) < bytes_per_hop:
                break
            samples = array.array("f")
            samples.frombytes(frame_bytes)
            sq = sum(v * v for v in samples) / hop
            energies.append(math.sqrt(sq + eps))
    finally:
        proc.stdout.close()
        proc.wait()

    if proc.returncode != 0 or len(energies) < 4:
        return None if proc.returncode != 0 else []

    logs = [math.log(e + eps) for e in energies]
    novelty = [max(0.0, logs[i] - logs[i - 1]) for i in range(1, len(logs))]
    if max(novelty, default=0.0) <= 0.0:
        return []
    smoothed = [
        (novelty[i - 1] + novelty[i] + novelty[i + 1]) / 3.0
        if 0 < i < len(novelty) - 1 else novelty[i]
        for i in range(len(novelty))
    ]
    mean = sum(smoothed) / len(smoothed)
    std = math.sqrt(sum((v - mean) ** 2 for v in smoothed) / len(smoothed))
    threshold = mean + std
    hop_s = hop / sr
    min_gap_hops = max(1, int(ONSET_MIN_GAP_S / hop_s))

    candidates: list[tuple[float, int]] = []
    for i, v in enumerate(smoothed):
        if v < threshold or novelty[i] < ONSET_MIN_JUMP:
            continue
        lo = max(0, i - min_gap_hops)
        hi = min(len(smoothed), i + min_gap_hops + 1)
        if v >= max(smoothed[lo:hi]):
            candidates.append((v, i))
    candidates.sort(reverse=True)
    picked = sorted(i for _, i in candidates[:max_onsets])
    return [round((i + 1) * hop_s, 3) for i in picked]


@dataclass(frozen=True)
class MediaInfo:
    path: Path
    duration: float
    has_audio: bool

    @property
    def key(self) -> str:
        return self.path.resolve().as_posix()


class ProbeCache:
    def __init__(self, cache_file: Path):
        self.cache_file = cache_file
        self._entries: dict[str, dict] = {}
        self._dirty = False
        if cache_file.exists():
            try:
                doc = json.loads(cache_file.read_text(encoding="utf-8"))
                if doc.get("version") in (2, 3, 4, CACHE_VERSION):
                    self._entries = doc.get("entries", {})
            except (json.JSONDecodeError, OSError):
                pass

    def invalidate(self) -> None:
        self._entries.clear()
        self._dirty = True

    def reset(self) -> None:
        """Drop all cached metadata, removing the on-disk file as well."""
        from videomixer.locks import workspace_lock

        self._entries.clear()
        self._dirty = False
        lock_path = self.cache_file.parent / (self.cache_file.name + ".lock")
        with workspace_lock(lock_path, timeout_s=10.0, stale_s=30.0):
            try:
                self.cache_file.unlink()
            except OSError:
                pass

    def prune(self) -> int:
        stale = self.stale_entries()
        for key in stale:
            del self._entries[key]
        if stale:
            self._dirty = True
        return len(stale)

    def get(self, path: Path) -> MediaInfo | None:
        key = path.resolve().as_posix()
        entry = self._entries.get(key)
        if entry is None:
            return None
        try:
            stat = path.stat()
        except OSError:
            return None
        if entry.get("mtime") != stat.st_mtime or entry.get("size") != stat.st_size:
            return None
        return MediaInfo(path=path, duration=float(entry["duration"]),
                         has_audio=bool(entry["has_audio"]))

    def put(self, info: MediaInfo) -> None:
        stat = info.path.stat()
        key = info.key
        new = {"mtime": stat.st_mtime, "size": stat.st_size,
               "duration": info.duration, "has_audio": info.has_audio}
        if self._entries.get(key) != new:
            self._entries[key] = new
            self._dirty = True

    def get_loudness(self, path: Path) -> LoudnessStats | None:
        key = path.resolve().as_posix()
        entry = self._entries.get(key)
        if entry is None or "loudness" not in entry:
            return None
        try:
            return LoudnessStats.from_dict(entry["loudness"])
        except (KeyError, TypeError, ValueError):
            return None

    def put_loudness(self, path: Path, stats: LoudnessStats) -> None:
        key = path.resolve().as_posix()
        if key not in self._entries:
            try:
                stat = path.stat()
            except OSError:
                return
            self._entries[key] = {"mtime": stat.st_mtime, "size": stat.st_size,
                                  "duration": 0.0, "has_audio": True}
        self._entries[key]["loudness"] = stats.to_dict()
        self._dirty = True

    def _touch_entry(self, path: Path) -> str | None:
        key = path.resolve().as_posix()
        if key in self._entries:
            return key
        try:
            stat = path.stat()
        except OSError:
            return None
        self._entries[key] = {"mtime": stat.st_mtime, "size": stat.st_size,
                              "duration": 0.0, "has_audio": True}
        self._dirty = True
        return key

    def get_onsets(self, path: Path) -> list[float] | None:
        key = path.resolve().as_posix()
        entry = self._entries.get(key)
        if entry is None or "onsets" not in entry:
            return None
        try:
            onsets = entry["onsets"]
            if not isinstance(onsets, list):
                return None
            return [float(t) for t in onsets]
        except (TypeError, ValueError):
            return None

    def put_onsets(self, path: Path, onsets: list[float]) -> None:
        key = self._touch_entry(path)
        if key is None:
            return
        self._entries[key]["onsets"] = [round(float(t), 3) for t in onsets]
        self._dirty = True

    def stale_entries(self) -> list[str]:
        return [key for key in self._entries if not Path(key).exists()]

    def get_voice_gaps(self, path: Path) -> list[tuple[float, float]] | None:
        key = path.resolve().as_posix()
        entry = self._entries.get(key)
        if entry is None or "voice_gaps" not in entry:
            return None
        try:
            raw = entry["voice_gaps"]
            if not isinstance(raw, list):
                return None
            return [(float(s), float(e)) for s, e in raw]
        except (TypeError, ValueError):
            return None

    def put_voice_gaps(self, path: Path,
                       gaps: list[tuple[float, float]]) -> None:
        key = self._touch_entry(path)
        if key is None:
            return
        self._entries[key]["voice_gaps"] = [
            [round(float(s), 3), round(float(e), 3)] for s, e in gaps]
        self._dirty = True

    def save(self) -> None:
        """Persist the cache, merging with concurrent writers.

        Under an inter-process lock the latest on-disk entries are loaded
        and this process's entries are layered on top, so a concurrent
        `watch` measurement is never lost to a `run` planning save.
        """
        if not self._dirty:
            return
        from videomixer.locks import workspace_lock

        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.cache_file.parent / (self.cache_file.name + ".lock")
        with workspace_lock(lock_path, timeout_s=10.0, stale_s=30.0):
            merged: dict = {}
            try:
                doc = json.loads(self.cache_file.read_text(encoding="utf-8"))
                if doc.get("version") in (2, 3, 4, CACHE_VERSION):
                    merged = dict(doc.get("entries", {}))
            except (json.JSONDecodeError, OSError):
                pass
            merged.update(self._entries)
            self._entries = merged
            tmp = self.cache_file.with_suffix(self.cache_file.suffix + ".tmp")
            tmp.write_text(json.dumps(
                {"version": CACHE_VERSION, "entries": self._entries},
                indent=2), encoding="utf-8")
            tmp.replace(self.cache_file)
        self._dirty = False


def analyze_voice_gaps(path: Path) -> list[tuple[float, float]] | None:
    """Detect silence stretches in a file's audio via ffmpeg silencedetect.

    Returns (start, end) pairs sorted by start, or None on decode failure.
    An empty list means "decoded fine, no usable gaps".
    """
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats",
        "-i", str(path),
        "-af", f"silencedetect=noise={GAP_NOISE_DB}dB"
               f":d={GAP_MIN_DURATION_S}",
        "-f", "null", "-",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except OSError:
        return None
    starts: list[float] = []
    gaps: list[tuple[float, float]] = []
    for line in result.stderr.splitlines():
        if "silence_start:" in line:
            try:
                tail = line.split("silence_start:", 1)[1]
                starts.append(float(tail.split()[0]))
            except (ValueError, IndexError):
                pass
        elif "silence_end:" in line and starts:
            try:
                tail = line.split("silence_end:", 1)[1]
                end = float(tail.split("|")[0].split()[0])
                gaps.append((starts.pop(0), end))
            except (ValueError, IndexError):
                pass
    return sorted(gaps)


def probe_media(path: Path) -> MediaInfo | None:
    cmd = [
        "ffprobe", "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_streams",
        str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None
    try:
        doc = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    fmt = doc.get("format", {})
    duration: float | None = None
    try:
        duration = float(fmt["duration"])
    except (KeyError, ValueError, TypeError):
        durations = [float(s["duration"]) for s in doc.get("streams", [])
                     if "duration" in s]
        duration = max(durations) if durations else None
    if duration is None or duration <= 0:
        return None
    has_audio = any(s.get("codec_type") == "audio" for s in doc.get("streams", []))
    return MediaInfo(path=path, duration=duration, has_audio=has_audio)


def probe_with_cache(path: Path, cache: ProbeCache) -> MediaInfo | None:
    info = cache.get(path)
    if info is None:
        info = probe_media(path)
        if info is not None:
            cache.put(info)
    return info
