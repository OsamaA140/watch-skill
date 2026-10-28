#!/usr/bin/env python3
"""/watch entry point: download video, extract frames, parse transcript.

Prints a markdown report to stdout listing frame paths + transcript. Claude
then Reads each frame path to see the video.
"""
from __future__ import annotations

import argparse
import math
import shutil
import sys
import tempfile
from pathlib import Path


SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))

from config import ConfigError, frame_cap, get_config, load_gemini_key, resolve_engine, site_cookie_for, validate_language  # noqa: E402
import gemini  # noqa: E402
from download import download, fetch_captions, is_url, auth_args  # noqa: E402
from frames import MAX_FPS, auto_fps, auto_fps_focus, dedupe_perceptual, extract, extract_at_timestamps, extract_keyframes, extract_scene_or_uniform, format_time, get_metadata, merge_frames, parse_time, parse_timestamps, validate_controls  # noqa: E402
import pacing  # noqa: E402
from transcribe import filter_range, format_transcript, parse_vtt  # noqa: E402
from whisper import load_api_key, transcribe_video  # noqa: E402
from runtime import configure_stdio  # noqa: E402


LOCAL_ONLY_FLAGS = (("--detail", "detail"), ("--fps", "fps"), ("--max-frames", "max_frames"),
                    ("--timestamps", "timestamps"), ("--whisper", "whisper"), ("--sub-lang", "sub_lang"),
                    ("--resolution", "resolution"), ("--language", "language"), ("--hook", "hook"),
                    ("--pacing/--no-pacing", "pacing"), ("--prompt", "prompt"))
HOOK_FPS = 2.0
HOOK_MAX_SECONDS = 30.0

ARABIC_LETTERS = range(0x0621, 0x064B)
URDU_ONLY = set('ٹڈڑںےۓ')
PERSIAN_HINTS = set('یک')  # U+06CC, U+06A9: Persian/Urdu forms, not Arabic.


def script_language_hint(text: str | None) -> str | None:
    """Guess a spoken-language hint from a title written mostly in Arabic script.

    Only a hint for local WhisperX, whose small model misidentifies non-English
    speech without one. Urdu-only letters mean ur; Persian forms without them
    mean fa; otherwise Arabic script means ar. Latin-dominant text gives None.
    """
    if not text:
        return None
    arabic_script = sum(1 for ch in text if 0x0600 <= ord(ch) <= 0x06FF)
    latin = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    if arabic_script < 6 or arabic_script <= latin:
        return None
    if any(ch in URDU_ONLY for ch in text):
        return 'ur'
    if any(ch in PERSIAN_HINTS for ch in text):
        return 'fa'
    return 'ar' if any(ord(ch) in ARABIC_LETTERS for ch in text) else None


def resolve_language(cli_value, config, info, backend):
    """Return (code, origin) for the spoken-language hint, or (None, None).

    Order: --language, WATCH_LANGUAGE, the saved WhisperX hint (WhisperX only),
    the video's own metadata language, then (WhisperX only) the title's script.
    A caption translation request (--sub-lang) is never used.
    """
    if cli_value:
        return cli_value, '--language'
    if config.get('language'):
        return config['language'], 'WATCH_LANGUAGE'
    if backend == 'whisperx' and config.get('whisperx_language'):
        return config['whisperx_language'], 'WATCH_WHISPERX_LANGUAGE'
    try:
        meta_language = validate_language(str((info or {}).get('language') or '').split('-')[0])
    except ConfigError:
        meta_language = None
    if meta_language:
        return meta_language, 'video metadata'
    if backend == 'whisperx':
        guess = script_language_hint((info or {}).get('title'))
        if guess:
            return guess, 'title script, unverified'
    return None, None


def resolve_auth(source, args, config):
    """CLI cookies > a WATCH_SITE_COOKIES rule for this host > global saved cookies."""
    if args.cookies or args.cookies_from_browser:
        return {"cookies_file": args.cookies, "cookies_from_browser": args.cookies_from_browser}, "command line"
    if is_url(source):
        site = site_cookie_for(source, config.get("site_cookies") or [])
        if site:
            return site, "site rule"
    if config["cookies_file"] or config["cookies_from_browser"]:
        if config["cookies_file"] and config["cookies_from_browser"]:
            raise SystemExit("Choose either WATCH_COOKIES_FILE or WATCH_COOKIES_FROM_BROWSER, not both.")
        return {"cookies_file": config["cookies_file"] or None,
                "cookies_from_browser": config["cookies_from_browser"] or None}, "saved setting"
    return {"cookies_file": None, "cookies_from_browser": None}, None


