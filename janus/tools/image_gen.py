"""
tools/image_gen.py — model-callable image generation (v1.34.2,
Phase 7.3).

WHY THIS EXISTS:
Phase 7 / New differentiation. Janus already has vision (image
analysis), browser (screenshots), and Telegram media-send. The
missing piece was image GENERATION — the model can describe but
not create. v1.34.2 adds an `image_gen` tool that wraps the
provider-of-choice's images endpoint.

PROVIDERS SUPPORTED (v1.34.2):
  * openai-dalle  — POST https://api.openai.com/v1/images/generations
                    Models: dall-e-3 (default), dall-e-2

PROVIDERS DEFERRED (future point releases):
  * stability-ai  — Stability AI's SDXL endpoint
  * fal-ai        — Fal AI hosted Flux
  * local         — Stable Diffusion via webui API

CONFIG:
  JANUS_IMAGE_PROVIDER     — defaults to 'openai-dalle'
  JANUS_IMAGE_MODEL        — defaults to 'dall-e-3'
  JANUS_IMAGE_API_KEY      — defaults to JANUS_API_KEY (shared)
  JANUS_IMAGE_API_BASE     — defaults to https://api.openai.com/v1
  JANUS_IMAGE_DIR          — output dir (default ~/.janus/images/)

RISK: 'exec' — image generation costs money and produces a file
on disk. Subject to permission mode and approval prompt. A skill
can grant via `image.gen` capability.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable

from .base import Tool
from .. import config


def _provider() -> str:
    return os.environ.get("JANUS_IMAGE_PROVIDER", "openai-dalle").lower()


def _model() -> str:
    return os.environ.get("JANUS_IMAGE_MODEL", "dall-e-3")


def _api_key() -> str:
    return os.environ.get("JANUS_IMAGE_API_KEY") or config.API_KEY or ""


def _api_base() -> str:
    return os.environ.get(
        "JANUS_IMAGE_API_BASE",
        "https://api.openai.com/v1",
    )


def _output_dir() -> Path:
    custom = os.environ.get("JANUS_IMAGE_DIR")
    if custom:
        return Path(custom)
    return Path(config.HOME) / "images"


def _filename(prompt: str) -> str:
    """Generate a stable, filesystem-safe filename for the output."""
    slug = "".join(c if c.isalnum() else "_" for c in prompt[:40])
    slug = slug.strip("_") or "image"
    ts = time.strftime("%Y%m%d_%H%M%S")
    return f"{ts}_{slug}.png"


def _generate_openai_dalle(
    prompt: str, *, size: str, n: int,
) -> tuple[bool, list[str], str]:
    """Call OpenAI's /images/generations endpoint. Returns
    (ok, image_urls_or_paths, message).

    Saves images to JANUS_IMAGE_DIR. Returns local file paths
    when save succeeds; URLs when only URL is available.
    """
    import urllib.request
    api_key = _api_key()
    if not api_key:
        return False, [], "no API key (set JANUS_IMAGE_API_KEY or JANUS_API_KEY)"
    body = {
        "model": _model(),
        "prompt": prompt,
        "size": size,
        "n": n,
        # response_format=b64_json embeds the image bytes in the
        # response so we can save without a second HTTP request.
        # DALL-E 3 doesn't honor this on all endpoints — fall back
        # to URL when needed.
        "response_format": "b64_json",
    }
    req = urllib.request.Request(
        f"{_api_base()}/images/generations",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return False, [], f"image API call failed: {type(e).__name__}: {e}"

    out_dir = _output_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    for entry in payload.get("data", []) or []:
        # Prefer base64; fall back to downloading via URL.
        b64 = entry.get("b64_json")
        url = entry.get("url")
        out_path = out_dir / _filename(prompt)
        # Avoid filename collision when n > 1
        i = 1
        while out_path.exists():
            stem, ext = out_path.stem, out_path.suffix
            out_path = out_dir / f"{stem}_{i}{ext}"
            i += 1
        if b64:
            try:
                import base64
                out_path.write_bytes(base64.b64decode(b64))
                saved.append(str(out_path))
                continue
            except Exception as e:
                # Fall through to URL fetch.
                pass
        if url:
            try:
                req2 = urllib.request.Request(url)
                with urllib.request.urlopen(req2, timeout=120) as r:
                    out_path.write_bytes(r.read())
                saved.append(str(out_path))
                continue
            except Exception as e:
                saved.append(url)  # at least give the URL back
                continue
        # Neither — note this entry as failed.
    if not saved:
        return False, [], "API returned no usable image data"
    return True, saved, f"generated {len(saved)} image(s)"


# ---------- Tool subclass ----------


class ImageGen(Tool):
    """Generate one or more images from a text prompt."""

    name = "image_gen"
    description = (
        "Generate images from a text prompt. Returns the file paths "
        "of saved images (or URLs when the provider doesn't support "
        "base64). Output saves to ~/.janus/images/. Use sparingly — "
        "each call costs money. Provider/model configurable via "
        "JANUS_IMAGE_PROVIDER and JANUS_IMAGE_MODEL env vars."
    )
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": (
                    "What the image should show. Concrete is better "
                    "than abstract. ≤4000 chars (most providers cap "
                    "around there)."
                ),
            },
            "size": {
                "type": "string",
                "description": (
                    "Image dimensions. DALL-E 3 supports 1024x1024 "
                    "(default), 1792x1024, 1024x1792."
                ),
            },
            "n": {
                "type": "integer",
                "description": (
                    "How many images to generate. DALL-E 3 only "
                    "supports n=1. Default 1."
                ),
            },
        },
        "required": ["prompt"],
    }
    risk = "exec"  # costs money, writes files

    def run(self, args: dict, approver: Callable[..., bool]) -> str:
        prompt = (args.get("prompt") or "").strip()
        if not prompt:
            return "error: prompt is required"
        size = str(args.get("size") or "1024x1024").strip()
        try:
            n = int(args.get("n") or 1)
        except (TypeError, ValueError):
            n = 1
        n = max(1, min(n, 4))  # cap at 4

        # Approver — risk='exec', capability 'image.gen.<provider>'
        provider = _provider()
        details = (
            f"image_gen via {provider}: prompt={prompt[:120]!r}, "
            f"size={size}, n={n}"
        )
        if not approver(
            "image_gen",
            details,
            capability=("image", "gen", provider),
        ):
            return f"refused by user: image_gen({prompt[:60]!r})"

        # Audit (v1.33.5)
        try:
            from .. import audit_log
            audit_log.record(
                "image.gen",
                provider=provider,
                model=_model(),
                size=size,
                n=n,
                prompt_preview=prompt[:120],
            )
        except Exception:
            pass

        if provider == "openai-dalle":
            ok, paths, msg = _generate_openai_dalle(prompt, size=size, n=n)
        else:
            return (
                f"error: provider {provider!r} not supported; "
                f"set JANUS_IMAGE_PROVIDER=openai-dalle (or wait for "
                f"future support)"
            )

        if not ok:
            return f"error: {msg}"
        return f"{msg}\n" + "\n".join(paths)
