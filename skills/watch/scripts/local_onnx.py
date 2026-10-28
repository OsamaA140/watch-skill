"""Managed local speech-to-text with Whisper ONNX models (sherpa-onnx).

No PyTorch, no Hugging Face: packages come from PyPI and model files from the
sherpa-onnx GitHub releases, verified against pinned SHA-256 digests. That
makes it work on a normal computer and inside hosted sandboxes (such as a
Cowork task) whose network allows PyPI and GitHub. Audio never leaves the
machine. The base watch runtime stays standard-library-only; inference runs
in a separate venv through onnx_asr.py.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

from config import ConfigError, get_config, positive_number, validate_language
from runtime import diagnostic
from transcribe import normalize_segments

SHERPA_ONNX_VERSION = '1.13.8'
RELEASE = 'https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/'
# Pinned digests of the published release assets (verified 2026-10-07).
MODELS = {
    'turbo': {'file': 'sherpa-onnx-whisper-turbo.tar.bz2', 'bytes': 563_790_207,
              'sha256': 'b11acbbcd660b44a8e0df33724feb5aaa709cf65668f2823d59f656312544f22'},
    'small': {'file': 'sherpa-onnx-whisper-small.tar.bz2', 'bytes': 639_387_718,
              'sha256': '486a46afbb7ba798507190ffe02fea2dd726049af212e774537efac6afb210a6'},
}
VAD = {'file': 'silero_vad.onnx', 'bytes': 643_854,
       'sha256': '9e2449e1087496d8d4caba907f23e0bd3f78d91fa552479bb9c23ac09cbb1fd6'}
DEFAULT_MODEL = 'turbo'


def cache_root() -> Path:
    return Path(os.environ.get('WATCH_CACHE_DIR') or (Path.home() / '.cache' / 'watch')).expanduser()


def venv_dir() -> Path:
    return cache_root() / 'onnx-venv'


def venv_python() -> Path:
    base = venv_dir()
    return base / ('Scripts/python.exe' if platform.system() == 'Windows' else 'bin/python')


def model_dir(model: str) -> Path:
    return cache_root() / 'models' / f'sherpa-onnx-whisper-{model}'


def model_files(model: str) -> dict[str, Path]:
    base = model_dir(model)
    return {'encoder': base / f'{model}-encoder.int8.onnx', 'decoder': base / f'{model}-decoder.int8.onnx',
            'tokens': base / f'{model}-tokens.txt', 'vad': cache_root() / 'models' / VAD['file']}


def sentinel(model: str) -> Path:
    return model_dir(model) / '.watch-ready'


def settings(cfg=None) -> dict:
    cfg = dict(get_config() if cfg is None else cfg)
    if cfg['onnx_model'] not in MODELS:
        raise ConfigError(f"WATCH_ONNX_MODEL must be one of: {', '.join(sorted(MODELS))} (default {DEFAULT_MODEL}).")
    cfg['onnx_timeout'] = positive_number(cfg['onnx_timeout'], 'WATCH_ONNX_TIMEOUT') if cfg['onnx_timeout'] else None
    return cfg


def is_ready(model: str | None = None) -> bool:
    model = model or settings()['onnx_model']
    return (venv_python().is_file() and sentinel(model).is_file()
            and all(path.is_file() for path in model_files(model).values()))


def _log(message: str) -> None:
    print(f'[watch] {message}', file=sys.stderr, flush=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def _download(asset: dict, destination: Path) -> Path:
    """Download one pinned release asset, verifying its size and SHA-256."""
    if destination.is_file() and destination.stat().st_size == asset['bytes'] and _sha256(destination) == asset['sha256']:
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + '.part')
    url = RELEASE + asset['file']
    _log(f"downloading {asset['file']} ({asset['bytes'] / 1_048_576:.0f} MB) from GitHub…")
    request = urllib.request.Request(url, headers={'User-Agent': 'watch-skill (python-urllib)'})
    try:
        with urllib.request.urlopen(request, timeout=120) as response, partial.open('wb') as out:
            shutil.copyfileobj(response, out, 1 << 20)
    except OSError as exc:
        partial.unlink(missing_ok=True)
        raise SystemExit(f"Could not download {asset['file']} from GitHub ({type(exc).__name__}: {exc}). "
                         'Check that this environment can reach github.com.') from None
    if partial.stat().st_size != asset['bytes'] or _sha256(partial) != asset['sha256']:
        partial.unlink(missing_ok=True)
        raise SystemExit(f"{asset['file']} failed its integrity check (size or SHA-256 mismatch); nothing was installed.")
    os.replace(partial, destination)
    return destination


def _extract_model(archive: Path, model: str) -> None:
    """Extract only the int8 encoder/decoder and tokens, refusing unsafe paths."""
    wanted = {f'{model}-encoder.int8.onnx', f'{model}-decoder.int8.onnx', f'{model}-tokens.txt'}
    target = model_dir(model)
    target.mkdir(parents=True, exist_ok=True)
    _log('unpacking the speech model (about a minute)…')
    found = set()
    with tarfile.open(archive, 'r:bz2') as bundle:
        for member in bundle:
            name = Path(member.name).name
            if name not in wanted or not member.isfile():
                continue
            source = bundle.extractfile(member)
            if source is None:
                continue
            temporary = target / (name + '.part')
            with source, temporary.open('wb') as out:
                shutil.copyfileobj(source, out, 1 << 20)
            os.replace(temporary, target / name)
            found.add(name)
    missing = wanted - found
    if missing:
        raise SystemExit(f"The model archive is missing {', '.join(sorted(missing))}.")


def _pip_install(python: Path) -> None:
    _log(f'installing sherpa-onnx {SHERPA_ONNX_VERSION} and numpy into {venv_dir()}…')
    cmd = [str(python), '-m', 'pip', 'install', '--disable-pip-version-check', '--quiet',
           f'sherpa-onnx=={SHERPA_ONNX_VERSION}', 'numpy>=1.24,<3']
    result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
    if result.returncode != 0:
        raise SystemExit(f'pip could not install sherpa-onnx: {diagnostic(result.stderr or result.stdout)}')


def install(model: str | None = None) -> dict:
    """Create the venv, fetch and verify the model, warm it up. Idempotent."""
    cfg = settings()
    model = model or cfg['onnx_model']
    if model not in MODELS:
        raise SystemExit(f"Unknown onnx model {model}; choose one of {', '.join(sorted(MODELS))}.")
    started = time.monotonic()
    python = venv_python()
    if not python.is_file():
        _log(f'creating the speech environment at {venv_dir()}…')
        result = subprocess.run([sys.executable, '-m', 'venv', str(venv_dir())], capture_output=True, text=True)
        if result.returncode != 0 or not python.is_file():
            raise SystemExit(f'Could not create a Python venv: {diagnostic(result.stderr or result.stdout)}')
    probe = subprocess.run([str(python), '-c', f'import sherpa_onnx, numpy; assert sherpa_onnx.__version__ == "{SHERPA_ONNX_VERSION}"'],
                           capture_output=True, text=True)
    if probe.returncode != 0:
        _pip_install(python)
    files = model_files(model)
    if not all(files[key].is_file() for key in ('encoder', 'decoder', 'tokens')):
        archive = cache_root() / 'downloads' / MODELS[model]['file']
        _download(MODELS[model], archive)
        _extract_model(archive, model)
        archive.unlink(missing_ok=True)  # The extracted files are what we keep.
    _download(VAD, files['vad'])
    _log('warming up the speech model…')
    with tempfile.TemporaryDirectory(prefix='watch-onnx-warm-') as temporary:
        silence = Path(temporary) / 'silence.wav'
        import wave
        with wave.open(str(silence), 'wb') as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(16000)
            stream.writeframes(b'\x00\x00' * 16000)
        _run_runner(silence, Path(temporary) / 'out.json', model, None, cfg, timeout=600)
    sentinel(model).write_text(json.dumps({'sherpa_onnx': SHERPA_ONNX_VERSION, 'model': model}), encoding='utf-8')
    _log(f'local speech model ready ({model}) in {time.monotonic() - started:.0f}s.')
    return {'python': str(python), 'model': model}


def _threads(cfg: dict) -> int:
    value = cfg.get('onnx_threads')
    if value:
        return max(1, int(positive_number(value, 'WATCH_ONNX_THREADS')))
    return max(1, min(8, os.cpu_count() or 1))


def _run_runner(audio: Path, output: Path, model: str, language: str | None, cfg: dict, timeout=None) -> dict:
    files = model_files(model)
    cmd = [str(venv_python()), str(Path(__file__).resolve().parent / 'onnx_asr.py'), str(audio),
           '--encoder', str(files['encoder']), '--decoder', str(files['decoder']), '--tokens', str(files['tokens']),
           '--vad', str(files['vad']), '--threads', str(_threads(cfg)), '--output', str(output)]
    if language:
        cmd += ['--language', language]
    env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONWARNINGS='ignore')
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace',
                                timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise SystemExit('Local speech transcription timed out and was stopped. Increase WATCH_ONNX_TIMEOUT or leave it unset.') from None
    except OSError as exc:
        raise SystemExit(f'The speech environment is not runnable ({type(exc).__name__}); rerun setup.py --install-onnx.') from None
    if result.returncode != 0:
        raise SystemExit(f'Local speech transcription failed (exit {result.returncode}): {diagnostic(result.stderr)}')
    try:
        return json.loads(output.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise SystemExit(f'Local speech output missing or malformed ({type(exc).__name__}).') from None


def extract_wav(video_path: str, out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i', str(video_path), '-vn',
           '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', str(out_path)]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
    if result.returncode != 0 or not out_path.is_file():
        raise SystemExit(f'Audio extraction failed: {diagnostic(result.stderr)}')
    return out_path


def transcribe_video(video_path: str, work: Path, language: str | None = None, cfg=None):
    """Transcribe a video's audio locally. Returns Segments with .language and .filtered."""
    cfg = settings(cfg)
    model = cfg['onnx_model']
    if language:
        language = validate_language(language, '--language')
    if not is_ready(model):
        raise SystemExit('The local speech model (onnx) is not installed. Run setup.py --install-onnx '
                         '(about 2 minutes, ~600 MB download from GitHub).')
    wav = extract_wav(video_path, work / 'audio-16k.wav')
    _log(f'local speech transcription (Whisper {model}, onnx); CPU runs take about half the clip length…')
    data = _run_runner(wav, work / 'onnx-transcript.json', model, language, cfg, cfg['onnx_timeout'])
    try:
        segments = normalize_segments({'segments': data.get('segments', [])})
    except (ValueError, TypeError) as exc:
        raise SystemExit(f'Local speech output malformed: {exc}') from None
    segments.language = data.get('language') if isinstance(data.get('language'), str) else None
    segments.filtered = data.get('filtered') if isinstance(data.get('filtered'), int) else 0
    return segments