def _fmt_count(value) -> str:
    return f"{value:,}" if isinstance(value, int) else str(value)


def _fmt_date(value) -> str:
    return f"{value[:4]}-{value[4:6]}-{value[6:]}" if isinstance(value, str) and len(value) == 8 and value.isdigit() else str(value)


def run_gemini(args, config, key, start_sec, end_sec, auth) -> int:
    """Google watches the video. No captions, frames, or Whisper; no fallback on failure."""
    ignored = [flag for flag, name in LOCAL_ONLY_FLAGS if getattr(args, name) is not None]
    ignored += [flag for flag, on in (("--no-whisper", args.no_whisper), ("--no-dedup", args.no_dedup)) if on]
    clip = (start_sec, end_sec) if start_sec is not None or end_sec is not None else None
    uploaded, warning, result, error, sent, work = None, None, None, None, "URL sent to Google", None
    try:
        if gemini.is_youtube(args.source):
            video = {"uri": args.source}
        else:
            parent = Path(args.out_dir).expanduser().resolve() if args.out_dir else None
            if parent:
                parent.mkdir(parents=True, exist_ok=True)
            sent = "not sent to Google"
            work = Path(tempfile.mkdtemp(prefix="watch-", dir=parent))
            print("[watch] downloading media…" if is_url(args.source) else "[watch] using local file…", file=sys.stderr)
            media = download(args.source, work / "download", **(auth if is_url(args.source) else {}))
            sent = "upload to Google failed"
            print("[watch] uploading to the Gemini Files API…", file=sys.stderr)
            uploaded = gemini.upload_file(Path(media["video_path"]), key)
            video = {"uri": uploaded["uri"], "mime_type": uploaded["mime_type"]}
            sent = "video uploaded to Google, deleted after the answer"
        print(f"[watch] asking {config['gemini_model']}…", file=sys.stderr)
        result = gemini.ask(video, args.question, model=config["gemini_model"], key=key,
                            clip=clip, timeout=config["gemini_timeout"])
    except SystemExit as exc:
        error = str(exc)
    finally:
        if uploaded:
            warning = gemini.delete_file(uploaded["name"], key)
        if work:  # Run-owned; holds at most a downloaded copy. A local source file lives elsewhere.
            shutil.rmtree(work, ignore_errors=True)

    print()
    print("# watch: video report")
    print()
    print(f"- **Source:** {args.source} ({sent})")
    print(f"- **Engine:** {config['gemini_model']} ({result['processing'] if result else 'failed'})")
    if clip:
        print(f"- **Focus range:** {format_time(start_sec or 0)} → {format_time(end_sec) if end_sec is not None else 'end'}")
    if ignored:
        print(f"- **Ignored local options:** {', '.join(ignored)} (these only apply with --engine local)")
    if result and result["total_tokens"] is not None:
        print(f"- **Gemini tokens:** {result['total_tokens']}")
    if warning:
        print(f"- **Cleanup warning:** {warning}")
    print()
    if error:
        print("## Unavailable evidence")
        print()
        print(f"- {error}")
        return 1
    print("## Answer (from Gemini)")
    print()
    print("_These are Gemini's observations of the video, not frames you viewed yourself. "
          "Relay them as such; rerun with `--engine local` to inspect frames directly._")
    print()
    print(result["text"])
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="watch",
        description="Download a video, extract auto-scaled frames, and surface the transcript.",
    )
    ap.add_argument("source", help="Video URL or local file path")
    ap.add_argument("--max-frames", type=int, default=None, help="Override frame cap")
    ap.add_argument("--resolution", type=int, default=None,
                    help="Frame width in pixels (default 512, or 640 for vertical video; "
                         "WATCH_RESOLUTION / WATCH_PORTRAIT_RESOLUTION change the defaults)")
    ap.add_argument("--fps", type=float, default=None, help="Override auto-fps")
    ap.add_argument(
        "--detail",
        choices=["transcript", "efficient", "balanced", "token-burner"],
        default=None,
        help="Fidelity/speed dial: transcript (no frames), efficient (fast keyframes, cap 50), "
             "balanced (scene, cap 100), token-burner (scene, uncapped).",
    )
    ap.add_argument(
        "--timestamps",
        type=str,
        default=None,
        help="Comma-separated absolute timestamps (SS, MM:SS, HH:MM:SS) to grab a frame at, "
             "e.g. transcript-flagged 'look here' moments. Added on top of the detail frames "
             "(reserved against the cap); with --detail transcript these become the only frames.",
    )
    ap.add_argument("--start", type=str, default=None, help="Range start (SS, MM:SS, or HH:MM:SS)")
    ap.add_argument("--end", type=str, default=None, help="Range end (SS, MM:SS, or HH:MM:SS)")
    ap.add_argument("--out-dir", type=str, default=None, help="Working directory (default: tmp)")
    ap.add_argument(
        "--no-whisper",
        action="store_true",
        help="Disable all local/cloud transcription fallbacks; native captions still work.",
    )
    ap.add_argument(
        "--whisper",
        choices=["groq", "openai", "whisperx", "onnx"],
        default=None,
        help="Select the fallback backend for this run (onnx = local Whisper turbo, no key); "
             "native captions still come first.",
    )
    ap.add_argument(
        "--no-dedup",
        action="store_true",
        help="Disable near-duplicate frame removal. Keeps visually identical "
             "frames (static screen recordings, held slides) instead of collapsing them.",
    )
    ap.add_argument("--sub-lang", default=None, help="Exact caption language preference (default auto/native)")
    ap.add_argument("--captions", default=None,
                    help="Caption file (.vtt/.srt) for a local video; name.vtt/name.srt next to the video is found automatically")
    ap.add_argument("--info-json", default=None,
                    help="yt-dlp-style metadata JSON for a local video (title, uploader, view_count, …); "
                         "name.info.json next to the video is found automatically")
    ap.add_argument("--language", default=None,
                    help="Spoken-language hint for speech transcription, e.g. ar, en, ur (default: "
                         "WATCH_LANGUAGE, then the video's metadata)")
    cookies = ap.add_mutually_exclusive_group()
    cookies.add_argument("--cookies", default=None, help="Explicit cookie file (yt-dlp may update this jar)")
    cookies.add_argument("--cookies-from-browser", default=None, help="Explicit yt-dlp browser selector")
    ap.add_argument("--hook", type=float, nargs="?", const=10.0, default=None, metavar="SECONDS",
                    help="Hook microscope: extra frames at 2 fps over the opening SECONDS (default 10, max 30), "
                         "listed with the speech in that window.")
    ap.add_argument("--pacing", action=argparse.BooleanOptionalAction, default=None,
                    help="Editing pace (cuts, shot lengths, cuts/min). Default on with frames for ranges up to "
                         "20 minutes; --pacing forces it (also with transcript detail), --no-pacing skips it.")
    ap.add_argument("--prompt", default=None,
                    help="Vocabulary hint for speech fallback: names, brands, places and terms said in the video. "
                         "Used by groq, openai and whisperx; the onnx backend ignores it.")
    ap.add_argument("--engine", choices=["auto", "gemini", "local"], default=None,
                    help="gemini: Google watches the video and answers (needs GEMINI_API_KEY). "
                         "local: frames + transcript on this machine. Default auto: gemini when a key exists.")
    ap.add_argument("--question", default=None,
                    help="The user's question. Sent to Gemini on a gemini run; unused by the local engine.")
    args = ap.parse_args()
    if args.no_whisper and args.whisper:
        ap.error("--no-whisper conflicts with --whisper")
    if args.hook is not None and not (0 < args.hook <= HOOK_MAX_SECONDS):
        ap.error(f"--hook must be between 0 and {HOOK_MAX_SECONDS:g} seconds")
    if args.prompt is not None:
        args.prompt = " ".join(args.prompt.split())[:600] or None

    config = get_config(backend_override="none" if args.no_whisper else args.whisper)
    detail = args.detail or config["detail"]
    max_frames = args.max_frames if args.max_frames is not None else frame_cap(detail)
    budget_cap = max_frames if max_frames is not None else 100
    start_sec, end_sec = parse_time(args.start), parse_time(args.end)
    validate_controls(args.resolution or config["resolution"], max_frames, start_sec, end_sec, args.fps)
    args.language = validate_language(args.language, "--language")
    cue_timestamps = parse_timestamps(args.timestamps)
    backend_choice = "none" if args.no_whisper else args.whisper or config["whisper_backend"]
    auth, auth_origin = resolve_auth(args.source, args, config)
    auth_args(auth["cookies_file"], auth["cookies_from_browser"])  # Validate even before starting network work.
    if auth_origin and is_url(args.source):
        label = "browser login" if auth["cookies_from_browser"] else "cookie file"
        print(f"[watch] using {label} for this site ({auth_origin})", file=sys.stderr)

    gemini_key = load_gemini_key()
    if resolve_engine(args.engine or config["engine"], bool(gemini_key)) == "gemini":
        return run_gemini(args, config, gemini_key, start_sec, end_sec, auth)

    parent = Path(args.out_dir).expanduser().resolve() if args.out_dir else None
    if parent:
        parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="watch-", dir=parent))
    print(f"[watch] working dir: {work}", file=sys.stderr)
    url_source = is_url(args.source)
    dl = {"subtitle_path": None, "info": {}, "downloaded": False}
    errors = []
    all_segments = []
    track_available = False
    transcript_source = None
    video_path = None
    visual_error = None
    gaps = []

    if url_source:
        print("[watch] checking metadata/captions via yt-dlp…", file=sys.stderr)
        dl = fetch_captions(args.source, work / "download", sub_lang=args.sub_lang or config["sub_lang"], **auth)
        errors.extend(dl.get("errors", []))
        if dl.get("subtitle_path"):
            try:
                all_segments = parse_vtt(dl["subtitle_path"])
                track_available = bool(all_segments)
                track = dl.get("caption_track") or {}
                transcript_source = (f"captions ({track.get('language', 'unknown')}, "
                                     f"{track.get('kind', 'unknown')}, {track.get('provenance', 'unknown')})")
            except (OSError, ValueError) as exc:
                errors.append(f"Caption parsing failed: {exc}")

    audio_only = detail == "transcript" and not cue_timestamps
    # Captions are assessed before range filtering: a quiet interval is not a missing track.
    need_media = not (audio_only and (track_available or backend_choice == "none") and url_source)
    if need_media:
        try:
            print("[watch] downloading media…" if url_source else "[watch] using local file…", file=sys.stderr)
            media = download(args.source, work / "download", audio_only=audio_only,
                             **({"context": dl, **auth} if url_source else
                                {"info_json": args.info_json, "captions": args.captions}))
            dl.update(media)
            video_path = dl["video_path"]
            if not url_source and dl.get("subtitle_path") and not track_available:
                try:
                    all_segments = parse_vtt(dl["subtitle_path"])
                    track_available = bool(all_segments)
                    transcript_source = f"captions (file {Path(dl['subtitle_path']).name})"
                except (OSError, ValueError, UnicodeError) as exc:
                    errors.append(f"Caption file unreadable: {exc}")
        except SystemExit as exc:
            visual_error = f"Media unavailable: {exc}"
            errors.append(visual_error)

    try:
        duration = float((dl.get("info") or {}).get("duration") or 0)
        if not math.isfinite(duration) or duration < 0:
            duration = 0.0
    except (TypeError, ValueError):
        duration = 0.0
    meta = {"duration_seconds": duration, "width": None, "height": None, "codec": None,
            "has_audio": False, "has_video": False}
    if video_path:
        try:
            meta = get_metadata(video_path)
        except SystemExit as exc:
            visual_error = f"Visuals/audio metadata unavailable: {exc}"
            errors.append(visual_error)
    full_duration = meta["duration_seconds"]
    portrait = bool(meta.get("width") and meta.get("height") and meta["height"] > meta["width"])
    resolution = args.resolution or (config["portrait_resolution"] if portrait else config["resolution"])
    validate_controls(resolution, max_frames, start_sec, end_sec, args.fps)
    if full_duration > 0 and start_sec is not None and start_sec >= full_duration:
        raise SystemExit(f"--start {start_sec:.1f}s is past end of video ({full_duration:.1f}s)")
    effective_start = start_sec or 0.0
    effective_end = min(end_sec, full_duration) if end_sec is not None and full_duration > 0 else end_sec or full_duration
    effective_duration = max(0.0, effective_end - effective_start)
    focused = start_sec is not None or end_sec is not None
    if focused:
        fps, target = auto_fps_focus(effective_duration, max_frames=budget_cap)
    else:
        fps, target = auto_fps(effective_duration, max_frames=budget_cap)
    if args.fps is not None:
        fps = min(args.fps, MAX_FPS)
        target = max(1, int(round(fps * effective_duration)))

    frames, cue_frames = [], []
    frame_meta = {"engine": "none", "candidate_count": 0, "selected_count": 0, "fallback": False}
    cue_meta = {}
    detail_budget = max_frames
    if video_path and meta.get("has_video", bool(meta.get("width"))):
        try:
            if cue_timestamps:
                cue_frames, cue_meta = extract_at_timestamps(
                    video_path, work / "frames", cue_timestamps, resolution=resolution,
                    max_frames=max_frames, start_seconds=start_sec, end_seconds=effective_end or end_sec)
            detail_budget = None if max_frames is None else max_frames - len(cue_frames)
            if detail != "transcript" and detail_budget != 0:
                kwargs = dict(resolution=resolution, max_frames=detail_budget,
                              start_seconds=start_sec, end_seconds=effective_end or end_sec, dedup=not args.no_dedup)
                if detail == "efficient":
                    frames, frame_meta = extract_keyframes(video_path, work / "frames", **kwargs)
                else:
                    frames, frame_meta = extract_scene_or_uniform(video_path, work / "frames", fps=fps, target_frames=target, **kwargs)
        except SystemExit as exc:
            visual_error = f"Visual extraction unavailable: {exc}"
            errors.append(visual_error)
    elif video_path and not visual_error and (detail != "transcript" or cue_timestamps):
        visual_error = "No video stream; visual evidence is unavailable."
    if cue_frames:
        frames = merge_frames(frames, cue_frames)

    has_video = bool(video_path and meta.get("has_video", bool(meta.get("width"))))
    pace, pace_note = None, None
    pacing_wanted = args.pacing if args.pacing is not None else (
        detail != "transcript" and effective_duration <= pacing.AUTO_MAX_SECONDS)
    if has_video and pacing_wanted and effective_duration > 0:
        try:
            print("[watch] measuring editing pace…", file=sys.stderr)
            cuts = pacing.detect_cuts(video_path, start_sec, effective_end or None)
            pace = pacing.compute_pacing(cuts, effective_start, effective_end)
        except SystemExit as exc:
            pace_note = f"unavailable: {exc}"
    elif has_video and args.pacing is None and detail != "transcript":
        pace_note = f"skipped for a range over {pacing.AUTO_MAX_SECONDS // 60} minutes; rerun with --pacing"

    hook_frames, hook_end, hook_dropped = [], None, 0
    if has_video and args.hook:
        hook_end = min(args.hook, full_duration) if full_duration > 0 else args.hook
        try:
            hook_frames = extract(video_path, work / "hook", fps=HOOK_FPS, resolution=resolution,
                                  max_frames=int(math.ceil(hook_end * HOOK_FPS)) + 1,
                                  start_seconds=0.0, end_seconds=hook_end)
            if not args.no_dedup:
                hook_frames, hook_dropped = dedupe_perceptual(hook_frames)
        except SystemExit as exc:
            errors.append(f"Hook frames unavailable: {exc}")
    elif args.hook and video_path is None and not visual_error:
        errors.append("Hook frames need the video itself; none was downloaded.")

    transcript_state = "missing"
    language, language_origin = None, None
    if not track_available and backend_choice != "none" and video_path and meta.get("has_audio"):
        backend, api_key = (backend_choice, None) if backend_choice in ("whisperx", "onnx") else load_api_key(None if backend_choice == "auto" else backend_choice)
        if backend:
            language, language_origin = resolve_language(args.language, config, dl.get("info"), backend)
            extra = {"language": language} if language else {}
            if args.prompt:
                extra["prompt"] = args.prompt
                if backend == "onnx":
                    print("[watch] the onnx backend has no vocabulary prompt; --prompt ignored", file=sys.stderr)
            try:
                all_segments, used_backend = transcribe_video(video_path, work / "audio.mp3", backend=backend,
                                                              api_key=api_key, **extra)
                gaps = getattr(all_segments, "gaps", [])
                track_available = True
                transcript_state = "no speech" if not all_segments else "available"
                hint = f"{language} (from {language_origin})" if language else None
                if used_backend == "whisperx":
                    transcript_source = f"whisper (whisperx {config['whisperx_model']}, language {hint or 'auto (unverified)'})"
                elif used_backend == "onnx":
                    detected = getattr(all_segments, "language", None)
                    label = hint or (f"{detected} (auto-detected)" if detected else "auto-detected")
                    transcript_source = f"whisper ({config['onnx_model']}, local onnx, language {label})"
                    filtered = getattr(all_segments, "filtered", 0)
                    if filtered:
                        transcript_source += f"; {filtered} likely non-speech piece{'s' if filtered != 1 else ''} (music/noise) dropped"
                else:
                    transcript_source = f"whisper ({used_backend}, language {hint or 'auto-detected'})"
                if args.prompt and used_backend != "onnx":
                    transcript_source += "; vocabulary prompt used"
            except SystemExit as exc:
                transcript_state = "failed"
                errors.append(f"Transcription failed: {exc}")
        else:
            transcript_state = f"unavailable: no matching API key for {backend_choice}"
    elif not track_available and backend_choice == "none":
        transcript_state = "fallback disabled; no captions available"
    elif not track_available and video_path and not meta.get("has_audio"):
        transcript_state = "no audio stream" if not visual_error else "audio metadata unavailable"

    transcript_segments = filter_range(all_segments, start_sec, end_sec) if focused else all_segments
    transcript_text = format_transcript(transcript_segments)
    if track_available:
        transcript_state = "available" if transcript_segments else ("no speech in selected range" if focused and all_segments else "no speech")
        dropped = getattr(all_segments, "filtered", 0)
        if not transcript_segments and dropped:
            transcript_state += f" ({dropped} music/noise piece{'s' if dropped != 1 else ''} ignored)"
    for error in errors:
        print(f"[watch] {error}", file=sys.stderr)

    info = dl.get("info") or {}

    print()
    print("# watch: video report")
    print()
    print(f"- **Source:** {args.source}")
    if video_path:
        print(f"- **Local media:** `{video_path}`")
    if visual_error:
        print(f"- **Visual status:** {visual_error}")
    if errors:
        print("- **Result:** partial evidence" if frames or transcript_segments else "- **Result:** unavailable evidence")
    if info.get("title"):
        print(f"- **Title:** {info['title']}")
    if info.get("uploader"):
        handle = info.get("uploader_id")
        extra_handle = f" (@{handle.lstrip('@')})" if handle and str(handle).lstrip('@') != str(info['uploader']).lstrip('@') else ""
        print(f"- **Uploader:** {info['uploader']}{extra_handle}")
    if info.get("upload_date"):
        print(f"- **Upload date:** {_fmt_date(info['upload_date'])}")
    stats = [(label, info.get(key)) for label, key in (("views", "view_count"), ("likes", "like_count"),
                                                         ("comments", "comment_count"), ("shares", "repost_count"))]
    stats = [f"{_fmt_count(value)} {label}" for label, value in stats if value is not None]
    if stats:
        print(f"- **Engagement:** {', '.join(stats)} (at fetch time)")
    if info.get("description"):
        print(f"- **Description:** {info['description']}")
    print(f"- **Duration:** {format_time(full_duration)} ({full_duration:.1f}s)")
    if focused:
        print(
            f"- **Focus range:** {format_time(effective_start)} → {format_time(effective_end)} "
            f"({effective_duration:.1f}s)"
        )
    if meta.get("width") and meta.get("height"):
        print(f"- **Resolution:** {meta['width']}x{meta['height']} ({meta.get('codec') or 'unknown codec'})")
    range_mode = "focused" if focused else "full"
    print(f"- **Detail:** {detail}")
    detail_count = frame_meta.get("selected_count", 0)
    if detail != "transcript":
        cap_label = "unlimited" if detail_budget is None else str(detail_budget)
        engine = frame_meta.get("engine", "scene")
        fallback = " fallback" if frame_meta.get("fallback") else ""
        deduped = frame_meta.get("deduped_count", 0)
        dedup_note = f", {deduped} near-duplicate{'s' if deduped != 1 else ''} dropped" if deduped else ""
        print(
            f"- **Frames:** {detail_count} selected from {frame_meta.get('candidate_count', detail_count)} "
            f"candidates ({engine}{fallback}{dedup_note}, {range_mode} range, budget {target}, cap {cap_label})"
        )
    elif not cue_frames:
        print("- **Frames:** skipped (transcript detail)")
    if cue_frames:
        dropped = cue_meta.get("dropped_out_of_window", 0)
        drop_note = f", {dropped} dropped outside range" if dropped else ""
        print(
            f"- **Cue frames:** {len(cue_frames)} at transcript-flagged timestamps "
            f"(transcript-cue{drop_note})"
        )
    if hook_frames:
        hook_note = f", {hook_dropped} near-duplicate{'s' if hook_dropped != 1 else ''} dropped" if hook_dropped else ""
        print(f"- **Hook frames:** {len(hook_frames)} over 00:00 → {format_time(hook_end)} "
              f"({HOOK_FPS:g} fps{hook_note})")
    if pace:
        print(f"- **Editing pace:** {pacing.summary_line(pace)}")
    elif pace_note:
        print(f"- **Editing pace:** {pace_note}")
    if frames or hook_frames:
        orientation = " (vertical video)" if portrait and not args.resolution else ""
        print(f"- **Frame size:** max {resolution}px wide{orientation}, max 1998px tall")
    if transcript_segments:
        in_range = " in range" if focused else ""
        print(
            f"- **Transcript:** {len(transcript_segments)} segments{in_range} "
            f"(via {transcript_source or 'captions'})"
        )
    else:
        print(f"- **Transcript:** {transcript_state}")
    if gaps:
        print("- **Transcript status:** partial; missing intervals:")
        for gap in gaps:
            end = gap["end"] if gap["end"] is not None else full_duration
            print(f"  - {format_time(gap['start'])} → {format_time(end)}")

    if detail == "token-burner" and len(frames) > 250:
        print()
        print(
            f"> **Warning:** token-burner detail selected {len(frames)} frames. "
            "This may use a large number of image tokens."
        )

    if not focused and full_duration > 600 and detail not in ("transcript", "token-burner"):
        mins = int(full_duration // 60)
        print()
        print(
            f"> **Warning:** This is a {mins}-minute video. Frame coverage is sparse at this length "
            f"under `{detail}` detail — its cap spreads thin across the full clip. For better results, "
            "re-run with `--start HH:MM:SS --end HH:MM:SS` to zoom into a section, or use "
            "`--detail token-burner` to keep every scene-change frame across the whole video."
        )

    print()
    print("## Frames")
    print()
    if frames:
        print(f"Frames live at: `{work / 'frames'}`")
        print()
        print(
            "**Read each frame path below with the Read tool to view the image.** "
            "Frames are in chronological order; `t=MM:SS` is the absolute timestamp in the source video."
        )
        print()
        for frame in frames:
            print(
                f"- `{frame['path']}` "
                f"(t={format_time(frame['timestamp_seconds'])}, reason={frame.get('reason', 'selected')})"
            )
    else:
        print("_No frames extracted._")

    if hook_frames:
        print()
        print(f"## Hook (00:00 → {format_time(hook_end)})")
        print()
        print("Dense frames over the opening, separate from the frames above. **Read each one.** "
              "Line them up with the speech below: what is on screen, what is said, and when the first "
              "change, text or promise lands.")
        print()
        for frame in hook_frames:
            print(f"- `{frame['path']}` (t={frame['timestamp_seconds']:.1f}s)")
        opening = format_transcript(filter_range(all_segments, 0.0, hook_end)) if all_segments else ""
        print()
        if opening:
            print("```")
            print(opening)
            print("```")
        else:
            print("_No speech in the opening._" if track_available else "_No transcript for the opening._")

    if pace and pace["cuts"]:
        print()
        print("## Cuts")
        print()
        print(f"Hard cuts at: {pacing.cut_list(pace)}")

    print()
    print("## Transcript")
    print()
    if transcript_text:
        label = transcript_source or "captions"
        if focused:
            print(f"_Source: {label}. Filtered to {format_time(effective_start)} → {format_time(effective_end)}:_")
        else:
            print(f"_Source: {label}._")
        print()
        print("```")
        print(transcript_text)
        print("```")
    else:
        print(f"_{transcript_state.capitalize()}._")
        if detail == "transcript" and not track_available:
            print("_Re-run with `--detail balanced` for visual evidence, or configure a transcription backend with setup.py._")
    if errors:
        print()
        print("## Unavailable evidence")
        for error in errors:
            print(f"- {error}")

    print()
    print("---")
    print(f"_Work dir: `{work}` — delete when done._")

    return 1 if errors and not frames and not transcript_segments and not track_available else 0


if __name__ == "__main__":
    configure_stdio()
    try:
        raise SystemExit(main())
    except (ConfigError, OSError) as exc:
        raise SystemExit(str(exc)) from None
