"""Provenance manifests and usage bookkeeping for every rendered output."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from videomixer import __version__
from videomixer.design import AudioDesign

MANIFEST_SUFFIX = ".mix.json"


def write_manifest(
    design: AudioDesign,
    output_path: Path,
    cmd: list[str],
    recipe_name: str,
    elapsed_s: float,
    fingerprint: str = "",
) -> Path:
    manifest_path = output_path.with_suffix("")
    manifest_path = manifest_path.with_suffix(manifest_path.suffix + MANIFEST_SUFFIX)
    doc = {
        "mixer_version": __version__,
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "recipe": recipe_name,
        "fingerprint": fingerprint or design.fingerprint(),
        "output": str(output_path),
        "elapsed_s": round(elapsed_s, 2),
        "ffmpeg_command": cmd,
        "design": design.to_dict(),
    }
    manifest_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return manifest_path
