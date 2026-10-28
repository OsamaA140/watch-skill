#!/usr/bin/env python3
"""Editing pace: hard cuts, shot lengths and cuts per minute.

One cheap decode at thumbnail size with FFmpeg's scene score, independent of
which frames the report shows. FFmpeg's scene score is already a change of
change, so steady pans and zooms stay low; measured on synthetic clips, cuts
between similar-brightness flat shots scored 0.24-0.27, a caption change on a
static shot under 0.05, and a continuous zoom never reached 0.1. Cuts closer
than MIN_SHOT_SECONDS to the previous one (flashes, glitch transitions, the
noisy frames right after a cut) are merged into it.
"""
from __future__ import annotations

import json
import math
import statistics
import sys
from pathlib import Path

from runtime import configure_stdio, diagnostic, run_text

CUT_THRESHOLD = 0.22
MIN_SHOT_SECONDS = 0.25
ANALYSIS_WIDTH = 160
LIST_LIMIT = 60
# A full decode of a long video is slow; past this the report asks for --pacing.
AUTO_MAX_SECONDS = 1200


def _fmt(seconds: float) -> str:
    """MM:SS.s, keeping the tenths that short-form cuts need (HH:MM:SS past an hour)."""
    tenths = int(round(seconds * 10))
    minutes, rest = divmod(tenths, 600)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{rest // 10:02d}"
    return f"{minutes:02d}:{rest // 10:02d}.{rest % 10}"


def detect_cuts(video_path: str, start: float | None = None, end: float | None = None,
                threshold: float = CUT_THRESHOLD) -> list[float]:
    """Return absolute source times of hard cuts inside [start, end]."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "info", "-nostats"]
    if start:
        cmd += ["-ss", f"{start:.6f}"]
    if end is not None:
        cmd += ["-to", f"{end:.6f}"]
    cmd += ["-i", str(Path(video_path).resolve()), "-an", "-sn",
            "-vf", f"scale={ANALYSIS_WIDTH}:-2,select='gt(scene\\,{threshold})',showinfo",
            "-f", "null", "-"]
    result = run_text(cmd)
    if result.returncode != 0:
        raise SystemExit(f"ffmpeg cut detection failed: {diagnostic(result.stderr)}")
    offset = start or 0.0
    cuts: list[float] = []
    for line in result.stderr.splitlines():
        if "pts_time:" not in line:
            continue
        try:
            t = float(line.split("pts_time:", 1)[1].split()[0])
        except (IndexError, ValueError):
            continue
        if not math.isfinite(t):
            continue
        t = offset + t
        previous = cuts[-1] if cuts else offset
        if t - previous >= MIN_SHOT_SECONDS:
            cuts.append(round(t, 3))
    return cuts


def compute_pacing(cuts: list[float], range_start: float, range_end: float) -> dict:
    """Shot statistics for the analysed range. Shots = cuts + 1."""
    span = max(0.0, range_end - range_start)
    if span <= 0:
        return {"cuts": [], "cut_count": 0, "shot_count": 0, "cuts_per_minute": 0.0,
                "mean_shot": 0.0, "median_shot": 0.0, "longest_shot": 0.0,
                "longest_shot_start": range_start, "first_cut": None, "span": 0.0}
    inside = sorted(t for t in cuts if range_start < t < range_end)
    bounds = [range_start, *inside, range_end]
    shots = [b - a for a, b in zip(bounds, bounds[1:])]
    longest = max(range(len(shots)), key=shots.__getitem__)
    return {
        "cuts": inside,
        "cut_count": len(inside),
        "shot_count": len(shots),
        "cuts_per_minute": round(len(inside) / (span / 60.0), 1),
        "mean_shot": round(statistics.mean(shots), 1),
        "median_shot": round(statistics.median(shots), 1),
        "longest_shot": round(shots[longest], 1),
        "longest_shot_start": bounds[longest],
        "first_cut": inside[0] if inside else None,
        "span": span,
    }


def summary_line(p: dict) -> str:
    """One header line; batch.py copies it into its comparison table."""
    if not p["cut_count"]:
        return f"no hard cuts (one continuous shot of {p['span']:.1f}s)"
    cuts = "1 cut" if p["cut_count"] == 1 else f"{p['cut_count']} cuts"
    return (f"{cuts} / {p['shot_count']} shots, {p['cuts_per_minute']} cuts/min, "
            f"mean shot {p['mean_shot']}s, median {p['median_shot']}s, longest {p['longest_shot']}s "
            f"at {_fmt(p['longest_shot_start'])}, first cut at {_fmt(p['first_cut'])}")


def cut_list(p: dict) -> str:
    shown = [_fmt(t) for t in p["cuts"][:LIST_LIMIT]]
    more = len(p["cuts"]) - len(shown)
    return ", ".join(shown) + (f", … ({more} more)" if more > 0 else "")


if __name__ == "__main__":
    configure_stdio()
    if len(sys.argv) != 2:
        raise SystemExit("usage: pacing.py <video-path>")
    from frames import get_metadata
    duration = get_metadata(sys.argv[1])["duration_seconds"]
    found = detect_cuts(sys.argv[1])
    print(json.dumps(compute_pacing(found, 0.0, duration), indent=2))
