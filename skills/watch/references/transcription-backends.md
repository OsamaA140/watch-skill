# Transcription backends and setup fields

Captions always come first. A backend is used only when a video has no usable caption track.

## onnx (local Whisper large-v3-turbo, recommended)

- Installs with `setup.py --backend onnx` or `setup.py --install-onnx`. In a cloud sandbox it is installed by `setup.py --cloud-defaults`.
- It creates `~/.cache/watch/onnx-venv` with `sherpa-onnx==1.13.8` and numpy from PyPI. It downloads `sherpa-onnx-whisper-turbo.tar.bz2` (about 540 MB) and `silero_vad.onnx` from the sherpa-onnx GitHub releases, checks both against pinned SHA-256 digests, extracts only the int8 encoder, decoder and tokens, and warms up on one second of silence. It takes about 90 seconds on a 2-CPU sandbox and uses about 1.2 GB of disk.
- Silero VAD splits speech into pieces of up to 27 seconds. Each piece is transcribed and timestamped from its start. Whisper's music/noise phantoms are dropped and counted.
- Measured on a 2-CPU sandbox: a 68-second English TikTok took 29 seconds, matching TikTok's own captions almost word for word. A 60-second Arabic hotel TikTok with a sung nasheed took 26 seconds and correctly came out as Arabic.
- Settings: `WATCH_ONNX_MODEL=turbo|small` (default turbo; `small` is a larger download and is not faster), `WATCH_ONNX_THREADS` (default: CPU count, at most 8), `WATCH_ONNX_TIMEOUT` (seconds, unset by default). `WATCH_CACHE_DIR` moves the cache.
- If the integrity check fails, nothing is installed: the release asset changed upstream. Report it, and do not bypass the check.

## whisperx

Defaults: `WATCH_WHISPERX_MODEL=small`, `WATCH_WHISPERX_DEVICE=cpu`, `WATCH_WHISPERX_COMPUTE_TYPE=int8`, `WATCH_WHISPERX_BATCH_SIZE=8`. No alignment or diarization is run. `WATCH_WHISPERX_LANGUAGE=es` (for example) gives a spoken-language hint. Without a hint, auto-detection is used but reported as unverified, because WhisperX 3.8.6 mislabels its JSON language when alignment is disabled.

If a small-model transcript is nonsense, suggest the actual spoken-language hint or `WATCH_WHISPERX_MODEL=large-v3` (a 2.9 GB model, about 6 GB peak process RAM in the reference measurement), then rerun the installer to warm that model. CPU inference may take minutes. `WATCH_WHISPERX_TIMEOUT` optionally sets positive seconds; by default there is no deadline. CUDA is configurable but untested; do not promise MPS support.

## groq / openai

Cloud Whisper (`whisper-large-v3` on Groq, `whisper-1` on OpenAI). Only extracted mono 16 kHz audio is uploaded, within a 24,000,000-byte budget, and longer audio is chunked. The spoken-language hint is sent as the `language` field.

## setup.py --json fields

`status`, `can_proceed`, `engine`, `configured_engine`, `gemini_key_present` (boolean only), `gemini_model`, `binaries_required`, `cloud_sandbox`, `local_files_ready` (FFmpeg and ffprobe present), `language`, `site_cookies` (domains and kinds only), `first_run`, `setup_complete`, `missing_binaries`, `whisper_backend`, `configured_backend`, `has_api_key`, `backend_ready`, `whisperx_ready`, `whisperx_bin`, `whisperx_model`, `onnx_ready`, `onnx_model`, `config_file`, `watch_detail`, `platform`, plus `tools` and `youtube` diagnostics in detailed mode. Detailed mode may start the WhisperX CLI's `--help`; `--check` never does.
