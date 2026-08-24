"""Server-side speech-to-text (Phase 11, V-059).

faster-whisper (CTranslate2) on CPU int8. The GTX 1070 is Pascal and the
CT2 CUDA path is finicky; short capture clips (~5–60 s) transcribe in a
couple of seconds on the 2700X, which is the whole workload — one clip
per explicit phone recording.

Explicit-recording contract (dev plan §13): the caller hands us one
audio file per user tap; we transcribe it and the ROUTE deletes the
temporary file in a finally block. This module never persists audio and
keeps no copy of transcripts — the caller decides what the text becomes
(capture / chat / agent request are explicit user transitions).

Thread model: WhisperModel is not documented thread-safe, so a single
process-wide lock serializes load + transcribe. Single-user server, one
clip at a time — queuing is the correct behaviour, not a bottleneck.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

import av
from faster_whisper import WhisperModel

logger = logging.getLogger(__name__)

_model: WhisperModel | None = None
_model_key: tuple | None = None  # config fingerprint the singleton was built with
_lock = threading.Lock()


class AudioDecodeError(Exception):
    """The uploaded bytes are not decodable audio (av could not open/probe)."""


def reset_model() -> None:
    """Test hook — drop the singleton so config changes take effect."""
    global _model, _model_key
    with _lock:
        _model = None
        _model_key = None


def _get_model(cfg) -> WhisperModel:
    """Lazy singleton. First caller downloads/loads the model (~2–5 s
    warm, minutes cold); later calls reuse it. Config fingerprinted —
    a changed model_size/compute/device reloads on next call."""
    global _model, _model_key
    key = (cfg.model_size, cfg.device, cfg.compute_type, cfg.cpu_threads)
    with _lock:
        if _model is None or _model_key != key:
            if _model is not None:
                logger.info("voice: reloading model %s (config changed)", key)
            _model = WhisperModel(
                cfg.model_size,
                device=cfg.device,
                compute_type=cfg.compute_type,
                cpu_threads=cfg.cpu_threads,
            )
            _model_key = key
            logger.info("voice: model %s ready (cpu, %s)", cfg.model_size, cfg.compute_type)
        return _model


def probe_duration_s(path: str | Path) -> float:
    """Cheap container probe — fail-fast duration check before we spend
    CPU transcribing an over-long upload. Raises AudioDecodeError if av
    cannot open the file at all (garbage bytes → 415 upstream)."""
    try:
        with av.open(str(path)) as container:
            raw = container.duration  # microseconds, None on some live streams
            if raw is None:
                return 0.0  # unknown → let whisper's own info decide
            return raw / 1_000_000
    except av.error.InvalidDataError as exc:  # garbage bytes, wrong container
        raise AudioDecodeError(str(exc)) from exc
    except av.AVError as exc:  # corrupt/truncated container
        raise AudioDecodeError(str(exc)) from exc


def transcribe_file(path: str | Path, *, language: str | None, cfg) -> dict:
    """Blocking transcription — call from a worker thread.

    language: None → auto-detect; "no" forces Norwegian (the phone's
    primary language). Returns {text, language, duration_s}; an empty
    text is legitimate (silence) — the client shows "nothing heard".
    """
    model = _get_model(cfg)
    with _lock:  # serialize transcribe (see module docstring)
        segments, info = model.transcribe(
            str(path),
            language=language or None,
            vad_filter=True,  # trims leading/trailing silence → snappier text
            beam_size=1,  # short-command clips: greedy is plenty, ~2x faster
        )
        text = "".join(seg.text for seg in segments).strip()

    return {
        "text": text,
        "language": info.language,
        "duration_s": round(float(info.duration), 2),
    }
