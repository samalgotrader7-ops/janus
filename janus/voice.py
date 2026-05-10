"""
voice.py — TTS/STT framework (v1.35.7, Phase 7.1).

WHY:
Voice in/out is the most-requested differentiator after the
self-improving learning loop. Pre-v1.35.7 Janus had no voice
support. This module ships the pure-Python framework that wraps
OpenAI-compatible TTS + STT endpoints. Audio capture / playback
on the local machine is left to a thin per-platform adapter
(future v1.35.x).

Provider variants supported in v1.35.7:
  TTS: openai-tts (POST /audio/speech)
       elevenlabs (POST /v1/text-to-speech/{voice_id})
  STT: openai-whisper (POST /audio/transcriptions)

CONFIG (env):
  JANUS_TTS_PROVIDER       openai-tts | elevenlabs (default openai-tts)
  JANUS_TTS_API_KEY        defaults to JANUS_API_KEY
  JANUS_TTS_API_BASE       defaults to https://api.openai.com/v1
  JANUS_TTS_VOICE          provider-specific voice id (default 'alloy')
  JANUS_TTS_MODEL          tts model id (default 'tts-1')

  JANUS_STT_PROVIDER       openai-whisper (default)
  JANUS_STT_API_KEY        defaults to JANUS_API_KEY
  JANUS_STT_API_BASE       defaults to https://api.openai.com/v1
  JANUS_STT_MODEL          default 'whisper-1'

NOT IN SCOPE FOR v1.35.7:
  * Push-to-talk capture (PyAudio / sounddevice integration)
  * Local Whisper inference (`whisper-cpp` Python binding)
  * Audio playback (per-platform; adapter ships when push-to-talk does)
  * Streaming STT
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class TTSResult:
    audio_bytes: bytes
    content_type: str   # e.g. 'audio/mpeg', 'audio/wav'


@dataclass(frozen=True)
class STTResult:
    text: str
    duration_seconds: float = 0.0


# ---------- Config helpers ----------


def tts_provider() -> str:
    return os.environ.get("JANUS_TTS_PROVIDER", "openai-tts").lower()


def stt_provider() -> str:
    return os.environ.get("JANUS_STT_PROVIDER", "openai-whisper").lower()


def _api_key(specific_var: str) -> str:
    v = os.environ.get(specific_var) or os.environ.get("JANUS_API_KEY") or ""
    return v.strip()


def _api_base(specific_var: str, default: str) -> str:
    return os.environ.get(specific_var, default)


# ---------- TTS ----------


def synthesize(
    text: str,
    *,
    voice: str | None = None,
    model: str | None = None,
) -> TTSResult:
    """Generate speech from text. Returns audio bytes + content
    type. Provider chosen by JANUS_TTS_PROVIDER.

    No audio playback — caller writes bytes to a file or pipes to
    a player.
    """
    if not text or not text.strip():
        raise ValueError("text required")

    provider = tts_provider()
    if provider == "openai-tts":
        return _synthesize_openai(text, voice=voice, model=model)
    if provider == "elevenlabs":
        return _synthesize_elevenlabs(text, voice=voice)
    raise ValueError(f"unsupported TTS provider {provider!r}")


def _synthesize_openai(text, *, voice, model) -> TTSResult:
    import json as _json
    import urllib.request

    api_key = _api_key("JANUS_TTS_API_KEY")
    if not api_key:
        raise RuntimeError("no API key (set JANUS_TTS_API_KEY or JANUS_API_KEY)")
    base = _api_base("JANUS_TTS_API_BASE", "https://api.openai.com/v1")
    body = {
        "model": model or os.environ.get("JANUS_TTS_MODEL", "tts-1"),
        "voice": voice or os.environ.get("JANUS_TTS_VOICE", "alloy"),
        "input": text,
    }
    req = urllib.request.Request(
        f"{base}/audio/speech",
        data=_json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
        ct = resp.headers.get("content-type", "audio/mpeg")
    return TTSResult(audio_bytes=data, content_type=ct)


def _synthesize_elevenlabs(text, *, voice) -> TTSResult:
    import json as _json
    import urllib.request

    api_key = _api_key("JANUS_TTS_API_KEY")
    if not api_key:
        raise RuntimeError("no API key (set JANUS_TTS_API_KEY)")
    voice_id = voice or os.environ.get("JANUS_TTS_VOICE", "21m00Tcm4TlvDq8ikWAM")  # default ElevenLabs Rachel
    base = _api_base("JANUS_TTS_API_BASE", "https://api.elevenlabs.io")
    req = urllib.request.Request(
        f"{base}/v1/text-to-speech/{voice_id}",
        data=_json.dumps({"text": text}).encode("utf-8"),
        headers={
            "xi-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    return TTSResult(audio_bytes=data, content_type="audio/mpeg")


# ---------- STT ----------


def transcribe(audio_bytes: bytes, *, content_type: str = "audio/wav") -> STTResult:
    """Transcribe audio bytes to text. Provider chosen by
    JANUS_STT_PROVIDER (default openai-whisper)."""
    if not audio_bytes:
        raise ValueError("audio_bytes required")
    provider = stt_provider()
    if provider == "openai-whisper":
        return _transcribe_openai(audio_bytes, content_type=content_type)
    raise ValueError(f"unsupported STT provider {provider!r}")


def _transcribe_openai(audio_bytes: bytes, *, content_type: str) -> STTResult:
    import json as _json
    import urllib.request
    import uuid

    api_key = _api_key("JANUS_STT_API_KEY")
    if not api_key:
        raise RuntimeError("no API key (set JANUS_STT_API_KEY or JANUS_API_KEY)")
    base = _api_base("JANUS_STT_API_BASE", "https://api.openai.com/v1")
    model = os.environ.get("JANUS_STT_MODEL", "whisper-1")

    # Build multipart/form-data manually (no `requests` dep coupling).
    boundary = "----janus" + uuid.uuid4().hex
    crlf = b"\r\n"
    parts = []
    parts.append(f"--{boundary}".encode())
    parts.append(b'Content-Disposition: form-data; name="model"')
    parts.append(b"")
    parts.append(model.encode())
    parts.append(f"--{boundary}".encode())
    ext = "wav" if content_type.endswith("/wav") else "mp3"
    parts.append(
        f'Content-Disposition: form-data; name="file"; filename="audio.{ext}"'.encode()
    )
    parts.append(f"Content-Type: {content_type}".encode())
    parts.append(b"")
    parts.append(audio_bytes)
    parts.append(f"--{boundary}--".encode())
    parts.append(b"")
    body = crlf.join(parts)

    req = urllib.request.Request(
        f"{base}/audio/transcriptions",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        payload = _json.loads(resp.read().decode("utf-8"))
    return STTResult(text=str(payload.get("text", "")))
