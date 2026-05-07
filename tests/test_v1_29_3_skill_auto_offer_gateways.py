"""Tests for v1.29.3 — per-surface skill auto-offer parity.

v1.28.1 wired the auto-offer in cli_rich's after-turn flow.
v1.29.3 extends the same pattern to telegram + web gateways with
identical thresholds (AUTO_OFFER_MIN_OCCURRENCES) and cooldown
(mark_offered after surfacing).

Source-pin tests: the gateway code calls skill_proposer.list_offerable
+ checks the threshold + calls mark_offered, all wrapped in
try/except so detection failure can't break the chat reply.
"""

from __future__ import annotations

import inspect
import re

from janus import skill_proposer
from janus.gateways import telegram as tg_mod
from janus.gateways import web as web_mod


_MARKER = "v1.29.3: skill auto-offer parity"


# ============================================================
# Telegram gateway source-pin
# ============================================================


def test_telegram_post_turn_calls_list_offerable():
    src = inspect.getsource(tg_mod)
    assert _MARKER in src, "v1.29.3 marker missing in telegram gateway"
    # The block must call skill_proposer.list_offerable
    region_start = src.find(_MARKER)
    region = src[region_start:region_start + 2000]
    assert "skill_proposer" in region
    assert "list_offerable" in region


def test_telegram_uses_threshold_constant():
    """Source-pin: the offer compares against
    AUTO_OFFER_MIN_OCCURRENCES, not a magic number — env override
    actually changes behavior."""
    src = inspect.getsource(tg_mod)
    region_start = src.find(_MARKER)
    region = src[region_start:region_start + 2000]
    assert "AUTO_OFFER_MIN_OCCURRENCES" in region


def test_telegram_marks_offered():
    src = inspect.getsource(tg_mod)
    region_start = src.find(_MARKER)
    region = src[region_start:region_start + 2000]
    assert "mark_offered" in region


def test_telegram_block_wrapped_in_try_except():
    """The auto-offer must NEVER break the chat reply. Wrapped in
    try/except Exception."""
    src = inspect.getsource(tg_mod)
    region_start = src.find(_MARKER)
    region = src[region_start:region_start + 2000]
    assert "try:" in region
    assert "except Exception:" in region


def test_telegram_top_only_no_iteration():
    """One offer per turn — index patterns[0], no for-loop over
    the patterns list within this block."""
    src = inspect.getsource(tg_mod)
    region_start = src.find(_MARKER)
    region = src[region_start:region_start + 2000]
    assert "patterns[0]" in region
    block_lines = region.split("\n")[:30]
    has_iter_over_patterns = any(
        ln.strip().startswith("for ") and "patterns" in ln
        for ln in block_lines
    )
    assert not has_iter_over_patterns


def test_telegram_after_inferred_offer_block():
    """The skill auto-offer fires AFTER the inferred-memory offer
    so both fit in the same after-turn cadence as cli_rich."""
    src = inspect.getsource(tg_mod)
    inf_idx = src.find("interview_inferred")
    sp_idx = src.find(_MARKER)
    assert -1 < inf_idx < sp_idx


# ============================================================
# Web gateway source-pin
# ============================================================


def test_web_post_turn_calls_list_offerable():
    src = inspect.getsource(web_mod)
    assert _MARKER in src
    region_start = src.find(_MARKER)
    region = src[region_start:region_start + 2000]
    assert "skill_proposer" in region
    assert "list_offerable" in region


def test_web_uses_threshold_constant():
    src = inspect.getsource(web_mod)
    region_start = src.find(_MARKER)
    region = src[region_start:region_start + 2000]
    assert "AUTO_OFFER_MIN_OCCURRENCES" in region


def test_web_marks_offered():
    src = inspect.getsource(web_mod)
    region_start = src.find(_MARKER)
    region = src[region_start:region_start + 2000]
    assert "mark_offered" in region


def test_web_block_wrapped_in_try_except():
    src = inspect.getsource(web_mod)
    region_start = src.find(_MARKER)
    region = src[region_start:region_start + 2000]
    assert "try:" in region
    assert "except Exception:" in region


def test_web_appends_to_drip_suffix():
    """Source-pin: web inserts the offer line into the drip_suffix
    accumulator (not a separate response field) so it surfaces
    inline with the chat reply, same shape as inferred offers."""
    src = inspect.getsource(web_mod)
    region_start = src.find(_MARKER)
    region = src[region_start:region_start + 2000]
    assert "drip_suffix" in region


def test_web_top_only_no_iteration():
    src = inspect.getsource(web_mod)
    region_start = src.find(_MARKER)
    region = src[region_start:region_start + 2000]
    assert "patterns[0]" in region


def test_web_after_inferred_offer_block():
    src = inspect.getsource(web_mod)
    inf_idx = src.find("interview_inferred")
    sp_idx = src.find(_MARKER)
    assert -1 < inf_idx < sp_idx


# ============================================================
# Cross-surface parity invariants
# ============================================================


def test_all_three_surfaces_use_same_threshold_constant():
    """cli_rich + telegram + web all reach for
    AUTO_OFFER_MIN_OCCURRENCES so flipping the env var changes
    behavior uniformly."""
    from janus import cli_rich
    for module in (cli_rich, tg_mod, web_mod):
        src = inspect.getsource(module)
        # cli_rich uses the v1.28.1 marker; gateways use v1.29.3
        assert "AUTO_OFFER_MIN_OCCURRENCES" in src, (
            f"{module.__name__} doesn't reach for the threshold constant"
        )


def test_all_three_surfaces_call_mark_offered():
    """Cooldown integrity: every surface that surfaces an offer
    must mark_offered, otherwise the same pattern would re-fire
    every turn from that surface."""
    from janus import cli_rich
    for module in (cli_rich, tg_mod, web_mod):
        src = inspect.getsource(module)
        assert "mark_offered" in src, (
            f"{module.__name__} doesn't call mark_offered"
        )


def test_all_three_surfaces_filter_by_min_occurrences():
    """Each surface must check `>=` the threshold before surfacing."""
    from janus import cli_rich
    for module in (cli_rich, tg_mod, web_mod):
        src = inspect.getsource(module)
        # >=AUTO_OFFER_MIN_OCCURRENCES (whitespace flexible)
        pattern = r">=\s*_sp\.AUTO_OFFER_MIN_OCCURRENCES"
        assert re.search(pattern, src), (
            f"{module.__name__} doesn't gate on the threshold"
        )
