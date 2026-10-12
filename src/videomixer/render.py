"""Compiles an AudioDesign into one ffmpeg stream-copy invocation."""

from __future__ import annotations

import subprocess
from pathlib import Path

from videomixer.config import Recipe
from videomixer.design import AudioDesign

SAMPLE_RATE = 48000
AFORMAT = f"aformat=sample_rates={SAMPLE_RATE}:channel_layouts=stereo"


def _db_to_linear(db: float) -> float:
    return 10.0 ** (db / 20.0)


def input_layout(design: AudioDesign) -> dict[str, int]:
    n_beds = len(design.movements) if design.multi_movement else 1
    layout = {"video": 0}
    for i in range(n_beds):
        layout[f"bgm{i}"] = 1 + i
    base = 1 + n_beds
    for i in range(len(design.sfx_cues)):
        layout[f"sfx{i}"] = base + i
    if not design.video.has_audio:
        layout["silence"] = base + len(design.sfx_cues)
    return layout


def _loudnorm_fragment(style, stats) -> str:
    loudnorm = f"loudnorm=I={style.loudness_lufs:g}:TP=-1.5:LRA=11"
    if stats is not None:
        loudnorm += (
            f":measured_I={stats.input_i:.2f}"
            f":measured_TP={stats.input_tp:.2f}"
            f":measured_LRA={stats.input_lra:.2f}"
            f":measured_thresh={stats.input_thresh:.2f}"
            f":offset={stats.target_offset:.2f}"
            f":linear=true"
        )
    return loudnorm + f",aresample={SAMPLE_RATE}"


def _single_bed_chain(design: AudioDesign) -> tuple[str, str]:
    dur = design.duration
    style = design.style
    chain = [f"[1:a]{AFORMAT}", f"atrim=duration={dur:.3f}"]
    if style.loudness_lufs is not None:
        chain.append(_loudnorm_fragment(style, design.track_loudness))
    chain.append(f"volume={_db_to_linear(style.bgm_gain_db):.6f}")
    fade_in = min(style.fade_in_s, dur / 3.0)
    fade_out = min(style.fade_out_s, dur / 3.0)
    if fade_in > 0:
        chain.append(f"afade=t=in:st=0:d={fade_in:.3f}")
    if fade_out > 0:
        st = max(0.0, dur - fade_out)
        chain.append(f"afade=t=out:st={st:.3f}:d={fade_out:.3f}")
    return ",".join(chain) + "[bgm]", "[bgm]"


def _movement_chains(design: AudioDesign) -> tuple[list[str], str]:
    """Per-movement trims + an acrossfade chain; global fades at the ends."""
    dur = design.duration
    style = design.style
    layout = input_layout(design)
    cf = min(max(style.crossfade_s, 0.05), dur / (2.0 * len(design.movements)))
    parts: list[str] = []
    fade_in = min(style.fade_in_s, dur / 3.0)
    fade_out = min(style.fade_out_s, dur / 3.0)

    for i, mv in enumerate(design.movements):
        end_tail = min(mv.end_s + cf, dur) if i < len(design.movements) - 1 else dur
        seg_len = end_tail - mv.start_s
        chain = [
            f"[{layout[f'bgm{i}']}:a]{AFORMAT}",
            f"atrim=start={mv.start_s:.3f}:end={end_tail:.3f},asetpts=N/SR/TB",
        ]
        if style.loudness_lufs is not None:
            chain.append(_loudnorm_fragment(style, mv.loudness))
        chain.append(f"volume={_db_to_linear(style.bgm_gain_db):.6f}")
        chain.append(f"apad,atrim=duration={seg_len:.3f}")
        parts.append(",".join(chain) + f"[seg{i}]")

    merged = "[seg0]"
    for i in range(1, len(design.movements)):
        out_label = f"mv{i}" if i < len(design.movements) - 1 else "mvfinal"
        parts.append(
            f"{merged}[seg{i}]acrossfade=d={cf:.3f}:c1=tri:c2=tri[{out_label}]"
        )
        merged = f"[{out_label}]"

    tail_fades = []
    if fade_in > 0:
        tail_fades.append(f"afade=t=in:st=0:d={fade_in:.3f}")
    if fade_out > 0:
        st = max(0.0, dur - fade_out)
        tail_fades.append(f"afade=t=out:st={st:.3f}:d={fade_out:.3f}")
    if tail_fades:
        parts.append(f"{merged}{','.join(tail_fades)}[musicbus]")
    else:
        parts.append(f"{merged}anull[musicbus]")
    return parts, "[musicbus]"


