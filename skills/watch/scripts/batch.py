#!/usr/bin/env python3
"""/watch batch mode: many videos (or a whole profile/playlist) in one run.

Runs watch.py once per video with a light per-video frame budget, isolates
failures, and prints one combined report: an index table with metadata and
engagement for comparison, followed by every per-video report. Also writes
batch-summary.json and batch-summary.csv beside the reports.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))

from config import ConfigError, get_config, validate_language  # noqa: E402
from download import expand_collection, is_url  # noqa: E402
from runtime import configure_stdio  # noqa: E402
import watch as watch_module  # noqa: E402

DEFAULT_FRAMES_PER_VIDEO = 8
DEFAULT_JOBS = 3
DEFAULT_TIMEOUT = 900
FIELD_RE = re.compile(r'^- \*\*([^*]+):\*\* (.*)$')
PACE_RE = re.compile(r'([\d.]+) cuts/min, mean shot ([\d.]+)s')
SUMMARY_FIELDS = ['index', 'status', 'source', 'title', 'uploader', 'upload_date', 'duration',
                  'engagement', 'cuts_per_min', 'mean_shot_s', 'editing_pace', 'frames', 'transcript', 'report', 'error']


def read_sources(args) -> list[str]:
    sources = list(args.sources)
    if args.from_file:
        text = Path(args.from_file).expanduser().read_text(encoding='utf-8-sig')
        sources += [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith('#')]
    unique = []
    for source in sources:
        if source not in unique:
            unique.append(source)
    if not unique:
        raise SystemExit('Give at least one video URL or file (or --from-file).')
    return unique


def expand_sources(sources: list[str], limit: int | None, args, config) -> tuple[list[str], list[str]]:
    """Replace profile/channel/playlist URLs with up to ``limit`` video URLs each."""
    if not limit:
        return sources, []
    expanded, notes = [], []
    for source in sources:
        if not is_url(source):
            expanded.append(source)
            continue
        auth, _ = watch_module.resolve_auth(source, args, config)
        try:
            urls = expand_collection(source, limit, **auth)
        except SystemExit as exc:
            notes.append(f'Could not list {source}: {exc}')
            expanded.append(source)
            continue
        if urls != [source]:
            notes.append(f'{source} → {len(urls)} video(s)')
        expanded += [url for url in urls if url not in expanded]
    return expanded, notes


def watch_command(source: str, args, out_dir: Path) -> list[str]:
    cmd = [sys.executable, str(SCRIPT_DIR / 'watch.py'), source, '--out-dir', str(out_dir)]
    detail = args.detail or 'efficient'
    cmd += ['--detail', detail]
    if detail != 'transcript':
        cmd += ['--max-frames', str(args.frames_per_video)]
    for flag, value in (('--question', args.question), ('--engine', args.engine), ('--language', args.language),
                        ('--resolution', args.resolution), ('--whisper', args.whisper), ('--sub-lang', args.sub_lang),
                        ('--cookies', args.cookies), ('--cookies-from-browser', args.cookies_from_browser),
                        ('--start', args.start), ('--end', args.end), ('--hook', args.hook),
                        ('--prompt', args.prompt)):
        if value is not None:
            cmd += [flag, str(value)]
    if args.no_whisper:
        cmd.append('--no-whisper')
    if args.no_dedup:
        cmd.append('--no-dedup')
    if args.pacing is not None:
        cmd.append('--pacing' if args.pacing else '--no-pacing')
    return cmd


def parse_report(text: str) -> dict:
    """Pull the header fields and frame count out of one watch report."""
    fields, frames, section = {}, 0, None
    for line in text.splitlines():
        if line.startswith('## '):
            section = line[3:].strip()
            continue
        match = FIELD_RE.match(line)
        if match and section is None:
            fields[match.group(1).strip()] = match.group(2).strip()
        elif section == 'Frames' and line.startswith('- `'):
            frames += 1
    fields['_frames'] = frames
    fields['_unavailable'] = '## Unavailable evidence' in text
    fields['_gemini'] = '## Answer (from Gemini)' in text
    return fields


def run_one(index: int, source: str, args, batch_dir: Path) -> dict:
    report_path = batch_dir / f'video-{index:02d}.md'
    entry = {'index': index, 'source': source, 'report': str(report_path), 'status': 'failed', 'error': ''}
    try:
        result = subprocess.run(watch_command(source, args, batch_dir), capture_output=True,
                                encoding='utf-8', errors='replace', timeout=args.timeout)
        stdout, stderr, code = result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired:
        stdout, stderr, code = '', f'Timed out after {args.timeout:.0f}s', -1
    except OSError as exc:
        stdout, stderr, code = '', f'Could not start watch.py: {exc}', -1
    report = stdout.strip()
    if not report:
        last = [line for line in stderr.splitlines() if line.strip()]
        entry['error'] = (last[-1] if last else f'watch.py exited {code}')[:500]
        report = (f'# watch: video report\n\n- **Source:** {source}\n- **Result:** unavailable evidence\n\n'
                  f'## Unavailable evidence\n\n- {entry["error"]}')
    report_path.write_text(report + '\n', encoding='utf-8')
    fields = parse_report(report)
    entry.update(title=fields.get('Title', ''), uploader=fields.get('Uploader', ''),
                 upload_date=fields.get('Upload date', ''), duration=fields.get('Duration', ''),
                 engagement=fields.get('Engagement', '').removesuffix(' (at fetch time)'),
                 frames=fields['_frames'], transcript=fields.get('Transcript', ''),
                 editing_pace=fields.get('Editing pace', ''))
    pace = PACE_RE.search(entry['editing_pace'])
    if pace:
        entry.update(cuts_per_min=pace.group(1), mean_shot_s=pace.group(2))
    elif entry['editing_pace'].startswith('no hard cuts'):
        entry.update(cuts_per_min='0', mean_shot_s=fields.get('Duration', '').rpartition('(')[2].rstrip('s)'))
    if code == 0 and (fields['_frames'] or fields['_gemini'] or 'segments' in entry['transcript']):
        entry['status'] = 'partial' if fields['_unavailable'] else 'ok'
    elif code == 0:
        entry['status'] = 'partial'
    if entry['status'] != 'ok' and not entry['error']:
        unavailable = report.split('## Unavailable evidence', 1)
        if len(unavailable) == 2:
            first = [line[2:] for line in unavailable[1].splitlines() if line.startswith('- ')]
            entry['error'] = (first[0] if first else '')[:500]
    if entry['status'] == 'failed':  # No evidence: placeholder fields would only mislead a comparison.
        entry.update(duration='', transcript='', frames=0, cuts_per_min='', mean_shot_s='', editing_pace='')
    entry['report_text'] = report
    return entry


def _cell(value) -> str:
    return str(value).replace('|', '\\|').replace('\n', ' ') if value not in (None, '') else '—'


def main() -> int:
    ap = argparse.ArgumentParser(prog='watch-batch', description=__doc__.splitlines()[0])
    ap.add_argument('sources', nargs='*', help='Video URLs or local files')
    ap.add_argument('--from-file', help='Text file with one URL or path per line (# comments allowed)')
    ap.add_argument('--expand', type=int, default=None, metavar='N',
                    help='Treat URLs as profiles/channels/playlists and take up to N videos from each')
    ap.add_argument('--jobs', type=int, default=DEFAULT_JOBS, help=f'Videos processed at once (default {DEFAULT_JOBS})')
    ap.add_argument('--frames-per-video', type=int, default=DEFAULT_FRAMES_PER_VIDEO,
                    help=f'Frame cap per video (default {DEFAULT_FRAMES_PER_VIDEO})')
    ap.add_argument('--detail', choices=['transcript', 'efficient', 'balanced', 'token-burner'], default=None,
                    help='Per-video detail (default efficient)')
    ap.add_argument('--timeout', type=float, default=DEFAULT_TIMEOUT, help='Seconds allowed per video')
    ap.add_argument('--out-dir', default=None, help='Parent directory for this batch (default: temp)')
    ap.add_argument('--question', default=None)
    ap.add_argument('--engine', choices=['auto', 'gemini', 'local'], default=None)
    ap.add_argument('--language', default=None)
    ap.add_argument('--resolution', type=int, default=None)
    ap.add_argument('--sub-lang', default=None)
    ap.add_argument('--start', default=None)
    ap.add_argument('--end', default=None)
    ap.add_argument('--no-dedup', action='store_true')
    ap.add_argument('--hook', type=float, nargs='?', const=3.0, default=None, metavar='SECONDS',
                    help='Hook frames at 2 fps over each opening (default 3 s here, to keep image cost down)')
    ap.add_argument('--pacing', action=argparse.BooleanOptionalAction, default=None,
                    help='Editing pace per video (default on unless --detail transcript)')
    ap.add_argument('--prompt', default=None, help='Vocabulary hint for speech fallback, shared by every video')
    speech = ap.add_mutually_exclusive_group()
    speech.add_argument('--no-whisper', action='store_true')
    speech.add_argument('--whisper', choices=['groq', 'openai', 'whisperx', 'onnx'], default=None)
    cookies = ap.add_mutually_exclusive_group()
    cookies.add_argument('--cookies', default=None)
    cookies.add_argument('--cookies-from-browser', default=None)
    args = ap.parse_args()

    if args.jobs < 1 or args.frames_per_video < 1 or args.timeout <= 0 or (args.expand is not None and args.expand < 1):
        ap.error('--jobs, --frames-per-video, --timeout and --expand must be positive')
    args.language = validate_language(args.language, '--language')
    config = get_config()
    sources = read_sources(args)
    auth_args = SimpleNamespace(cookies=args.cookies, cookies_from_browser=args.cookies_from_browser)
    sources, notes = expand_sources(sources, args.expand, auth_args, config)

    parent = Path(args.out_dir).expanduser().resolve() if args.out_dir else None
    if parent:
        parent.mkdir(parents=True, exist_ok=True)
    batch_dir = Path(tempfile.mkdtemp(prefix='watch-batch-', dir=parent))
    print(f'[watch-batch] {len(sources)} video(s), {args.jobs} at a time → {batch_dir}', file=sys.stderr)

    def task(item):
        index, source = item
        entry = run_one(index, source, args, batch_dir)
        print(f"[watch-batch] {index}/{len(sources)} {entry['status']}: {entry.get('title') or source}", file=sys.stderr)
        return entry

    with ThreadPoolExecutor(max_workers=min(args.jobs, len(sources))) as pool:
        entries = list(pool.map(task, enumerate(sources, 1)))

    summary = [{key: entry.get(key, '') for key in SUMMARY_FIELDS} for entry in entries]
    (batch_dir / 'batch-summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    with (batch_dir / 'batch-summary.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=SUMMARY_FIELDS)
        writer.writeheader()
        writer.writerows(summary)

    ok = sum(entry['status'] == 'ok' for entry in entries)
    partial = sum(entry['status'] == 'partial' for entry in entries)
    failed = len(entries) - ok - partial
    print('# watch: batch report')
    print()
    print(f'- **Videos:** {len(entries)} ({ok} ok, {partial} partial, {failed} failed)')
    print(f'- **Per video:** detail {args.detail or "efficient"}, frame cap {args.frames_per_video}')
    for note in notes:
        print(f'- **Expanded:** {note}')
    print(f'- **Summary files:** `{batch_dir / "batch-summary.csv"}`, `{batch_dir / "batch-summary.json"}`')
    print()
    print('## Index')
    print()
    print('| # | Status | Title | Uploader | Date | Duration | Engagement | Cuts/min | Mean shot | Frames | Transcript |')
    print('|---|---|---|---|---|---|---|---|---|---|---|')
    for entry in entries:
        print('| ' + ' | '.join(_cell(entry.get(key)) for key in (
            'index', 'status', 'title', 'uploader', 'upload_date', 'duration', 'engagement', 'cuts_per_min',
            'mean_shot_s', 'frames', 'transcript')) + ' |')
    failures = [entry for entry in entries if entry['status'] != 'ok' and entry.get('error')]
    if failures:
        print()
        print('## Problems')
        print()
        for entry in failures:
            print(f"- #{entry['index']} {entry['source']}: {entry['error']}")
    for entry in entries:
        print()
        print('---')
        print()
        print(f"## Video {entry['index']} of {len(entries)}: {entry.get('title') or entry['source']}")
        print()
        print(entry['report_text'].replace('# watch: video report', '### watch: video report', 1))
    print()
    print('---')
    print(f'_Batch dir: `{batch_dir}` — holds every report and frame; delete when done._')
    return 0 if ok or partial else 1


if __name__ == '__main__':
    configure_stdio()
    try:
        raise SystemExit(main())
    except (ConfigError, OSError) as exc:
        raise SystemExit(str(exc)) from None
