"""
tools/vision.py — Phase 9: image description via the configured LLM.

Routes through `llm.chat` with multimodal content. Requires the
configured model to be vision-capable (e.g. gpt-4o, claude-sonnet-4-6,
gemini-1.5-pro). If the model isn't, the API will return an error and
we surface it.
"""

from __future__ import annotations
import base64
from typing import Callable

from . import base
from .. import llm
from .fs import _resolve_within_workspace


_SUPPORTED_EXTS = ("png", "jpg", "jpeg", "gif", "webp")


class ImageDescribe(base.Tool):
    name = "image_describe"
    description = (
        "Describe the contents of an image file (workspace-bounded). "
        "Routes through the configured LLM; the model must be "
        "vision-capable. Returns the model's textual description."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to image file inside workspace."},
            "prompt": {
                "type": "string",
                "description": "What to look for / how to describe (default: general 2-3 sentence description).",
            },
        },
        "required": ["path"],
    }
    dangerous = False
    risk = "read"

    def run(self, args: dict, approver: Callable[..., bool]) -> str:
        try:
            p = _resolve_within_workspace(args["path"])
        except ValueError as e:
            return f"error: {e}"
        if not p.exists() or not p.is_file():
            return f"error: not a file: {args['path']}"
        ext = p.suffix.lower().lstrip(".")
        if ext not in _SUPPORTED_EXTS:
            return f"error: unsupported image type .{ext} (need {', '.join(_SUPPORTED_EXTS)})"
        try:
            data = p.read_bytes()
        except Exception as e:
            return f"error: read failed: {e}"
        b64 = base64.b64encode(data).decode("ascii")
        prompt = args.get("prompt") or "Describe this image in 2-3 sentences."
        try:
            msg = llm.chat(
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/{ext};base64,{b64}",
                            },
                        },
                    ],
                }],
                temperature=0.2,
            )
        except Exception as e:
            return f"error: {type(e).__name__}: {e}"
        return (msg.get("content") or "").strip() or "(empty response)"
