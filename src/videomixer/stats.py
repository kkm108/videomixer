"""Workspace analytics: coverage gaps, duration distribution, usage heat."""

from __future__ import annotations

from pathlib import Path

BUCKET_EDGES = (
    (0.0, 15.0, "<15s"),
    (15.0, 30.0, "15-30s"),
    (30.0, 60.0, "30-60s"),
    (60.0, 120.0, "60-120s"),
    (120.0, float("inf"), "2min+"),
)


def bucketize(durations: list[float]) -> list[dict]:
    counts = {label: 0 for _, _, label in BUCKET_EDGES}
    for d in durations:
        for lo, hi, label in BUCKET_EDGES:
            if lo <= d < hi:
                counts[label] += 1
                break
    return [{"label": label, "count": counts[label]}
            for _, _, label in BUCKET_EDGES]


def build_report(
    *,
    workspace: str,
    tolerance: float,
    tracks,
    videos,
    uses: dict[str, int],
    outputs: int,
    manifests: int,
    loudness_ready: int,
    onsets_ready: int,
    stale_entries: int,
) -> dict:
    track_keys = {t.key for t in tracks}
    coverage = []
    for v in videos:
        within = sum(1 for t in tracks
                     if abs(t.duration - v.duration) <= tolerance)
        longer = sum(1 for t in tracks if t.duration >= v.duration)
        coverage.append({
            "video": v.path.name,
            "duration": round(v.duration, 2),
            "within_tolerance": within,
            "longer": longer,
            "total": len(tracks),
            "gap": within == 0,
        })

    known_heat = sorted(
        ((k, u) for k, u in uses.items() if k in track_keys),
        key=lambda kv: (-kv[1], kv[0]),
    )
    top = [{"track": Path(k).name, "uses": u} for k, u in known_heat[:5]]
    used_count = sum(1 for k in track_keys if uses.get(k, 0) > 0)
    orphans = sum(1 for k in uses if k not in track_keys)

    return {
        "workspace": workspace,
        "tolerance": tolerance,
        "counts": {
            "tracks": len(tracks),
            "videos": len(videos),
            "outputs": outputs,
            "manifests": manifests,
        },
        "coverage": coverage,
        "buckets": bucketize([t.duration for t in tracks]),
        "usage": {
            "top": top,
            "used_tracks": used_count,
            "total_tracks": len(tracks),
            "cold_tracks": len(tracks) - used_count,
            "orphan_ledger_entries": orphans,
        },
        "cache": {
            "loudness_ready": loudness_ready,
            "onsets_ready": onsets_ready,
            "stale_entries": stale_entries,
        },
    }


def format_text(report: dict) -> str:
    c = report["counts"]
    lines = [
        f"workspace: {report['workspace']}",
        f"library: {c['tracks']} tracks | inputs: {c['videos']} videos | "
        f"outputs: {c['outputs']} renders ({c['manifests']} manifests)",
        "",
        f"coverage (tolerance +/-{report['tolerance']:g}s):",
    ]
    for entry in report["coverage"]:
        marker = "   <-- GAP" if entry["gap"] else ""
        lines.append(
            f"  {entry['video']:<36} {entry['duration']:>7.1f}s   "
            f"within tol: {entry['within_tolerance']:>3}/{entry['total']:<3} "
            f"longer: {entry['longer']}/{entry['total']}{marker}"
        )
    if not report["coverage"]:
        lines.append("  (no input videos)")

    lines.append("")
    lines.append("library duration buckets:")
    peak = max((b["count"] for b in report["buckets"]), default=0) or 1
    for b in report["buckets"]:
        bar = "#" * max(round(b["count"] * 24 / peak), 1 if b["count"] else 0)
        lines.append(f"  {b['label']:<8} {bar} {b['count']}")

    u = report["usage"]
    lines.append("")
    if u["top"]:
        heat = ", ".join(f"{e['track']} x{e['uses']}" for e in u["top"])
        lines.append(f"usage heat (top): {heat}")
    else:
        lines.append("usage heat: nothing rendered yet")
    lines.append(f"cold pool: {u['cold_tracks']}/{u['total_tracks']} "
                 f"tracks never used")
    if u["orphan_ledger_entries"]:
        lines.append(f"note: {u['orphan_ledger_entries']} ledger entries "
                     f"reference deleted tracks")

    cache = report["cache"]
    total = max(c["tracks"], 1)
    lines.append("")
    lines.append(f"cache readiness: loudness {cache['loudness_ready']}/{total}"
                 f" | onsets {cache['onsets_ready']}/{total}"
                 + (f" | {cache['stale_entries']} stale entries"
                    if cache["stale_entries"] else ""))
    if cache["loudness_ready"] < total or cache["onsets_ready"] < total:
        lines.append("pre-warm hint: videomixer index --loudness --onsets")

    return "\n".join(lines)