def build_filter_graph(design: AudioDesign) -> str:
    dur = design.duration
    style = design.style
    layout = input_layout(design)

    parts: list[str] = []

    if design.duck_active:
        parts.append(f"[0:a]{AFORMAT},asplit=2[voice][duckkey]")
    elif design.video.has_audio:
        parts.append(f"[0:a]{AFORMAT}[voice]")
    else:
        parts.append(f"[{layout['silence']}:a]{AFORMAT}[voice]")

    if design.multi_movement:
        mv_parts, music_src = _movement_chains(design)
        parts.extend(mv_parts)
    else:
        bed_part, music_src = _single_bed_chain(design)
        parts.append(bed_part)

    sfx_labels: list[str] = []
    for i, cue in enumerate(design.sfx_cues):
        ms = int(round(cue.at_s * 1000))
        chain = [
            f"[{layout[f'sfx{i}']}:a]{AFORMAT}",
            "atrim=duration=3.0",
            f"volume={_db_to_linear(cue.gain_db):.6f}",
            f"adelay=delays={ms}:all=1",
            f"apad,atrim=duration={dur:.3f}",
        ]
        label = f"sfx{i}"
        parts.append(",".join(chain) + f"[{label}]")
        sfx_labels.append(label)

    music_inputs = [music_src] + [f"[{label}]" for label in sfx_labels]
    if len(music_inputs) > 1:
        parts.append(
            "".join(music_inputs)
            + f"amix=inputs={len(music_inputs)}:normalize=0:duration=longest[musicpre]"
        )
        music_src = "[musicpre]"

    if design.duck_active:
        threshold = _db_to_linear(style.duck_threshold_db)
        parts.append(
            f"{music_src}[duckkey]sidechaincompress="
            f"threshold={threshold:.6f}:ratio={style.duck_ratio:g}"
            f":attack={style.duck_attack_ms}:release={style.duck_release_ms}"
            f":makeup=1[music]"
        )
    else:
        parts.append(f"{music_src}anull[music]")

    out_chain = [
        "[voice][music]amix=inputs=2:normalize=0:duration=longest",
        f"atrim=duration={dur:.3f}",
        "asetpts=N/SR/TB",
    ]
    if style.limiter:
        out_chain.append("alimiter=limit=0.9512:level=false")
    out_chain.append(AFORMAT + "[a]")
    parts.append(",".join(out_chain))

    return ";".join(parts)


def build_inputs(design: AudioDesign) -> list[str]:
    inputs: list[str] = ["-i", str(design.video.path)]

    def add_bed(loop: bool, path: Path) -> None:
        if loop:
            inputs.extend(["-stream_loop", "-1"])
        inputs.extend(["-i", str(path)])

    if design.multi_movement:
        for mv in design.movements:
            seg_len = max(0.1, mv.end_s - mv.start_s)
            add_bed(design.style.loop_bgm
                    and mv.track.duration < seg_len - 0.5, mv.track.path)
    else:
        add_bed(design.style.loop_bgm
                and design.track.duration < design.duration - 0.5,
                design.track.path)

    for cue in design.sfx_cues:
        inputs += ["-i", str(cue.path)]
    if not design.video.has_audio:
        inputs += [
            "-f", "lavfi", "-t", f"{design.duration:.3f}",
            "-i", f"anullsrc=r={SAMPLE_RATE}:cl=stereo",
        ]
    return inputs


def render_command(
    design: AudioDesign,
    recipe: Recipe,
    output_path: Path,
) -> list[str]:
    return [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        *build_inputs(design),
        "-filter_complex", build_filter_graph(design),
        "-map", "0:v", "-map", "[a]",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", recipe.audio_bitrate, "-ar", str(SAMPLE_RATE),
        "-movflags", "+faststart",
        "-shortest",
        str(output_path),
    ]


def run_render(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)
