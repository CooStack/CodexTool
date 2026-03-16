#!/usr/bin/env python3
"""Validate a deterministic shader scene manifest."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys


REQUIRED_TOP_LEVEL = {
    "scene_id",
    "render_pass",
    "viewport",
    "time_seconds",
    "seed",
    "camera",
    "textures",
    "uniforms",
    "baseline",
}

FORBIDDEN_KEYS = {
    "use_real_time",
    "randomize",
    "auto_seed",
    "latest",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=pathlib.Path)
    parser.add_argument("--json", action="store_true", help="Emit machine-readable output")
    args = parser.parse_args()

    payload = json.loads(args.manifest.read_text(encoding="utf-8"))
    errors: list[str] = []
    warnings: list[str] = []

    missing = sorted(REQUIRED_TOP_LEVEL - payload.keys())
    if missing:
        errors.append(f"Missing top-level fields: {', '.join(missing)}")

    viewport = payload.get("viewport", {})
    for key in ("width", "height"):
        value = viewport.get(key)
        if not isinstance(value, int) or value <= 0:
            errors.append(f"viewport.{key} must be a positive integer")

    time_seconds = payload.get("time_seconds")
    if not isinstance(time_seconds, (int, float)):
        errors.append("time_seconds must be numeric")

    seed = payload.get("seed")
    if not isinstance(seed, int):
        errors.append("seed must be an integer")

    textures = payload.get("textures")
    if not isinstance(textures, list):
        errors.append("textures must be a list")
    else:
        for index, texture in enumerate(textures):
            if not isinstance(texture, dict):
                errors.append(f"textures[{index}] must be an object")
                continue
            for required in ("name", "path", "sampler"):
                if required not in texture:
                    errors.append(f"textures[{index}] is missing `{required}`")

    uniforms = payload.get("uniforms")
    if not isinstance(uniforms, dict):
        errors.append("uniforms must be an object")

    for forbidden in FORBIDDEN_KEYS:
        if forbidden in payload:
            errors.append(f"`{forbidden}` is not allowed in deterministic scene manifests")

    baseline = payload.get("baseline")
    if isinstance(baseline, str) and baseline.endswith("latest.png"):
        warnings.append("Baseline path ends with `latest.png`; use a stable variant name instead")

    result = {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "manifest": str(args.manifest),
    }

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        for warning in warnings:
            print(f"WARNING: {warning}")
        for error in errors:
            print(f"ERROR: {error}")
        if not errors:
            print(f"OK: {args.manifest}")

    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
