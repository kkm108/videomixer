"""Generate synthetic test fixtures (videos, BGM library, SFX) for a workspace."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


def run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip()[-500:])


def generate(root: Path) -> None:
    inputs = root / "inputs"
    library = root / "library"
    sfx = root / "sfx"
    for d in (inputs, library, sfx):
        d.mkdir(parents=True, exist_ok=True)

    run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc2=duration=10:size=640x360:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=10",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        str(inputs / "voiced_video_10s.mp4"),
    ])
    run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc2=duration=30:size=640x360:rate=30",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-an",
        str(inputs / "silent_video_30s.mp4"),
    ])

    for dur in (10, 15, 30, 45, 60):
        freq = 200 + dur * 10
        run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={dur}",
            "-c:a", "libmp3lame",
            str(library / f"bgm_{dur}s.mp3"),
        ])

    for i in (1, 2):
        run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"sine=frequency={800 * i}:duration=0.5",
            "-af", "afade=t=out:st=0.2:d=0.3",
            "-c:a", "libmp3lame",
            str(sfx / f"impact_{i}.mp3"),
        ])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="workspace root")
    args = parser.parse_args()
    generate(Path(args.root))
    print(f"fixtures generated under {Path(args.root).resolve()}")


if __name__ == "__main__":
    main()
