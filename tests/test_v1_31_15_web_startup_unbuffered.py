"""Tests for v1.31.15 — web startup banner reaches the log unbuffered.

FIELD-VALIDATION FINDING (Sam, 2026-05-09 evening):

After v1.31.14 added ``janus web UI v{VERSION} on http://...``
to serve()'s startup banner, Sam restarted with::

    nohup janus web > /tmp/janus-web.log 2>&1 & disown

Then ran ``head -3 /tmp/janus-web.log`` to verify the version was
visible. Output::

    nohup: ignoring input
    INFO:     Started server process [83748]
    INFO:     Waiting for application startup.

The version banner was MISSING. Root cause: when CPython detects
stdout is NOT a TTY (redirected to a file), it switches to block
buffering — typical buffer is 4-8KB. ``print()`` calls in
serve() sit in the buffer until enough activity flushes them.
Uvicorn's logging goes through stderr (line-buffered by default
+ merged via ``2>&1``), which is why uvicorn's lines appeared in
``head`` while the version banner did not.

The whole point of v1.31.14 was making staleness diagnosable
from a log head. The buffer ate the diagnostic.

THE FIX:

Two layers of belt-and-suspenders:
1. ``sys.stdout.reconfigure(line_buffering=True)`` — module-level
   primitive that flips stdout to line-buffer mode. Available on
   Python 3.7+. Wrapped in try/except so weird stdouts (closed
   file, custom wrapper) don't crash startup.
2. ``flush=True`` on the version + login prints specifically — so
   if reconfigure raised on someone's stdout, the critical lines
   still reach the log immediately.

DESIGN INVARIANTS PINNED:
  * sys imported at module top
  * sys.stdout.reconfigure(line_buffering=True) called inside
    serve(), early enough to affect the version banner
  * Wrapped in try/except for graceful degradation
  * Version + login prints have flush=True as backup
  * v1.31.15 marker comment present
  * branding.VERSION + pyproject version match
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _web_source() -> str:
    """Read gateways/web.py source text once for source pins."""
    p = Path(__file__).parent.parent / "janus" / "gateways" / "web.py"
    return p.read_text(encoding="utf-8")


def test_sys_imported_at_module_top():
    """``sys`` is now imported alongside the other stdlib modules
    so reconfigure(line_buffering=True) can be called without an
    inline late import."""
    src = _web_source()
    # Match the import block — sys should appear before pathlib
    head = src[: src.index("from pathlib")]
    assert "\nimport sys\n" in head


def test_serve_reconfigures_stdout_for_line_buffering():
    """Source-pin: serve() calls ``sys.stdout.reconfigure(
    line_buffering=True)`` so block-buffered stdouts (nohup +
    redirected file) flush every newline instead of waiting for
    4-8KB of data."""
    src = _web_source()
    serve_idx = src.index("def serve(")
    serve_block = src[serve_idx: serve_idx + 4500]
    assert "sys.stdout.reconfigure(line_buffering=True)" in serve_block


def test_reconfigure_call_wrapped_in_try_except():
    """Defensive: reconfigure() raises on closed/exotic stdouts.
    A try/except keeps startup resilient — startup failure due to
    a print configuration would be a worse regression than
    block-buffering itself."""
    src = _web_source()
    serve_idx = src.index("def serve(")
    serve_block = src[serve_idx: serve_idx + 4500]
    # The reconfigure call should be inside a try block
    reconfig_idx = serve_block.index(
        "sys.stdout.reconfigure(line_buffering=True)"
    )
    # Find the most recent 'try:' before the reconfigure call
    try_idx = serve_block.rfind("try:", 0, reconfig_idx)
    assert try_idx >= 0
    # And an except after it before the next major statement
    after_block = serve_block[reconfig_idx: reconfig_idx + 200]
    assert "except" in after_block


def test_version_print_uses_flush_true():
    """Belt-and-suspenders: even if reconfigure() raised on this
    stdout, the version banner itself uses flush=True so it lands
    in the log immediately. This is the line a user runs ``head -3``
    to verify version on a stale-process check."""
    src = _web_source()
    serve_idx = src.index("def serve(")
    serve_block = src[serve_idx: serve_idx + 4500]
    # Locate the version print and check flush=True is in the call.
    version_print_idx = serve_block.index("janus web UI v")
    # Look at the next 300 chars — covers the multi-line print(...).
    print_call = serve_block[version_print_idx: version_print_idx + 300]
    assert "flush=True" in print_call


def test_login_print_uses_flush_true():
    """Same belt-and-suspenders for the login URL line — useful
    diagnostic that a user typically pairs with the version line."""
    src = _web_source()
    serve_idx = src.index("def serve(")
    serve_block = src[serve_idx: serve_idx + 4500]
    login_print_idx = serve_block.index("login at http://")
    print_call = serve_block[login_print_idx: login_print_idx + 200]
    assert "flush=True" in print_call


def test_v1_31_15_marker_present():
    """v1.31.15 comment marker so future maintainers can grep
    for the field-validation context."""
    src = _web_source()
    assert "v1.31.15" in src


def test_version_bumped_to_1_31_15():
    """branding.VERSION + pyproject version match."""
    from janus import branding
    assert branding.VERSION == "1.31.15"
    pyproject_path = (
        Path(__file__).parent.parent / "pyproject.toml"
    )
    py_src = pyproject_path.read_text(encoding="utf-8")
    assert 'version = "1.31.15"' in py_src


# ---------- Behavioral pin: print(..., flush=True) writes immediately ----------


def test_python_print_flush_true_actually_flushes_to_redirected_file(tmp_path):
    """Sanity-check that ``print(..., flush=True)`` to a file does
    bypass block buffering. Pins the assumption v1.31.15 relies on
    against any future Python regression."""
    import subprocess
    import sys
    log_path = tmp_path / "out.log"
    # Run a child Python that prints with flush=True then sleeps so
    # we can inspect the log BEFORE the process exits (which would
    # flush on any buffer mode anyway).
    script = (
        "import time, sys\n"
        "print('FLUSHED_LINE', flush=True)\n"
        "time.sleep(0.5)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=open(log_path, "w"),
        stderr=subprocess.STDOUT,
    )
    # Give the print a moment to land — much less than the 0.5s sleep.
    import time
    time.sleep(0.15)
    # Read while the child is still asleep — proves the line got out
    # before process exit (which would flush regardless).
    contents = log_path.read_text()
    proc.wait()
    assert "FLUSHED_LINE" in contents


def test_python_print_no_flush_does_not_appear_quickly(tmp_path):
    """Negative pin: without flush=True, the print sits in the
    buffer when stdout is a redirected file. If this test ever
    fails (i.e., default print does flush quickly), CPython has
    changed buffering semantics and our flush=True is no longer
    necessary — but until then, this asserts the bug v1.31.15
    fixes is real."""
    import subprocess
    import sys
    import time
    log_path = tmp_path / "out.log"
    # Print without flush, then sleep so we can inspect during
    # the sleep window.
    script = (
        "import time\n"
        "print('UNFLUSHED_LINE')\n"
        "time.sleep(0.5)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=open(log_path, "w"),
        stderr=subprocess.STDOUT,
    )
    time.sleep(0.15)
    contents = log_path.read_text()
    proc.wait()
    # We expect the line NOT to be in the log yet — the buffer
    # is holding it. If CPython ever line-buffers redirected
    # stdouts by default, this test will fail and we can
    # simplify v1.31.15 (or roll it back).
    assert "UNFLUSHED_LINE" not in contents
