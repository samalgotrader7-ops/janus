import json
from janus import index, config, logger


def _write_record(rec: dict) -> None:
    config.ensure_home()
    with config.LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def test_sync_empty(janus_home):
    assert index.sync() == 0
    assert index.stats()["rows"] == 0


def test_sync_incremental(janus_home):
    _write_record({"ts": "t1", "request": "merge two excel files",
                   "choice": 1, "output": "ok done", "trace": []})
    _write_record({"ts": "t2", "request": "deploy to vps",
                   "choice": 2, "output": "deployed",
                   "trace": [{"type": "tool_call", "tool": "shell"}]})
    n = index.sync()
    assert n == 2

    # Second sync should find no new rows.
    n2 = index.sync()
    assert n2 == 0


def test_search_finds_records(janus_home):
    _write_record({"ts": "t1", "request": "merge two excel files",
                   "choice": 1, "output": "ok done", "trace": []})
    _write_record({"ts": "t2", "request": "deploy to vps",
                   "choice": 2, "output": "deployed",
                   "trace": [{"type": "tool_call", "tool": "shell"}]})
    index.sync()
    hits = index.search("excel")
    assert any("excel" in h.request for h in hits)


def test_rebuild(janus_home):
    _write_record({"ts": "t1", "request": "alpha", "trace": []})
    index.sync()
    assert index.stats()["rows"] == 1

    _write_record({"ts": "t2", "request": "beta", "trace": []})
    n = index.rebuild()
    assert n == 2


def test_recent(janus_home):
    for i in range(5):
        _write_record({"ts": f"t{i}", "request": f"req-{i}", "trace": []})
    index.sync()
    hits = index.recent(k=3)
    assert len(hits) == 3
    # Most recent first.
    assert hits[0].ts == "t4"


def test_search_by_tool(janus_home):
    _write_record({"ts": "t1", "request": "no tools here",
                   "trace": []})
    _write_record({"ts": "t2", "request": "ran shell",
                   "trace": [{"type": "tool_call", "tool": "shell"}]})
    index.sync()
    hits = index.search_by_tool("shell")
    assert len(hits) == 1
    assert hits[0].request == "ran shell"
