"""V-059 voice transcribe route tests — Phase 11 server-side STT.

Plain python, no pytest (repo convention).
Run: .venv/bin/python -m tests.test_voice

Mounts the full v1 router on a bare app (no DB needed — the endpoint is
stateless by design: audio in, transcript out, temp file deleted). The
whisper engine and duration probe are patched at the ROUTER namespace
(imported there), so no model download and no network. What is real:
multipart parsing, content-type gate, byte cap, capability + feature
gates, temp-file lifecycle.

The model-backed leg is exercised live (scripts + phone E2E), not here.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.auth import set_principal
from src.config import VoiceConfig
from src.routers import v1 as v1_router
from src.voice import AudioDecodeError

PASS = 0
FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {extra}")


CLIP = b"\x1a\x45\xdf\xa3FAKE-WEBM-BYTES" * 8  # ~176 bytes of stand-in audio


def build_client(capabilities: list[str] | None = None) -> TestClient:
    """capabilities=None → no principal (auth disabled, checks pass).
    A list → a device principal stamped on every request."""
    app = FastAPI()
    app.include_router(v1_router.router, prefix="/v1")
    app.state.config = SimpleNamespace(voice=VoiceConfig())

    if capabilities is not None:

        @app.middleware("http")
        async def stamp_principal(request, call_next):
            set_principal(
                request, {"type": "device", "capabilities": capabilities}
            )
            return await call_next(request)

    return TestClient(app, raise_server_exceptions=False)


def upload(client, *, data: bytes = CLIP, content_type: str = "audio/webm",
           language: str = "", filename: str = "clip.webm"):
    form = {"language": language} if language else None
    return client.post(
        "/v1/voice/transcribe",
        files={"audio": (filename, data, content_type)},
        data=form,
    )


class FakeEngine:
    """Records what the route handed it; returns a canned transcript."""

    def __init__(self, result=None, exc: Exception | None = None):
        self.calls: list[dict] = []
        self.result = result or {
            "text": "kjøpe melk i morgen", "language": "no", "duration_s": 4.2
        }
        self.exc = exc

    def __call__(self, path, *, language, cfg):
        self.calls.append(
            {"path": path, "language": language,
             "bytes": open(path, "rb").read()}
        )
        if self.exc:
            raise self.exc
        return dict(self.result)


def run() -> int:
    print("voice transcribe (V-059)")
    engine = FakeEngine()

    with patch.object(v1_router, "transcribe_file", engine), \
         patch.object(v1_router, "probe_duration_s", lambda p: 4.2):
        client = build_client()

        # 1. happy path: shape + exact bytes handed to the engine
        r = upload(client)
        check("200 on valid clip", r.status_code == 200, f"got {r.status_code}: {r.text[:120]}")
        body = r.json()
        check("response shape text/language/duration_s",
              body == {"text": "kjøpe melk i morgen", "language": "no", "duration_s": 4.2},
              str(body))
        check("engine saw the exact uploaded bytes",
              engine.calls and engine.calls[-1]["bytes"] == CLIP)

        # 2. temp file deleted after success (dev plan §13)
        check("temp audio deleted after success",
              engine.calls and not __import__("pathlib").Path(engine.calls[-1]["path"]).exists())

        # 3. language normalization: " NO " → "no"
        r = upload(client, language=" NO ")
        check("language normalized to 'no'",
              engine.calls and engine.calls[-1]["language"] == "no",
              str(engine.calls[-1]["language"] if engine.calls else None))

        # 4. empty language → None → auto-detect
        upload(client, language="")
        check("empty language → None (auto-detect)",
              engine.calls and engine.calls[-1]["language"] is None)

        # 5. content-type gate
        r = upload(client, content_type="image/png", filename="clip.png")
        check("415 on non-audio content type", r.status_code == 415, str(r.status_code))
        r = upload(client, content_type="application/ogg", filename="clip.oga")
        check("application/ogg accepted", r.status_code == 200, str(r.status_code))

        # 6. empty upload
        r = upload(client, data=b"")
        check("422 on empty upload", r.status_code == 422, str(r.status_code))

        # 7. byte cap enforced mid-stream
        tight = build_client()
        tight.app.state.config = SimpleNamespace(
            voice=VoiceConfig(max_upload_bytes=64)
        )
        r = upload(tight)
        check("413 over byte cap", r.status_code == 413, str(r.status_code))

        # 8. duration cap via probe
        with patch.object(v1_router, "probe_duration_s", lambda p: 999.0):
            r = upload(client)
        check("413 over duration cap", r.status_code == 413, str(r.status_code))
        check("no transcribe attempt on over-long clip",
              engine.calls[-1]["bytes"] == CLIP)  # last call unchanged

        # 9. undecodable audio → 415
        def boom(p):
            raise AudioDecodeError("garbage")

        with patch.object(v1_router, "probe_duration_s", boom):
            r = upload(client)
        check("415 on undecodable bytes", r.status_code == 415, str(r.status_code))

        # 10. engine failure → 500 AND temp file still deleted
        calls = []
        import pathlib

        def exploding(path, *, language, cfg):
            calls.append(path)
            assert pathlib.Path(path).exists()
            raise RuntimeError("whisper exploded")

        with patch.object(v1_router, "transcribe_file", exploding):
            r = upload(client)
        check("500 on engine failure", r.status_code == 500, str(r.status_code))
        check("temp audio deleted even on engine failure",
              calls and not pathlib.Path(calls[-1]).exists())

    # 11. fail-closed feature flag → 501 (protocol §9)
    with patch.object(v1_router, "transcribe_file", FakeEngine()), \
         patch.object(v1_router, "probe_duration_s", lambda p: 1.0), \
         patch.dict(v1_router.FEATURES, {"voice_transcription": False}):
        r = upload(build_client())
    check("501 when feature flagged off", r.status_code == 501, str(r.status_code))

    # 12. capability gate: device principal WITHOUT the cap → 403
    with patch.object(v1_router, "transcribe_file", FakeEngine()), \
         patch.object(v1_router, "probe_duration_s", lambda p: 1.0):
        r = upload(build_client(capabilities=["today.read", "capture.commit"]))
        check("403 without voice.transcribe capability", r.status_code == 403, str(r.status_code))

        # 13. device principal WITH the cap → 200
        r = upload(build_client(capabilities=["voice.transcribe"]))
        check("200 with voice.transcribe capability", r.status_code == 200, str(r.status_code))

    # 14. capabilities served on the wire (client feature gating)
    caps = build_client().get("/v1/capabilities").json()
    check("features.voice_transcription advertised",
          caps["features"].get("voice_transcription") is True, str(caps["features"]))

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(run())
