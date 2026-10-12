# videomixer

Lossless, automated audio-finishing for batch-produced videos. It layers
duration-matched background music and sound effects onto finished videos —
TTS narration included — in a **single ffmpeg pass that copies the video
stream untouched**: zero quality loss, near-instant processing.

## Design

A layered pipeline where each stage is independent and testable:

```
probe ──▶ select ──▶ design ──▶ render ──▶ report
```

| Stage | Module | Responsibility |
|-------|--------|----------------|
| probe | `src/videomixer/probe.py` | ffprobe wrapper + incremental cache (keyed on path+mtime+size, never re-probes unchanged files) |
| select | `src/videomixer/library.py` | scored track selection: closest-duration first, penalizes recently-used tracks so a channel's batch never repeats the same BGM |
| design | `src/videomixer/design.py` | pure, serializable *audio plan* (cues, gains, fades) — inspectable before rendering |
| render | `src/videomixer/render.py` | compiles the plan into one ffmpeg invocation (`-c:v copy`) |
| report | `src/videomixer/report.py` | writes a `.mix.json` provenance manifest next to every output |

Key properties:

- **Recipes, not code.** Styles are declarative TOML (`recipes/*.toml`).
  Adding a "podcast", "trailer" or any new style is a config block, not a
  code change.
- **Deterministic & reproducible.** Seedable RNG; the chosen seed is stored
  in every manifest, so any output can be regenerated exactly.
- **Idempotent by design.** Every mix plan gets a content fingerprint; a
  rerun with the same inputs skips identical work instead of duplicating
  outputs. Change any recipe parameter and only the affected mixes render.
- **Beat-aligned impacts.** SFX cues can snap to energy transients in the
  music bed (`snap = true`), so impacts land on actual hits — including
  across looped beds. Placement is cached per track and recorded in the
  manifest (requested time vs. snapped time).
- **Scene-aware multi-BGM.** Set `movement = 45` on a style and long videos
  rotate through distinct beds, crossfaded at boundaries that snap into
  detected voice pauses — music changes during silence, never over
  narration.
- **Real DSP, not static volume knobs:** broadcast-exact **two-pass EBU R128
  loudness normalization** (linear mode) on the BGM, optional sidechain
  ducking against the voice track, safety limiter on the final mix.
- **Uniform pipeline.** Silent videos get an `anullsrc` voice bus, so there
  is exactly one render path (the old split/amovie hacks are gone).
- **Short music? No dead air.** Tracks shorter than the video are looped
  seamlessly (`-stream_loop`), then trimmed and faded.
- **Zero dependencies.** Python ≥ 3.11 stdlib + an ffmpeg/ffprobe binary on
  PATH.
- **Safe under concurrency.** Shared state (usage ledger, probe cache) is
  written with atomic replace, merged with concurrent writers, and guarded
  by cross-platform advisory locks — running `watch` alongside manual `run`
  invocations cannot silently lose bookkeeping. A crashed process can never
  wedge a workspace (stale locks are stolen automatically).

## Layout

```
recipes/    style definitions (TOML)
src/        package source
inputs/     drop finished videos here
library/    BGM tracks (any length; loops if shorter than video)
sfx/        short effects (impacts, whooshes)
outputs/    rendered videos + .mix.json manifests
metadata/   probe cache + usage ledger (auto-maintained)
legacy/     archived v1 script, kept for reference
tools/      fixture generator for testing
```

## Usage

```bash
pip install -e .          # or run with: PYTHONPATH=src python -m videomixer

videomixer index                          # rebuild media probe cache
videomixer index --loudness               # pre-measure BGM loudness for exact normalization
videomixer index --onsets                 # pre-analyze BGM transients for beat-aligned SFX
videomixer run --seed 42                  # mix all inputs, deterministic
videomixer run --styles impact,cinematic  # subset of styles
videomixer run --jobs 8                   # render variants in parallel
videomixer run --dry-run                  # print the plan, render nothing
videomixer run --limit 5                  # first N videos only
videomixer watch                          # poll inputs/, auto-process new videos
videomixer stats                          # coverage gaps, buckets, usage heat
videomixer stats --json                   # machine-readable report
```

### Factory mode (`watch`)

`videomixer watch` polls `inputs/` on an interval and processes new or
changed videos as they arrive — drop a finished render in the folder and the
mixed version appears in `outputs/` moments later. Processed inputs are
tracked by mtime+size in `metadata/watch_state.json`, so nothing is ever
processed twice unless it actually changed. Files modified in the last 2
seconds are left for the next poll (never pick up half-written inputs).
Failed renders are retried up to 3 attempts per file revision, then parked
with a warning — touch the file to reset. A single watcher per workspace is
enforced by an advisory lock (`metadata/watch.lock`); Ctrl+C stops cleanly.

