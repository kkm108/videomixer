"""Command-line interface: run / plan / index."""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import sys
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from videomixer import __version__
from videomixer.config import Recipe, load_recipe
from videomixer.design import build_design
from videomixer.library import (
    Library,
    UsageLedger,
    VIDEO_EXTENSIONS,
    WatchState,
    select_tracks,
)
from videomixer.locks import LockTimeout, touch_lock, workspace_lock
from videomixer.probe import ProbeCache, probe_with_cache
from videomixer.render import render_command, run_render
from videomixer.report import write_manifest

DEFAULT_JOBS = min(4, os.cpu_count() or 1)
WATCH_STABLE_AGE_S = 2.0


def require_tools() -> int | None:
    missing = [tool for tool in ("ffmpeg", "ffprobe")
               if shutil.which(tool) is None]
    if missing:
        print(f"error: missing required tool(s): {', '.join(missing)}. "
              f"Install ffmpeg and make sure it is on PATH.", file=sys.stderr)
        return 2
    return None


def _load_recipe_or_fail(path: Path) -> Recipe | None:
    try:
        return load_recipe(path)
    except FileNotFoundError:
        print(f"error: recipe not found: {path}", file=sys.stderr)
    except tomllib.TOMLDecodeError as exc:
        print(f"error: invalid TOML in recipe {path}: {exc}", file=sys.stderr)
    except ValueError as exc:
        print(f"error: invalid recipe {path}: {exc}", file=sys.stderr)
    except OSError as exc:
        print(f"error: cannot read recipe {path}: {exc}", file=sys.stderr)
    return None


def default_recipe_path() -> Path:
    return Path(__file__).resolve().parents[2] / "recipes" / "default.toml"


def open_workspace(root: Path):
    for name in ("inputs", "library", "outputs", "sfx", "metadata"):
        (root / name).mkdir(parents=True, exist_ok=True)
    cache = ProbeCache(root / "metadata" / "probe_cache.json")
    library = Library(root / "library", cache)
    ledger = UsageLedger.load(root / "metadata" / "usage.json")
    return cache, library, ledger


def list_videos(root: Path, cache: ProbeCache, warn: bool = True) -> list:
    videos = []
    for f in sorted((root / "inputs").iterdir()):
        if not f.is_file() or f.suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        info = probe_with_cache(f, cache)
        if info is not None:
            videos.append(info)
        elif warn:
            print(f"warning: cannot decode {f.name}; skipped "
                  f"(is it a finished file?)", file=sys.stderr)
    return videos


def plan_batch(
    root: Path,
    recipe: Recipe,
    styles,
    seed: int | None,
    limit: int | None,
    force_index: bool,
    only_paths: set[str] | None = None,
):
    cache, library, ledger = open_workspace(root)
    tracks = library.scan(force=force_index)
    if not tracks:
        print(f"No audio tracks found in {root / 'library'}", file=sys.stderr)
        return None
    for style in styles:
        for spec in style.sfx_cues:
            if not library.sfx_pool(root / "sfx", spec.pattern):
                print(f"warning: style '{style.name}' expects SFX matching "
                      f"'{spec.pattern}' but none are in sfx/; cues skipped",
                      file=sys.stderr)
    videos = list_videos(root, cache, warn=(only_paths is None))
    if limit:
        videos = videos[:limit]
    if only_paths is not None:
        videos = [v for v in videos if v.key in only_paths]
        if not videos:
            return [], ledger, root, []
    rng = random.Random(seed)
    session = UsageLedger(path=ledger.path, uses=dict(ledger.uses))
    session_picks: list[str] = []
    plans = []
    for video in videos:
        picked = select_tracks(
            tracks, video.duration,
            count=min(recipe.versions_per_video, len(tracks)),
            ledger=session, rng=rng, tolerance_s=recipe.tolerance_s,
        )
        used: list[str] = []
        for i, track in enumerate(picked):
            style = styles[i % len(styles)]
            stats, measured = library.ensure_loudness(track)
            if measured:
                print(f"  loudness: measured {track.path.name} "
                      f"({stats.input_i:.1f} LUFS)")
            needs_onsets = any(c.snap for c in style.sfx_cues)
            if needs_onsets:
                onsets, analyzed = library.ensure_onsets(track)
                if analyzed:
                    print(f"  onsets: {len(onsets)} transients in "
                          f"{track.path.name}")
            design = build_design(video, track, style, library,
                                  root / "sfx", rng, tracks=tracks,
                                  session=session,
                                  tolerance_s=recipe.tolerance_s)
            design.track_loudness = stats
            design.seed = seed
            if design.multi_movement:
                names = ", ".join(m.track.path.name for m in design.movements)
                print(f"  movements: {len(design.movements)} beds ({names})")
            elif style.movement_s:
                reason = ("video too short"
                          if video.duration <= style.movement_s * 1.5
                          else "not enough distinct tracks")
                print(f"  warning: style '{style.name}' requested movements "
                      f"but fell back to a single bed ({reason})",
                      file=sys.stderr)
            plans.append(design)
            for key in design.all_track_keys():
                if key not in used:
                    used.append(key)
        session.record(used)
        session_picks.extend(used)
    cache.save()
    return plans, ledger, root, session_picks


