"""Every Tool subclass must declare a risk class — v1.0 invariant.

Without a risk tag the mode-aware approver falls back to the most
restrictive ('exec'), which would ASK on every tool call regardless of
mode. That regression would silently break the v1.0 UX, so we assert
the tag at import time.
"""
from __future__ import annotations

from janus import permissions
from janus.tools import default_registry
from janus.tools.base import Tool


def test_every_bundled_tool_has_known_risk_class():
    reg = default_registry()
    for name in reg.names():
        tool = reg._tools[name]
        assert hasattr(tool, "risk"), f"{name}: missing 'risk' attribute"
        assert tool.risk in permissions.ALL_RISKS, (
            f"{name}: risk={tool.risk!r} not in {permissions.ALL_RISKS}"
        )


def test_registry_injects_risk_into_approver_kwargs():
    """When a tool calls its approver, the Registry must inject `risk=`
    matching the tool's class attribute, even if the tool itself didn't
    pass it. This is the seam that lets the mode-aware approver decide
    without each tool opting in."""
    captured: dict = {}

    class Probe(Tool):
        name = "probe"
        description = "test"
        parameters = {"type": "object", "properties": {}}
        dangerous = True
        risk = "write"

        def run(self, args, approver):
            ok = approver("probe action", "probe details")
            return f"ok={ok}"

    from janus.tools.base import Registry

    def approver(label, details, **kw):
        captured.update(kw)
        return True

    reg = Registry([Probe()])
    out = reg.call("probe", {}, approver)

    assert out == "ok=True"
    assert captured.get("risk") == "write"


def test_tool_explicit_risk_kwarg_wins_over_class_default():
    """If a tool explicitly passes risk=, it should not be overridden
    by the registry default. setdefault(), not assignment."""
    captured: dict = {}

    class Probe(Tool):
        name = "probe"
        description = "test"
        parameters = {"type": "object", "properties": {}}
        dangerous = True
        risk = "write"

        def run(self, args, approver):
            return f"ok={approver('label', 'details', risk='exec')}"

    from janus.tools.base import Registry

    def approver(label, details, **kw):
        captured.update(kw)
        return True

    reg = Registry([Probe()])
    reg.call("probe", {}, approver)
    assert captured["risk"] == "exec"  # tool-provided value wins
