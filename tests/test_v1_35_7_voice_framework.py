"""Tests for v1.35.7 — voice framework (Phase 7.1)."""

from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from janus import voice


def test_dataclasses_exist():
    t = voice.TTSResult(audio_bytes=b"", content_type="audio/mpeg")
    s = voice.STTResult(text="hello", duration_seconds=1.5)
    assert t.content_type == "audio/mpeg"
    assert s.text == "hello"
    assert s.duration_seconds == 1.5


def test_tts_provider_default(monkeypatch):
    monkeypatch.delenv("JANUS_TTS_PROVIDER", raising=False)
    assert voice.tts_provider() == "openai-tts"


def test_tts_provider_override(monkeypatch):
    monkeypatch.setenv("JANUS_TTS_PROVIDER", "elevenlabs")
    assert voice.tts_provider() == "elevenlabs"


def test_stt_provider_default(monkeypatch):
    monkeypatch.delenv("JANUS_STT_PROVIDER", raising=False)
    assert voice.stt_provider() == "openai-whisper"


def test_synthesize_empty_text_raises():
    with pytest.raises(ValueError):
        voice.synthesize("")
    with pytest.raises(ValueError):
        voice.synthesize("   ")


def test_synthesize_unsupported_provider_raises(monkeypatch):
    monkeypatch.setenv("JANUS_TTS_PROVIDER", "fake-provider")
    monkeypatch.setenv("JANUS_TTS_API_KEY", "test")
    with pytest.raises(ValueError, match="unsupported"):
        voice.synthesize("hello")


def test_synthesize_no_api_key_raises(monkeypatch):
    monkeypatch.setenv("JANUS_TTS_PROVIDER", "openai-tts")
    monkeypatch.setenv("JANUS_TTS_API_KEY", "")
    monkeypatch.setenv("JANUS_API_KEY", "")
    with pytest.raises(RuntimeError, match="API key"):
        voice.synthesize("hello")


def test_synthesize_openai_calls_audio_speech_endpoint(monkeypatch):
    monkeypatch.setenv("JANUS_TTS_API_KEY", "test")
    monkeypatch.setenv("JANUS_TTS_PROVIDER", "openai-tts")
    captured = {}

    class FakeResp:
        def __init__(self):
            self.headers = {"content-type": "audio/mpeg"}
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self): return b"FAKE_MP3_BYTES"

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = req.data
        return FakeResp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    out = voice.synthesize("hello world", voice="alloy")
    assert isinstance(out, voice.TTSResult)
    assert out.audio_bytes == b"FAKE_MP3_BYTES"
    assert "/audio/speech" in captured["url"]
    import json
    body = json.loads(captured["body"])
    assert body["input"] == "hello world"
    assert body["voice"] == "alloy"


def test_transcribe_empty_raises():
    with pytest.raises(ValueError):
        voice.transcribe(b"")


def test_transcribe_unsupported_provider(monkeypatch):
    monkeypatch.setenv("JANUS_STT_PROVIDER", "fake")
    monkeypatch.setenv("JANUS_STT_API_KEY", "test")
    with pytest.raises(ValueError, match="unsupported"):
        voice.transcribe(b"audio")


def test_transcribe_no_api_key_raises(monkeypatch):
    monkeypatch.setenv("JANUS_STT_API_KEY", "")
    monkeypatch.setenv("JANUS_API_KEY", "")
    with pytest.raises(RuntimeError, match="API key"):
        voice.transcribe(b"audio")


def test_transcribe_calls_transcriptions_endpoint(monkeypatch):
    monkeypatch.setenv("JANUS_STT_API_KEY", "test")
    captured = {}

    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self): return b'{"text": "hello transcribed"}'

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        return FakeResp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    out = voice.transcribe(b"FAKE_AUDIO_BYTES", content_type="audio/wav")
    assert out.text == "hello transcribed"
    assert "/audio/transcriptions" in captured["url"]


def test_version_bumped_to_1_35_7_or_later():
    from janus import branding
    parts = tuple(int(x) for x in branding.VERSION.split("."))
    assert parts >= (1, 35, 7)