def unique_output_path(outputs_dir: Path, stem: str, ext: str) -> Path:
    candidate = outputs_dir / f"{stem}{ext}"
    n = 2
    while candidate.exists():
        candidate = outputs_dir / f"{stem}_{n}{ext}"
        n += 1
    return candidate


def existing_fingerprints(outputs_dir: Path) -> dict[str, Path]:
    known: dict[str, Path] = {}
    if not outputs_dir.is_dir():
        return known
    for manifest in outputs_dir.glob("*.mix.json"):
        try:
            doc = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        fp = doc.get("fingerprint")
        out = doc.get("output")
        if fp and out and Path(out).exists():
            known[fp] = Path(out)
    return known


def _run_one(design, recipe, out, recipe_name):
    try:
        cmd = render_command(design, recipe, out)
        t0 = time.perf_counter()
        result = run_render(cmd)
        elapsed = time.perf_counter() - t0
        if result.returncode != 0:
            return ("fail", design, out, result.stderr.strip(), elapsed)
        write_manifest(design, out, cmd, recipe_name, elapsed,
                       design.fingerprint())
        return ("ok", design, out, "", elapsed)
    except Exception as exc:
        return ("fail", design, out, f"{type(exc).__name__}: {exc}", 0.0)


def execute_batch(
    root: Path,
    plans,
    ledger,
    session_picks,
    recipe,
    recipe_name: str,
    jobs: int = 1,
    seed=None,
) -> int:
    outputs_dir = root / "outputs"
    already_rendered = existing_fingerprints(outputs_dir)
    tasks = []
    skipped = 0
    for d in plans:
        fp = d.fingerprint()
        if fp in already_rendered:
            skipped += 1
            print(f"skip {already_rendered[fp].name} (fingerprint {fp})")
            continue
        out = unique_output_path(
            outputs_dir, f"{d.video.path.stem}__{d.style.name}__{d.track.path.stem}",
            d.video.path.suffix,
        )
        tasks.append((d, out))

    failures = 0

    def handle(result):
        nonlocal failures
        status, design, out, err, elapsed = result
        if status == "fail":
            failures += 1
            print(f"FAIL {out.name}: {err}", file=sys.stderr)
            return
        fields = [f"ok {out.name} ({elapsed:.1f}s)"]
        if design.duck_active:
            fields.append("+duck")
        if design.normalization_mode != "off":
            fields.append(f"norm={design.normalization_mode}")
        fields.append(f"seed={seed}")
        print(" ".join(fields))

    if jobs > 1 and len(tasks) > 1:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = [pool.submit(_run_one, d, recipe, out, recipe_name)
                       for d, out in tasks]
            for fut in as_completed(futures):
                handle(fut.result())
    else:
        for d, out in tasks:
            handle(_run_one(d, recipe, out, recipe_name))

    ledger.record(session_picks)
    total = len(plans)
    if skipped or len(tasks) > 1:
        print(f"{total} planned, {skipped} skipped (identical design), "
              f"{failures} failed")
    return failures


def resolve_seed(args_seed, recipe):
    return args_seed if args_seed is not None else (
        recipe.seed if recipe.seed is not None
        else random.SystemRandom().randrange(1 << 30))


def load_styles(recipe, styles_arg):
    names = styles_arg.split(",") if styles_arg else None
    selected = [s for s in recipe.styles if not names or s.name in names]
    return selected


