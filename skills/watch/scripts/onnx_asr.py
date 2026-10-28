#!/usr/bin/env python3
"""Speech-to-text runner for the managed sherpa-onnx environment.

Runs ONLY inside watch's onnx venv (numpy + sherpa-onnx); the base watch
runtime never imports it. Finds speech with Silero VAD, merges it into pieces
of up to PIECE_SECONDS (more context = better accuracy, especially for
dialects), pads each piece with silence so Whisper does not drop the last
words, and keeps Whisper's own segment timestamps. Writes
{"segments": [{"start", "end", "text"}], "language": ..., "filtered": n}.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import wave
from pathlib import Path

import numpy as np
import sherpa_onnx

SAMPLE_RATE = 16000
WINDOW_SECONDS = 30.0      # Whisper decodes at most 30 s at a time.
TAIL_PAD_SECONDS = 6.0     # Trailing silence: without it Whisper often drops the final words.
PIECE_SECONDS = WINDOW_SECONDS - TAIL_PAD_SECONDS
MAX_GAP_SECONDS = 3.0      # A longer pause (or music) starts a new piece.
MIN_PIECE_SECONDS = 0.3
# Whisper's well-known outputs for music, noise or silence. A segment whose whole
# text is one of these is dropped and counted, never reported as speech.
HALLUCINATIONS = {
    'you', 'thank you', 'thank you so much', 'thanks for watching', 'thank you for watching',
    'thanks for watching and see you next time', 'please subscribe', 'subscribe to my channel',
    'subtitles by the amaraorg community', 'ترجمة نانسي قنقر', 'اشتركوا في القناة', 'اشترك في القناة',
    'شكرا للمشاهدة', 'شكرا لكم على المشاهدة', 'شكرا', 'موسيقى', 'music',
    'im going to go to the next video', 'see you in the next video', 'ill see you in the next video',
    'see you next time', 'bye', 'bye bye', 'okay', 'so', 'oh', 'hmm',
}
TRAILING_HALLUCINATIONS = ('اشتركوا في القناة', 'اشترك في القناة', 'ترجمة نانسي قنقر', 'شكرا للمشاهدة',
                           'thanks for watching', 'thank you for watching', 'please subscribe')


def _normalized(text: str) -> str:
    keep = ''.join(ch.lower() for ch in text if ch.isalnum() or ch.isspace())
    return ' '.join(keep.split())


def is_hallucination(text: str) -> bool:
    normalized = _normalized(text)
    return not normalized or normalized in HALLUCINATIONS


def strip_trailing_hallucination(text: str) -> tuple[str, bool]:
    """Remove a known phantom phrase glued to the end of real speech."""
    stripped = text.rstrip(' .!،,؟?')
    for phrase in TRAILING_HALLUCINATIONS:
        if _normalized(stripped).endswith(phrase) and _normalized(stripped) != phrase:
            words = len(phrase.split())
            kept = ' '.join(stripped.split()[:-words]).rstrip(' .!،,')
            return kept, True
    return text, False


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), 'rb') as stream:
        if stream.getnchannels() != 1 or stream.getsampwidth() != 2 or stream.getframerate() != SAMPLE_RATE:
            raise SystemExit('Expected 16 kHz mono 16-bit PCM WAV input.')
        frames = stream.readframes(stream.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0


def vad_regions(samples: np.ndarray, vad_model: Path | None) -> list[tuple[int, int]]:
    """(start, end) sample ranges that contain speech."""
    if not (vad_model and vad_model.is_file()):
        return [(0, len(samples))]
    config = sherpa_onnx.VadModelConfig()
    config.silero_vad.model = str(vad_model)
    config.silero_vad.threshold = 0.5
    config.silero_vad.min_silence_duration = 0.4
    config.silero_vad.min_speech_duration = 0.25
    config.silero_vad.max_speech_duration = PIECE_SECONDS
    config.sample_rate = SAMPLE_RATE
    window = config.silero_vad.window_size
    vad = sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=WINDOW_SECONDS * 4)
    regions = []

    def drain():
        while not vad.empty():
            segment = vad.front
            regions.append((int(segment.start), int(segment.start) + len(segment.samples)))
            vad.pop()

    for offset in range(0, len(samples), window):
        vad.accept_waveform(samples[offset:offset + window])
        drain()
    vad.flush()
    drain()
    return regions


def merge_regions(regions: list[tuple[int, int]], total: int) -> list[tuple[int, int]]:
    """Merge nearby speech into pieces of at most PIECE_SECONDS, then hard-split longer ones."""
    limit, gap = int(PIECE_SECONDS * SAMPLE_RATE), int(MAX_GAP_SECONDS * SAMPLE_RATE)
    pad = int(0.2 * SAMPLE_RATE)  # Keep a little context around each region.
    pieces: list[list[int]] = []
    for start, end in regions:
        start, end = max(0, start - pad), min(total, end + pad)
        if pieces and start - pieces[-1][1] <= gap and end - pieces[-1][0] <= limit:
            pieces[-1][1] = max(pieces[-1][1], end)
        else:
            pieces.append([start, end])
    bounded = []
    for start, end in pieces:
        for offset in range(start, end, limit):
            part_end = min(end, offset + limit)
            if part_end - offset >= MIN_PIECE_SECONDS * SAMPLE_RATE:
                bounded.append((offset, part_end))
    return bounded


def decode_piece(recognizer, chunk: np.ndarray) -> tuple[list[tuple[float, float, str]], str]:
    """Return [(start, end, text)] relative to the chunk, plus the detected language."""
    padded = np.concatenate([chunk, np.zeros(int(TAIL_PAD_SECONDS * SAMPLE_RATE), dtype=np.float32)])
    stream = recognizer.create_stream()
    stream.accept_waveform(SAMPLE_RATE, padded)
    recognizer.decode_stream(stream)
    result = stream.result
    duration = len(chunk) / SAMPLE_RATE
    texts = list(getattr(result, 'segment_texts', []) or [])
    starts = list(getattr(result, 'segment_timestamps', []) or [])
    lengths = list(getattr(result, 'segment_durations', []) or [])
    parts = []
    if texts and len(texts) == len(starts):
        for index, (text, begin) in enumerate(zip(texts, starts)):
            if begin >= duration:  # Inside the padding: not real audio.
                continue
            length = lengths[index] if index < len(lengths) else 0.0
            end = begin + length if length > 0 else (starts[index + 1] if index + 1 < len(starts) else duration)
            parts.append((float(begin), float(min(max(end, begin), duration)), text.strip()))
    elif (result.text or '').strip():
        parts.append((0.0, duration, result.text.strip()))
    return parts, getattr(result, 'lang', '') or ''


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('audio', type=Path)
    parser.add_argument('--encoder', type=Path, required=True)
    parser.add_argument('--decoder', type=Path, required=True)
    parser.add_argument('--tokens', type=Path, required=True)
    parser.add_argument('--vad', type=Path, default=None)
    parser.add_argument('--language', default='')
    parser.add_argument('--threads', type=int, default=max(1, min(8, os.cpu_count() or 1)))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()

    samples = read_wav(args.audio)
    recognizer = sherpa_onnx.OfflineRecognizer.from_whisper(
        encoder=str(args.encoder), decoder=str(args.decoder), tokens=str(args.tokens),
        language=args.language or '', task='transcribe', num_threads=args.threads,
        enable_segment_timestamps=True)
    pieces = merge_regions(vad_regions(samples, args.vad), len(samples))
    segments, languages, filtered = [], {}, 0
    for index, (start, end) in enumerate(pieces, 1):
        offset = start / SAMPLE_RATE
        parts, lang = decode_piece(recognizer, samples[start:end])
        spoken = 0
        for begin, finish, text in parts:
            text, trimmed = strip_trailing_hallucination(text)
            filtered += trimmed
            if is_hallucination(text):
                filtered += bool(text)
                continue
            spoken += 1
            segments.append({'start': round(offset + begin, 3), 'end': round(offset + finish, 3), 'text': text})
        if lang and spoken:
            languages[lang] = languages.get(lang, 0) + (end - start)
        print(f'piece {index}/{len(pieces)} done', file=sys.stderr, flush=True)
    segments.sort(key=lambda item: item['start'])
    for previous, current in zip(segments, segments[1:]):  # Keep timestamps monotonic.
        if current['start'] < previous['start']:
            current['start'] = previous['start']
    language = max(languages, key=languages.get) if languages else (args.language or None)
    args.output.write_text(json.dumps({'segments': segments, 'language': language, 'filtered': filtered},
                                      ensure_ascii=False), encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