```bash
videomixer watch --interval 5 --jobs 4 --seed 1001
```

### Curation (`stats`)

`videomixer stats` reads the probe cache, inputs, ledger and outputs to
answer the questions that matter when curating a music library:

- **coverage** — per input video, how many tracks land within tolerance vs.
  force looping; videos with zero close matches are flagged `GAP` (buy/trim
  tracks for that duration)
- **duration buckets** — where the library is thin (e.g. nothing over 2min)
- **usage heat** — most-played beds and the never-used "cold pool"; a large
  cold pool means your rotation has room, zero means repetition risk
- **cache readiness** — loudness/onset pre-measurement coverage plus stale
  entries for deleted files

`--json` emits the same report for dashboards; `--tolerance` overrides the
recipe value for what-if checks.

### Loudness pipeline

Each BGM track's integrated loudness (LUFS) is measured once with ffmpeg's
`loudnorm` analyzer and cached in `metadata/probe_cache.json`. Renders then
use two-pass *linear* normalization — a constant gain derived from the
measured values, so the music bed hits the style's `loudness_lufs` target
within ~0.1 LU, with no dynamic pumping. Tracks are measured lazily on first
use; `index --loudness` pre-warms the whole library so batch runs never pay
measurement cost. The manifest records `normalization_mode`
(`linear`/`dynamic`/`off`), the target, and the measured source loudness.

### Onset analysis

For `snap = true` cues, each bed is decoded once and scanned for percussive
transients via an RMS-envelope novelty function with an absolute minimum
jump threshold (so MP3 ripple on flat tones yields zero false hits). Onset
times cache alongside loudness; `index --onsets` pre-analyzes the library.
When a bed loops across the video, transients repeat with the bed period.
If no transient lies within `snap_window` seconds of the desired time — or
the bed has none at all — the cue stays at its requested position.

### Movements (multi-BGM)

Styles with `movement = N` slice videos longer than 1.5×N into segments,
each scored and assigned a distinct track. Uniform boundaries are snapped
into voice silences (ffmpeg `silencedetect`, cached per video) so the
triangular crossfades ride inside pauses between sentences. Every segment
gets the same loudnorm/gain treatment; global fades bookend the whole bed.
The manifest lists each movement's track, span, and whether its boundary is
`gap_aligned`.

Every output lands in `outputs/` as
`<video>__<style>__<track>.mp4` plus a `.mix.json` manifest recording the
exact track, gains, cue timestamps, ducking settings and ffmpeg command used.

## Adding a style

```toml
[style.podcast]
bgm_gain_db = -20.0
loudness_lufs = -22.0
fade_in = 1.0
fade_out = 2.0

[style.podcast.duck]        # sidechain-compress BGM under the voice
enabled = true
threshold = 0.05
ratio = 8.0

[[style.podcast.sfx]]       # negative `at` counts from the video's end
pattern = "whoosh_*"
at = -0.8
gain_db = -10.0

[[style.trailer.sfx]]       # land the hit on an actual musical transient
pattern = "impact_*"
at = 0.0                    # desired position; snaps within ±0.6s of a hit
gain_db = -4.0
snap = true
snap_window = 0.6

[style.suite]               # long-form: rotate beds every ~45s
bgm_gain_db = -18.0
movement = 45               # 0 = off; videos ≤ 1.5× this stay single-bed
crossfade = 3.0             # crossfade length at each movement boundary

[style.suite.duck]
enabled = true
```

## Testing

141 tests, stdlib `unittest` only (no test framework dependency):

```bash
python -m unittest discover -s tests -t .
```

Unit tests cover recipe parsing and validation (invalid values are
rejected with clear messages), selection scoring/rotation, probe-cache
round-trips and concurrent-writer merges, lock acquisition/stale takeover,
watch-state bounded retries, onset detection on synthetic click tracks,
snap math, movement boundary planning (including pathological inputs) and
crossfade graph compilation, analytics math/formatting, and per-item
failure isolation in the render batch. The integration suite builds real
fixtures with ffmpeg and proves the full loop: render → manifest → skip on
rerun → re-render after recipe change, parallel `--jobs` execution,
watch-tick processing of new/changed inputs, stats reporting, and
multi-movement renders with exact durations. It self-skips when ffmpeg is
absent.

The fingerprint excludes the RNG seed but includes everything it influences
(track choice, cue placement, gains), so outputs are content-addressed:
identical plans never render twice.
