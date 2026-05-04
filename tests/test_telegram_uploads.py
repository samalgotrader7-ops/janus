"""Tests for v1.5.1 phase 3: Telegram photo + document upload handling.

Bug: User sent screenshots and Janus replied "I don't see any image
file in the workspace." Photos and document attachments never reached
any callback because telegram.py only registered a TEXT handler.

Fix: on_photo / on_document handlers download to ~/.janus/uploads/<chat_id>/
and inject the path as a synthetic chat turn the model can process.
"""
from __future__ import annotations
import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from janus.gateways import telegram


# ---------- Source-level pin: handlers exist + are registered ----------


def test_on_photo_handler_defined():
    assert hasattr(telegram, "on_photo")
    assert inspect.iscoroutinefunction(telegram.on_photo)


def test_on_document_handler_defined():
    assert hasattr(telegram, "on_document")
    assert inspect.iscoroutinefunction(telegram.on_document)


def test_run_chat_turn_helper_defined():
    """The shared chat-flow helper that on_text / on_photo / on_document call."""
    assert hasattr(telegram, "_run_chat_turn")
    assert inspect.iscoroutinefunction(telegram._run_chat_turn)


def test_handlers_registered_in_main():
    """Inspect the source of `main` to confirm both handlers are wired."""
    src = inspect.getsource(telegram)
    # PHOTO filter
    assert "filters.PHOTO" in src
    assert "on_photo" in src
    # Document filter
    assert "filters.Document.ALL" in src
    assert "on_document" in src


# ---------- on_photo behavior ----------


@pytest.mark.asyncio
async def test_on_photo_unauthorized_short_circuits(monkeypatch):
    """Unauthorized chat triggers the pairing prompt and never downloads."""
    monkeypatch.setattr(telegram, "_is_authorized", lambda chat_id: False)

    pair_calls = []

    async def fake_send_pairing(update):
        pair_calls.append(update)

    monkeypatch.setattr(telegram, "_send_pairing_prompt", fake_send_pairing)

    update = MagicMock()
    update.effective_chat.id = 12345
    update.message.photo = [MagicMock()]

    await telegram.on_photo(update, MagicMock())
    assert len(pair_calls) == 1


@pytest.mark.asyncio
async def test_on_photo_downloads_and_acks(monkeypatch, tmp_path):
    """Authorized photo: file downloaded to uploads dir, ack sent,
    chat-turn helper invoked with the path injected."""
    monkeypatch.setattr(telegram, "_is_authorized", lambda chat_id: True)
    monkeypatch.setattr(telegram.config, "HOME", tmp_path)

    # Stub session lookup
    fake_sess = MagicMock()
    monkeypatch.setattr(telegram, "_session", lambda chat_id: fake_sess)

    # Capture the chat-turn invocation
    chat_calls: list = []

    async def fake_run_chat_turn(update, ctx, chat_id, sess, req):
        chat_calls.append({"chat_id": chat_id, "req": req})

    monkeypatch.setattr(telegram, "_run_chat_turn", fake_run_chat_turn)

    # Build a fake update with a photo
    fake_file = MagicMock()

    async def fake_download(custom_path):
        # Simulate successful download by creating the file
        from pathlib import Path
        Path(custom_path).write_bytes(b"fake image data")

    fake_file.download_to_drive = fake_download

    fake_photo = MagicMock()

    async def fake_get_file():
        return fake_file
    fake_photo.get_file = fake_get_file

    update = MagicMock()
    update.effective_chat.id = 99
    update.message.photo = [fake_photo]
    update.message.caption = None
    update.message.reply_text = AsyncMock()

    await telegram.on_photo(update, MagicMock())

    # Acked
    update.message.reply_text.assert_called_once()
    ack_text = update.message.reply_text.call_args.args[0]
    assert "received image" in ack_text

    # Chat turn invoked with [user uploaded image at <path>]
    assert len(chat_calls) == 1
    assert "[user uploaded image at" in chat_calls[0]["req"]
    assert chat_calls[0]["chat_id"] == 99

    # File on disk
    upload_files = list((tmp_path / "uploads" / "99").iterdir())
    assert len(upload_files) == 1
    assert upload_files[0].name.startswith("photo_")
    assert upload_files[0].suffix == ".jpg"


@pytest.mark.asyncio
async def test_on_photo_caption_prefixed_to_request(monkeypatch, tmp_path):
    """If user sent a caption with the photo, caption + path both flow
    to the model."""
    monkeypatch.setattr(telegram, "_is_authorized", lambda chat_id: True)
    monkeypatch.setattr(telegram.config, "HOME", tmp_path)
    monkeypatch.setattr(telegram, "_session", lambda chat_id: MagicMock())

    chat_calls: list = []

    async def fake_run_chat_turn(update, ctx, chat_id, sess, req):
        chat_calls.append(req)
    monkeypatch.setattr(telegram, "_run_chat_turn", fake_run_chat_turn)

    fake_file = MagicMock()
    async def fake_download(custom_path):
        from pathlib import Path
        Path(custom_path).write_bytes(b"x")
    fake_file.download_to_drive = fake_download

    fake_photo = MagicMock()
    async def fake_get_file():
        return fake_file
    fake_photo.get_file = fake_get_file

    update = MagicMock()
    update.effective_chat.id = 1
    update.message.photo = [fake_photo]
    update.message.caption = "what's in this image?"
    update.message.reply_text = AsyncMock()

    await telegram.on_photo(update, MagicMock())

    req = chat_calls[0]
    assert req.startswith("what's in this image?")
    assert "[user uploaded image at" in req


