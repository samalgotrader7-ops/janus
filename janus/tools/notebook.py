"""
tools/notebook.py — Phase 9: read/edit Jupyter notebooks (.ipynb).

The .ipynb format is JSON, so we parse with stdlib `json` rather than
adding `nbformat` as a dep (P6 — fewer SDKs in the path).

NbRead is read-only (workspace-bounded).
NbEdit is dangerous=True; per-call approver, capability=("nb", "edit", path).
"""

from __future__ import annotations
import json
from typing import Callable

from . import base
from .fs import _resolve_within_workspace


class NbRead(base.Tool):
    name = "nb_read"
    description = (
        "Read a Jupyter notebook (.ipynb). Returns each cell with its "
        "type and source, separated by markers."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path relative to workspace."},
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
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            return f"error: parse failed: {type(e).__name__}: {e}"
        cells = data.get("cells")
        if not isinstance(cells, list):
            return "error: malformed notebook (cells not a list)"
        if not cells:
            return "(empty notebook)"
        out = []
        for i, cell in enumerate(cells):
            ctype = cell.get("cell_type", "unknown")
            src = cell.get("source", "")
            if isinstance(src, list):
                src = "".join(src)
            out.append(f"--- cell {i} [{ctype}] ---\n{src}")
        return "\n\n".join(out)


class NbEdit(base.Tool):
    name = "nb_edit"
    description = (
        "Modify a cell in a Jupyter notebook. Operations: "
        "'replace' (overwrite source of cell at cell_index), "
        "'insert' (add new cell at cell_index), "
        "'delete' (remove cell at cell_index). "
        "DESTRUCTIVE — requires approval."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "cell_index": {"type": "integer"},
            "operation": {"type": "string", "enum": ["replace", "insert", "delete"]},
            "source": {"type": "string", "description": "New cell source for replace/insert."},
            "cell_type": {
                "type": "string",
                "enum": ["code", "markdown", "raw"],
                "description": "Type for insert (default 'code').",
            },
        },
        "required": ["path", "cell_index", "operation"],
    }
    dangerous = True
    risk = "write"

    def run(self, args: dict, approver: Callable[..., bool]) -> str:
        try:
            p = _resolve_within_workspace(args["path"])
        except ValueError as e:
            return f"error: {e}"
        if not p.exists() or not p.is_file():
            return f"error: not a file: {args['path']}"
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            return f"error: parse failed: {type(e).__name__}: {e}"
        cells = data.get("cells")
        if not isinstance(cells, list):
            return "error: malformed notebook (cells not a list)"

        try:
            idx = int(args["cell_index"])
        except (TypeError, ValueError):
            return "error: cell_index must be an integer"
        op = args.get("operation")

        if op not in ("replace", "insert", "delete"):
            return f"error: unknown operation: {op}"

        details = f"{op} cell {idx} in {args['path']}"
        if not approver(
            "nb_edit",
            details,
            capability=("nb", "edit", args["path"]),
        ):
            return f"refused by user: edit {args['path']}"

        if op == "delete":
            if idx < 0 or idx >= len(cells):
                return f"error: cell_index {idx} out of range (0..{len(cells)-1})"
            cells.pop(idx)
        elif op == "replace":
            if idx < 0 or idx >= len(cells):
                return f"error: cell_index {idx} out of range (0..{len(cells)-1})"
            cells[idx]["source"] = str(args.get("source") or "")
        elif op == "insert":
            ctype = args.get("cell_type") or "code"
            new_cell: dict = {
                "cell_type": ctype,
                "source": str(args.get("source") or ""),
                "metadata": {},
            }
            if ctype == "code":
                new_cell["execution_count"] = None
                new_cell["outputs"] = []
            cells.insert(max(0, min(idx, len(cells))), new_cell)

        data["cells"] = cells
        p.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        return f"applied {op} on cell {idx} in {args['path']}"