def cmd_run(args) -> int:
    if (rc := require_tools()) is not None:
        return rc
    recipe_path = Path(args.recipe) if args.recipe else default_recipe_path()
    recipe = _load_recipe_or_fail(recipe_path)
    if recipe is None:
        return 1
    styles = load_styles(recipe, args.styles)
    if not styles:
        print("No matching styles in recipe.", file=sys.stderr)
        return 1
    seed = resolve_seed(args.seed, recipe)

    planned = plan_batch(Path(args.root), recipe, styles, seed, args.limit,
                         args.force_index)
    if planned is None:
        return 1
    plans, ledger, root, session_picks = planned
    if not plans:
        print("No input videos to process "
              "(inputs/ is empty or contains only unreadable files).")
        return 0

    if args.dry_run:
        print(f"plan (seed={seed}):")
        for d in plans:
            cues = ", ".join(f"{c.path.name}@{c.at_s:.2f}s" for c in d.sfx_cues) or "-"
            print(
                f"  {d.video.path.name} [{d.style.name}] <- {d.track.path.name}"
                f" ({d.track.duration:.1f}s) sfx: {cues} #{d.fingerprint()}"
            )
        return 0

    failures = execute_batch(root, plans, ledger, session_picks, recipe,
                             recipe_path.name,
                             jobs=(args.jobs or DEFAULT_JOBS), seed=seed)
    return 1 if failures else 0


