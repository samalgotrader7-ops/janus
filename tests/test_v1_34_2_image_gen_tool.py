"""Tests for v1.34.2 — image_gen tool (Phase 7.3)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from janus.tools.image_gen import ImageGen, _filename, _provider, _model
from janus.tools import _BUILTIN_TOOL_FACTORIES


# -------------------- Tool wiring --------------------


def test_image_gen_in_builtin_factories():
    """The tool is registered in the default tool registry."""
    assert "image_gen" in _BUILTIN_TOOL_FACTORIES
    assert _BUILTIN_TOOL_FACTORIES["image_gen"] is ImageGen


def test_image_gen_tool_attrs():
    tool = ImageGen()
    assert tool.name == "image_gen"
    assert tool.risk == "exec"
    # parameters has prompt
    assert "properties" in tool.parameters
    assert "prompt" in tool.parameters["properties"]
    assert tool.parameters["required"] == ["prompt"]


# -------------------- Helpers --------------------


def test_provider_default(monkeypatch):
    monkeypatch.delenv("JANUS_IMAGE_PROVIDER", raising=False)
    assert _provider() == "openai-dalle"


def test_provider_override(monkeypatch):
    monkeypatch.setenv("JANUS_IMAGE_PROVIDER", "stability-ai")
    assert _provider() == "stability-ai"


def test_model_default(monkeypatch):
    monkeypatch.delenv("JANUS_IMAGE_MODEL", raising=False)
    assert _model() == "dall-e-3"


def test_filename_safe():
    """_filename sanitizes the prompt and adds a timestamp."""
    name = _filename("Hello, world! 123")
    assert name.endswith(".png")
    # Should NOT contain illegal filename characters
    for ch in (" ", ",", "!", "/", "\\", ":"):
        assert ch not in name


def test_filename_handles_empty_prompt():
    name = _filename("")
    assert name.endswith(".png")
    assert "image" in name  # fallback name


def test_filename_truncates_long_prompts():
    name = _filename("a" * 200)
    # Slug part (before timestamp) should be ≤40 chars
    # Format: YYYYMMDD_HHMMSS_<slug>.png
    parts = name.split("_", 2)
    if len(parts) >= 3:
        slug = parts[2].replace(".png", "")
        assert len(slug) <= 40


# -------------------- run() — refused by approver --------------------


def test_run_returns_error_on_empty_prompt():
    tool = ImageGen()
    out = tool.run({"prompt": ""}, lambda *a, **kw: True)
    assert out.startswith("error:")
    assert "prompt" in out.lower()


def test_run_refused_by_approver():
    """Approver returning False means the tool returns a refusal
    string, not silently ignored."""
    tool = ImageGen()

    def deny_approver(*args, **kwargs):
        return False

    out = tool.run({"prompt": "a cat"}, deny_approver)
    assert "refused" in out.lower()


def test_run_uses_capability_image_gen_provider():
    """Approver receives capability=('image', 'gen', '<provider>')
    so a skill can grant via `image.gen.openai-dalle: ['*']`."""
    tool = ImageGen()
    captured = {}

    def capture(name, details, capability=None):
        captured["name"] = name
        captured["capability"] = capability
        return False  # deny so we don't actually call API

    tool.run({"prompt": "a cat"}, capture)
    assert captured["name"] == "image_gen"
    assert captured["capability"][0] == "image"
    assert captured["capability"][1] == "gen"


def test_run_caps_n_at_4():
    """n is clamped to [1, 4] regardless of input."""
    tool = ImageGen()
    captured = {}

    def capture(name, details, capability=None):
        captured["details"] = details
        return False  # deny so we don't actually call API

    tool.run({"prompt": "x", "n": 10}, capture)
    assert "n=4" in captured["details"]
    captured.clear()
    tool.run({"prompt": "x", "n": 0}, capture)
    assert "n=1" in captured["details"]


def test_run_unsupported_provider_returns_error(monkeypatch):
    monkeypatch.setenv("JANUS_IMAGE_PROVIDER", "stability-ai")
    monkeypatch.setenv("JANUS_IMAGE_API_KEY", "test")  # ensure key check passes
    tool = ImageGen()
    out = tool.run({"prompt": "x"}, lambda *a, **kw: True)
    assert "not supported" in out.lower()
    assert "stability-ai" in out


# -------------------- API call (mocked HTTP) --------------------


def test_generate_openai_dalle_no_key_returns_error(monkeypatch):
    monkeypatch.delenv("JANUS_IMAGE_API_KEY", raising=False)
    monkeypatch.delenv("JANUS_API_KEY", raising=False)
    from janus import config
    monkeypatch.setattr(config, "API_KEY", "")
    from janus.tools import image_gen
    ok, paths, msg = image_gen._generate_openai_dalle(
        "test", size="1024x1024", n=1,
    )
    assert ok is False
    assert "no api key" in msg.lower()


def test_generate_openai_dalle_saves_b64(monkeypatch, tmp_path):
    """Mock urllib so we can test the file-save path without
    actually hitting OpenAI's API."""
    import base64
    monkeypatch.setenv("JANUS_IMAGE_API_KEY", "test-key")
    monkeypatch.setenv("JANUS_IMAGE_DIR", str(tmp_path))
    fake_png = b"\x89PNG\r\n\x1a\nfake-image-bytes"
    fake_b64 = base64.b64encode(fake_png).decode("ascii")
    fake_response = {
        "data": [{"b64_json": fake_b64}]
    }

    class FakeResponse:
        def __init__(self, body):
            self._body = body
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass
        def read(self):
            import json
            return json.dumps(self._body).encode("utf-8")

    def fake_urlopen(req, timeout=None):
        return FakeResponse(fake_response)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    from janus.tools import image_gen
    ok, paths, msg = image_gen._generate_openai_dalle(
        "test prompt", size="1024x1024", n=1,
    )
    assert ok is True
    assert len(paths) == 1
    saved = paths[0]
    # File exists and has the expected bytes
    from pathlib import Path
    assert Path(saved).exists()
    assert Path(saved).read_bytes() == fake_png


# -------------------- Version pin --------------------


def test_version_bumped_to_1_34_2_or_later():
    from janus import branding
    parts = tuple(int(x) for x in branding.VERSION.split("."))
    assert parts >= (1, 34, 2)
