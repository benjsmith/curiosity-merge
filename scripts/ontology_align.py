#!/usr/bin/env python3
"""Compare two meta-ontology files and adapt one page toward a target.

Same name and same endpoints count as the same term. Same name with
different endpoints is a conflict and is not merged. Built-in page
types always match. A recorded map can say that a remote term means a
local one. Terms that still do not match are rewritten to the closest
built-in type, and the original term is reported so the caller can
record it. Nothing here invents a shared ontology.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BUILTIN_TYPES = (
    "source", "entity", "concept", "analysis", "evidence", "fact",
    "table", "figure", "note", "hypothesis", "summary-table", "extracted-table",
)


def _load(path: Path) -> dict:
    if not path.is_file():
        return {"nodes": [], "edges": []}
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(text)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {"nodes": [], "edges": [], "raw": text}


def _nodes(data: dict) -> dict[str, dict]:
    out = {}
    for row in data.get("nodes") or []:
        if isinstance(row, dict) and row.get("type"):
            out[str(row["type"])] = row
    return out


def _edges(data: dict) -> dict[str, dict]:
    out = {}
    for row in data.get("edges") or []:
        if isinstance(row, dict) and row.get("type"):
            out[str(row["type"])] = row
    return out


def _ends(row: dict) -> tuple[tuple[str, ...], tuple[str, ...]]:
    def norm(val) -> tuple[str, ...]:
        if val is None:
            return ()
        if isinstance(val, str):
            return (val,)
        if isinstance(val, list):
            return tuple(str(x) for x in val)
        return (str(val),)
    return norm(row.get("source")), norm(row.get("target"))


def _approved(path: Path | None) -> dict[str, str]:
    if path is None or not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8")
    mapping: dict[str, str] = {}
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(text) or {}
        for row in data.get("map") or []:
            if isinstance(row, dict) and row.get("remote") and row.get("local"):
                mapping[str(row["remote"])] = str(row["local"])
        return mapping
    except Exception:
        pass
    return mapping


def compare(local: dict, remote: dict, approved: dict[str, str] | None = None) -> dict:
    approved = approved or {}
    lnodes, rnodes = _nodes(local), _nodes(remote)
    identical = [name for name in BUILTIN_TYPES]
    conflicts = []
    remote_only = []
    local_only = []
    mapped = [{"remote": src, "local": dst} for src, dst in sorted(approved.items())]
    names = set(lnodes) | set(rnodes) | set(BUILTIN_TYPES)
    for name in sorted(names):
        if name in BUILTIN_TYPES:
            continue
        if name in approved:
            continue
        if name in lnodes and name in rnodes:
            identical.append(name)
        elif name in rnodes:
            remote_only.append(name)
        else:
            local_only.append(name)
    ledges, redges = _edges(local), _edges(remote)
    edge_conflicts = []
    edge_identical = []
    for name in sorted(set(ledges) | set(redges)):
        if name in ledges and name in redges:
            if _ends(ledges[name]) == _ends(redges[name]):
                edge_identical.append(name)
            else:
                edge_conflicts.append({
                    "name": name,
                    "local": {"source": list(_ends(ledges[name])[0]),
                              "target": list(_ends(ledges[name])[1])},
                    "remote": {"source": list(_ends(redges[name])[0]),
                               "target": list(_ends(redges[name])[1])},
                })
        elif name in redges:
            remote_only.append(name)
        else:
            local_only.append(name)
    return {
        "identical": identical,
        "map": mapped,
        "conflicts": conflicts + edge_conflicts,
        "remote_only": remote_only,
        "local_only": local_only,
        "edge_identical": edge_identical,
    }


def _split(text: str) -> tuple[list[str], str]:
    if not text.startswith("---"):
        return [], text
    end = text.find("\n---", 3)
    if end < 0:
        return [], text
    return text[4:end].splitlines(), text[end + 4:]


def adapt_page(text: str, mapping: dict, target_types: set[str]) -> tuple[str, list[dict]]:
    """Return a page that uses the target vocabulary, plus a record of changes."""
    lines, body = _split(text)
    if not lines and text.startswith("---") is False:
        lines = []
    notes: list[dict] = []
    approved = {}
    for row in mapping.get("map") or []:
        if isinstance(row, dict) and row.get("remote") and row.get("local"):
            approved[str(row["remote"])] = str(row["local"])
    allowed = set(target_types) | set(BUILTIN_TYPES) | set(approved.values())
    out_lines = []
    for line in lines:
        if ":" not in line or (line[:1] in " \t"):
            out_lines.append(line)
            continue
        key, _, val = line.partition(":")
        key_s = key.strip()
        val_s = val.strip().strip('"').strip("'")
        if key_s == "type":
            new = approved.get(val_s, val_s)
            if new not in allowed:
                notes.append({"from": val_s, "to": "concept", "field": "type"})
                new = "concept"
            elif new != val_s:
                notes.append({"from": val_s, "to": new, "field": "type"})
            out_lines.append(f"type: {new}")
            continue
        new_key = approved.get(key_s, key_s)
        if new_key != key_s:
            notes.append({"from": key_s, "to": new_key, "field": "key"})
            out_lines.append(f"{new_key}: {val.strip()}")
        else:
            out_lines.append(line)
    if lines:
        content = "---\n" + "\n".join(out_lines) + "\n---" + body
        if not body.startswith("\n"):
            # body from split already includes the leading newline when
            # the source had a blank line; keep it.
            pass
    else:
        content = text
    return content, notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ontology_align.py")
    sub = parser.add_subparsers(dest="cmd", required=True)
    cmp_ = sub.add_parser("compare")
    cmp_.add_argument("--local", type=Path, required=True)
    cmp_.add_argument("--remote", type=Path, required=True)
    cmp_.add_argument("--map", type=Path, default=None)
    ad = sub.add_parser("adapt")
    ad.add_argument("--page", type=Path, required=True)
    ad.add_argument("--map", type=Path, required=True)
    ad.add_argument("--target", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.cmd == "compare":
        result = compare(_load(args.local), _load(args.remote), _approved(args.map))
        json.dump(result, sys.stdout)
        sys.stdout.write("\n")
        return 0
    mapping = json.loads(args.map.read_text(encoding="utf-8")) if args.map.suffix == ".json" else {
        "map": [],
    }
    if args.map.suffix != ".json":
        mapping = compare({}, {}, _approved(args.map))
        # keep the approved pairs; compare() stored them under map
    target = _load(args.target)
    types = set(_nodes(target))
    content, notes = adapt_page(args.page.read_text(encoding="utf-8"), mapping, types)
    json.dump({"content": content, "notes": notes}, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