def cmd_watch(args) -> int:
    if (rc := require_tools()) is not None:
        return rc
    recipe_path = Path(args.recipe) if args.recipe else default_recipe_path()
    recipe = _load_recipe_or_fail(recipe_path)
    if recipe is None:
        return 1
    styles = load_styles(recipe, args.styles)
    if not styles:
        print("No matching styles in recipe.", file=sys.stderr)
        return 1
    seed = resolve_seed(args.seed, recipe)
    root = Path(args.root)
    state = WatchState.load(root / "metadata" / "watch_state.json")
    jobs = args.jobs or DEFAULT_JOBS
    interval: float = float(args.interval)
    lock_path = root / "metadata" / "watch.lock"
    stale_s = max(120.0, interval * 3 + 30.0)
    try:
        with workspace_lock(lock_path, timeout_s=0.5, stale_s=stale_s):
            print(f"watching {(root / 'inputs').resolve()} every {interval:g}s "
                  f"(jobs={jobs}, seed={seed}, Ctrl+C to stop)")
            while True:
                touch_lock(lock_path)
                try:
                    _tick(root, recipe, styles, seed, jobs, state,
                          recipe_path.name)
                except Exception as exc:
                    print(f"watch tick error: {type(exc).__name__}: {exc}",
                          file=sys.stderr)
                time.sleep(interval)
    except LockTimeout:
        print("error: another watcher appears to be running for this "
              "workspace. Delete metadata/watch.lock if the previous watcher "
              "crashed.", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nwatch stopped")
    return 0


def _stable(video: Path, min_age_s: float = WATCH_STABLE_AGE_S) -> bool:
    try:
        return time.time() - video.stat().st_mtime >= min_age_s
    except OSError:
        return False


def _tick(root, recipe, styles, seed, jobs, state, recipe_name) -> int:
    inputs_dir = root / "inputs"
    candidates = [f for f in sorted(inputs_dir.iterdir())
                  if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS]
    pending = [p for p in state.pending(candidates) if _stable(p)]
    if not pending:
        return 0
    print(f"watch: {len(pending)} new/changed input(s): "
          f"{', '.join(p.name for p in pending)}")
    planned = plan_batch(root, recipe, styles, seed, None, False,
                         only_paths={p.resolve().as_posix() for p in pending})
    if planned is None:
        print("watch: library empty, will retry", file=sys.stderr)
        return 0
    plans, ledger, _, picks = planned
    processed = 0
    failures = 0
    if plans:
        failures = execute_batch(root, plans, ledger, picks, recipe,
                                 recipe_name, jobs=jobs, seed=seed)
        processed = len(plans) - failures
    planned_videos = {d.video.path.resolve().as_posix() for d in plans}
    for p in pending:
        if p.resolve().as_posix() not in planned_videos:
            print(f"watch: cannot plan a mix for {p.name} "
                  f"(unreadable input?); will retry up to "
                  f"{state.max_attempts} times", file=sys.stderr)
    for p in pending:
        if failures or p.resolve().as_posix() not in planned_videos:
            state.mark_failed(p)
        else:
            state.mark_success(p)
    state.save()
    return processed


def cmd_stats(args) -> int:
    if (rc := require_tools()) is not None:
        return rc
    from videomixer.stats import build_report, format_text

    root = Path(args.root)
    tolerance: float | None = args.tolerance
    if tolerance is None:
        recipe_path = Path(args.recipe) if args.recipe else default_recipe_path()
        try:
            tolerance = load_recipe(recipe_path).tolerance_s
        except (OSError, ValueError):
            print(f"warning: cannot read recipe {recipe_path}; "
                  f"using tolerance 1.5", file=sys.stderr)
            tolerance = 1.5

    cache, library, ledger = open_workspace(root)
    tracks = library.scan()
    videos = list_videos(root, cache)
    outputs_dir = root / "outputs"
    report = build_report(
        workspace=str(root.resolve()),
        tolerance=float(tolerance),
        tracks=tracks,
        videos=videos,
        uses=dict(ledger.uses),
        outputs=len(list(outputs_dir.glob("*.mp4"))),
        manifests=len(list(outputs_dir.glob("*.mix.json"))),
        loudness_ready=sum(1 for t in tracks if cache.get_loudness(t.path)),
        onsets_ready=sum(1 for t in tracks if cache.get_onsets(t.path) is not None),
        stale_entries=len(cache.stale_entries()),
    )
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(format_text(report))
    return 0


def cmd_index(args) -> int:
    if (rc := require_tools()) is not None:
        return rc
    cache, library, _ = open_workspace(Path(args.root))
    stale_before = len(cache.stale_entries())
    tracks = library.scan(force=True)
    measured = 0
    if getattr(args, "loudness", False):
        for track in tracks:
            _, was_measured = library.ensure_loudness(track)
            measured += 1 if was_measured else 0
        print(f"loudness: {measured} newly measured, "
              f"{len(tracks) - measured} already cached")
    if getattr(args, "onsets", False):
        analyzed_count = 0
        total_onsets = 0
        for track in tracks:
            onsets, fresh = library.ensure_onsets(track)
            if fresh:
                analyzed_count += 1
                total_onsets += len(onsets or [])
        print(f"onsets: {analyzed_count} tracks analyzed "
              f"({total_onsets} transients)")
    cache.save()
    if stale_before:
        print(f"stale cache entries removed: {stale_before}")
    print(f"indexed {len(tracks)} tracks -> {cache.cache_file}")
    return 0


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected an integer, got '{text}'")
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1 (got {value})")
    return value


def _positive_float(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a number, got '{text}'")
    if value <= 0:
        raise argparse.ArgumentTypeError(f"must be > 0 (got {value})")
    return value


def _nonnegative_float(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a number, got '{text}'")
    if value < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0 (got {value})")
    return value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="videomixer",
        description="Lossless automated BGM/SFX finishing for batch-produced videos.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--root", default=".", help="workspace root (inputs/, library/, ...)")
        p.add_argument("--recipe", help="path to recipe TOML (default: recipes/default.toml)")

    p_run = sub.add_parser("run", help="mix and write outputs")
    common(p_run)
    p_run.add_argument("--styles", help="comma-separated style filter")
    p_run.add_argument("--limit", type=_positive_int,
                       help="process only first N videos")
    p_run.add_argument("--seed", type=int, help="override RNG seed")
    p_run.add_argument("--dry-run", action="store_true", help="print plan without rendering")
    p_run.add_argument("--force-index", action="store_true", help="re-probe all media")
    p_run.add_argument("--jobs", type=_positive_int,
                       help=f"parallel renders (default {DEFAULT_JOBS})")
    p_run.set_defaults(func=cmd_run)

    p_watch = sub.add_parser("watch",
                             help="poll inputs/ and process new/changed videos")
    common(p_watch)
    p_watch.add_argument("--styles", help="comma-separated style filter")
    p_watch.add_argument("--seed", type=int, help="override RNG seed")
    p_watch.add_argument("--jobs", type=_positive_int,
                         help=f"parallel renders (default {DEFAULT_JOBS})")
    p_watch.add_argument("--interval", type=_positive_float, default=10.0,
                         help="seconds between polls (default 10)")
    p_watch.set_defaults(func=cmd_watch)

    p_idx = sub.add_parser("index", help="rebuild the media probe cache")
    common(p_idx)
    p_idx.add_argument("--loudness", action="store_true",
                       help="also pre-measure BGM loudness (enables exact linear normalization)")
    p_idx.add_argument("--onsets", action="store_true",
                       help="also pre-analyze BGM transients (enables beat-aligned SFX snapping)")
    p_idx.set_defaults(func=cmd_index)

    p_stats = sub.add_parser("stats",
                             help="coverage gaps, duration buckets, usage heat")
    common(p_stats)
    p_stats.add_argument("--json", action="store_true",
                         help="emit machine-readable JSON report")
    p_stats.add_argument("--tolerance", type=_nonnegative_float,
                         help="override match tolerance (default: recipe value)")
    p_stats.set_defaults(func=cmd_stats)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
