#!/usr/bin/env python3
"""Compare expected and actual shader outputs and emit metrics plus a diff heatmap.

Supports Netpbm images (P2/P3/P5/P6) without dependencies.
Supports other formats such as PNG when Pillow is available.
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import sys
from dataclasses import dataclass
from typing import Iterable

try:
    from PIL import Image  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    Image = None


@dataclass
class LoadedImage:
    width: int
    height: int
    channels: int
    pixels: list[tuple[int, ...]]


def _tokenize_netpbm(data: bytes) -> list[bytes]:
    tokens: list[bytes] = []
    current = bytearray()
    in_comment = False
    for byte in data:
        if in_comment:
            if byte in (10, 13):
                in_comment = False
            continue
        if byte == 35:
            if current:
                tokens.append(bytes(current))
                current.clear()
            in_comment = True
            continue
        if byte in b" \t\r\n":
            if current:
                tokens.append(bytes(current))
                current.clear()
            continue
        current.append(byte)
    if current:
        tokens.append(bytes(current))
    return tokens


def _load_netpbm(path: pathlib.Path) -> LoadedImage:
    data = path.read_bytes()
    magic = data[:2]
    if magic not in {b"P2", b"P3", b"P5", b"P6"}:
        raise ValueError(f"Unsupported Netpbm format in {path}")

    if magic in {b"P2", b"P3"}:
        tokens = _tokenize_netpbm(data)
        if len(tokens) < 4:
            raise ValueError(f"Malformed Netpbm header in {path}")
        kind = tokens[0].decode("ascii")
        width = int(tokens[1])
        height = int(tokens[2])
        max_value = int(tokens[3])
        if max_value <= 0:
            raise ValueError(f"Invalid max value in {path}")
        channels = 1 if kind == "P2" else 3
        expected_values = width * height * channels
        values = [int(token) for token in tokens[4:]]
        if len(values) != expected_values:
            raise ValueError(
                f"Expected {expected_values} samples in {path}, found {len(values)}"
            )
        pixels: list[tuple[int, ...]] = []
        for index in range(0, len(values), channels):
            sample = values[index : index + channels]
            scaled = tuple(int(round((value / max_value) * 255.0)) for value in sample)
            pixels.append(scaled)
        return LoadedImage(width=width, height=height, channels=channels, pixels=pixels)

    offset = 0
    header_parts = []
    line = bytearray()
    while len(header_parts) < 4 and offset < len(data):
        byte = data[offset]
        offset += 1
        if byte == 35:
            while offset < len(data) and data[offset] not in (10, 13):
                offset += 1
            continue
        if byte in b" \t\r\n":
            if line:
                header_parts.append(bytes(line))
                line.clear()
            continue
        line.append(byte)
    if line and len(header_parts) < 4:
        header_parts.append(bytes(line))
    if len(header_parts) != 4:
        raise ValueError(f"Malformed Netpbm binary header in {path}")

    kind = header_parts[0].decode("ascii")
    width = int(header_parts[1])
    height = int(header_parts[2])
    max_value = int(header_parts[3])
    if max_value <= 0 or max_value > 255:
        raise ValueError(f"Only max values up to 255 are supported in {path}")
    channels = 1 if kind == "P5" else 3
    sample_count = width * height * channels
    payload = data[offset:]
    if len(payload) < sample_count:
        raise ValueError(f"Expected {sample_count} bytes in {path}, found {len(payload)}")
    pixels: list[tuple[int, ...]] = []
    for index in range(0, sample_count, channels):
        pixels.append(tuple(payload[index : index + channels]))
    return LoadedImage(width=width, height=height, channels=channels, pixels=pixels)


def _load_with_pillow(path: pathlib.Path) -> LoadedImage:
    if Image is None:
        raise RuntimeError(
            "Pillow is required for this file format. Install it with `python -m pip install pillow`, "
            "or use a Netpbm image such as .ppm for dependency-free comparisons."
        )
    image = Image.open(path).convert("RGBA")
    pixels = list(image.getdata())
    return LoadedImage(width=image.width, height=image.height, channels=4, pixels=pixels)


def load_image(path: pathlib.Path) -> LoadedImage:
    suffix = path.suffix.lower()
    if suffix in {".pbm", ".pgm", ".ppm", ".pnm"}:
        return _load_netpbm(path)
    return _load_with_pillow(path)


def ensure_rgba(image: LoadedImage) -> LoadedImage:
    if image.channels == 4:
        return image
    pixels: list[tuple[int, ...]] = []
    for pixel in image.pixels:
        if image.channels == 1:
            gray = pixel[0]
            pixels.append((gray, gray, gray, 255))
        elif image.channels == 3:
            pixels.append((pixel[0], pixel[1], pixel[2], 255))
        else:
            raise ValueError(f"Unsupported channel count: {image.channels}")
    return LoadedImage(width=image.width, height=image.height, channels=4, pixels=pixels)


def save_diff(path: pathlib.Path, width: int, height: int, pixels: Iterable[tuple[int, int, int]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pixels_list = list(pixels)
    suffix = path.suffix.lower()
    if Image is not None and suffix not in {".ppm", ".pgm", ".pnm"}:
        image = Image.new("RGB", (width, height))
        image.putdata(pixels_list)
        image.save(path)
        return
    with path.open("w", encoding="utf-8") as handle:
        handle.write(f"P3\n{width} {height}\n255\n")
        for red, green, blue in pixels_list:
            handle.write(f"{red} {green} {blue}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected", required=True, type=pathlib.Path)
    parser.add_argument("--actual", required=True, type=pathlib.Path)
    parser.add_argument("--diff", required=True, type=pathlib.Path)
    parser.add_argument("--metrics", required=True, type=pathlib.Path)
    parser.add_argument("--tolerance", type=int, default=2, help="Per-channel tolerance in 0..255")
    parser.add_argument("--rmse-threshold", type=float, default=0.0)
    parser.add_argument(
        "--max-failing-pixels-ratio",
        type=float,
        default=0.0,
        help="Allowed ratio of pixels whose max channel diff exceeds tolerance",
    )
    args = parser.parse_args()

    expected = ensure_rgba(load_image(args.expected))
    actual = ensure_rgba(load_image(args.actual))

    if expected.width != actual.width or expected.height != actual.height:
        result = {
            "passed": False,
            "reason": "size-mismatch",
            "expected_size": [expected.width, expected.height],
            "actual_size": [actual.width, actual.height],
        }
        args.metrics.parent.mkdir(parents=True, exist_ok=True)
        args.metrics.write_text(json.dumps(result, indent=2), encoding="utf-8")
        return 1

    channel_error_sum = 0.0
    channel_error_max = 0
    failing_pixels = 0
    diff_pixels: list[tuple[int, int, int]] = []

    for expected_pixel, actual_pixel in zip(expected.pixels, actual.pixels):
        diffs = [abs(left - right) for left, right in zip(expected_pixel, actual_pixel)]
        pixel_max = max(diffs)
        channel_error_max = max(channel_error_max, pixel_max)
        channel_error_sum += sum(diff * diff for diff in diffs)
        if pixel_max > args.tolerance:
            failing_pixels += 1
        diff_pixels.append((pixel_max, min(255, pixel_max // 2), 0))

    total_pixels = expected.width * expected.height
    total_channels = total_pixels * 4
    rmse = math.sqrt(channel_error_sum / total_channels) if total_channels else 0.0
    failing_ratio = failing_pixels / total_pixels if total_pixels else 0.0
    passed = rmse <= args.rmse_threshold and failing_ratio <= args.max_failing_pixels_ratio

    result = {
        "passed": passed,
        "expected": str(args.expected),
        "actual": str(args.actual),
        "diff": str(args.diff),
        "expected_size": [expected.width, expected.height],
        "actual_size": [actual.width, actual.height],
        "tolerance": args.tolerance,
        "rmse": rmse,
        "rmse_threshold": args.rmse_threshold,
        "max_channel_diff": channel_error_max,
        "failing_pixels": failing_pixels,
        "failing_pixels_ratio": failing_ratio,
        "max_failing_pixels_ratio": args.max_failing_pixels_ratio,
    }

    args.metrics.parent.mkdir(parents=True, exist_ok=True)
    args.metrics.write_text(json.dumps(result, indent=2), encoding="utf-8")
    save_diff(args.diff, expected.width, expected.height, diff_pixels)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