@pytest.mark.asyncio
async def test_on_photo_download_failure_acks_error(monkeypatch, tmp_path):
    monkeypatch.setattr(telegram, "_is_authorized", lambda chat_id: True)
    monkeypatch.setattr(telegram.config, "HOME", tmp_path)
    monkeypatch.setattr(telegram, "_session", lambda chat_id: MagicMock())

    chat_calls: list = []
    async def fake_run_chat_turn(*a, **kw):
        chat_calls.append(a)
    monkeypatch.setattr(telegram, "_run_chat_turn", fake_run_chat_turn)

    fake_photo = MagicMock()
    async def boom():
        raise RuntimeError("network down")
    fake_photo.get_file = boom

    update = MagicMock()
    update.effective_chat.id = 1
    update.message.photo = [fake_photo]
    update.message.caption = None
    update.message.reply_text = AsyncMock()

    await telegram.on_photo(update, MagicMock())

    # Error message sent, chat turn NOT invoked
    update.message.reply_text.assert_called_once()
    err = update.message.reply_text.call_args.args[0]
    assert "failed" in err.lower()
    assert chat_calls == []


# ---------- on_document behavior ----------


@pytest.mark.asyncio
async def test_on_document_downloads_with_safe_filename(monkeypatch, tmp_path):
    """Document filename gets sanitized — no path traversal, no metachars."""
    monkeypatch.setattr(telegram, "_is_authorized", lambda chat_id: True)
    monkeypatch.setattr(telegram.config, "HOME", tmp_path)
    monkeypatch.setattr(telegram, "_session", lambda chat_id: MagicMock())

    chat_calls: list = []
    async def fake_run(update, ctx, chat_id, sess, req):
        chat_calls.append(req)
    monkeypatch.setattr(telegram, "_run_chat_turn", fake_run)

    fake_file = MagicMock()
    async def fake_download(custom_path):
        from pathlib import Path
        Path(custom_path).write_bytes(b"data")
    fake_file.download_to_drive = fake_download

    fake_doc = MagicMock()
    fake_doc.file_name = "../../../etc/passwd"  # traversal attempt
    fake_doc.file_size = 100
    async def fake_get_file():
        return fake_file
    fake_doc.get_file = fake_get_file

    update = MagicMock()
    update.effective_chat.id = 7
    update.message.document = fake_doc
    update.message.caption = None
    update.message.reply_text = AsyncMock()

    await telegram.on_document(update, MagicMock())

    # File landed under the chat's upload dir, NOT outside it.
    upload_files = list((tmp_path / "uploads" / "7").iterdir())
    assert len(upload_files) == 1
    # Slash → underscore (no path components)
    assert "/" not in upload_files[0].name
    assert "\\" not in upload_files[0].name
    # Verify resolved path stays inside upload_dir (defense in depth).
    resolved = upload_files[0].resolve()
    upload_root = (tmp_path / "uploads" / "7").resolve()
    assert str(resolved).startswith(str(upload_root))
    # No real /etc/passwd read happened
    assert not (tmp_path / "etc" / "passwd").exists()


@pytest.mark.asyncio
async def test_on_document_with_no_filename_uses_fallback(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(telegram, "_is_authorized", lambda chat_id: True)
    monkeypatch.setattr(telegram.config, "HOME", tmp_path)
    monkeypatch.setattr(telegram, "_session", lambda chat_id: MagicMock())

    chat_calls: list = []
    async def fake_run(update, ctx, chat_id, sess, req):
        chat_calls.append(req)
    monkeypatch.setattr(telegram, "_run_chat_turn", fake_run)

    fake_file = MagicMock()
    async def fake_download(custom_path):
        from pathlib import Path
        Path(custom_path).write_bytes(b"x")
    fake_file.download_to_drive = fake_download

    fake_doc = MagicMock()
    fake_doc.file_name = None
    fake_doc.file_size = 50
    async def fake_get_file():
        return fake_file
    fake_doc.get_file = fake_get_file

    update = MagicMock()
    update.effective_chat.id = 1
    update.message.document = fake_doc
    update.message.caption = None
    update.message.reply_text = AsyncMock()

    await telegram.on_document(update, MagicMock())

    upload_files = list((tmp_path / "uploads" / "1").iterdir())
    assert len(upload_files) == 1
    assert upload_files[0].name.startswith("upload_")


@pytest.mark.asyncio
async def test_on_document_unauthorized_no_download(monkeypatch):
    monkeypatch.setattr(telegram, "_is_authorized", lambda chat_id: False)
    pair_calls = []
    async def fake_pp(update):
        pair_calls.append(update)
    monkeypatch.setattr(telegram, "_send_pairing_prompt", fake_pp)

    update = MagicMock()
    update.effective_chat.id = 99
    update.message.document = MagicMock()
    await telegram.on_document(update, MagicMock())
    assert len(pair_calls) == 1
