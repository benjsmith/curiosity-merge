#!/usr/bin/env python3
"""Three-way merge of one wiki page.

Used when the same page was written locally and on another graph.
Frontmatter list keys are unioned. A body that only one side changed
is kept. A body both sides changed becomes a conflict: this script
never picks a winner and never overwrites either body.

Prints one JSON object on stdout.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

LIST_KEYS = ("sources", "supports", "contradicts", "same_as", "projects", "aliases", "labels")


def _split(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    raw = text[4:end]
    body = text[end + 4:]
    if body.startswith("\n"):
        body = body[1:]
    fm: dict[str, str] = {}
    for line in raw.splitlines():
        if not line or line[0] in " \t" or ":" not in line:
            continue
        key, _, val = line.partition(":")
        fm[key.strip()] = val.strip()
    return fm, body


def _parse_list(val: str) -> list[str]:
    text = (val or "").strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    if not text:
        return []
    return [part.strip().strip('"').strip("'") for part in text.split(",") if part.strip()]


def _emit(fm: dict[str, str], body: str) -> str:
    lines = ["---"]
    for key in sorted(fm):
        lines.append(f"{key}: {fm[key]}")
    lines.append("---")
    lines.append("")
    lines.append(body.rstrip())
    lines.append("")
    return "\n".join(lines)


def merge_texts(base: str, ours: str, theirs: str) -> dict:
    bfm, bbody = _split(base)
    ofm, obody = _split(ours)
    tfm, tbody = _split(theirs)
    conflicts: list[dict] = []
    unioned: list[str] = []
    out = dict(ofm)
    keys = set(bfm) | set(ofm) | set(tfm)
    for key in sorted(keys):
        if key in LIST_KEYS:
            merged = []
            for item in _parse_list(ofm.get(key, "")) + _parse_list(tfm.get(key, "")):
                if item not in merged:
                    merged.append(item)
            if merged:
                out[key] = "[" + ", ".join(merged) + "]"
                unioned.append(key)
            continue
        bv, ov, tv = bfm.get(key), ofm.get(key), tfm.get(key)
        if ov == tv:
            if ov is not None:
                out[key] = ov
            else:
                out.pop(key, None)
            continue
        if ov == bv:
            if tv is None:
                out.pop(key, None)
            else:
                out[key] = tv
            continue
        if tv == bv:
            if ov is None:
                out.pop(key, None)
            else:
                out[key] = ov
            continue
        conflicts.append({"kind": "frontmatter", "key": key, "ours": ov, "theirs": tv})
        if ov is not None:
            out[key] = ov
    ob = obody.strip()
    tb = tbody.strip()
    bb = bbody.strip()
    if ob == tb or tb == bb:
        body = obody
    elif ob == bb:
        body = tbody
    else:
        conflicts.append({"kind": "body", "ours": obody, "theirs": tbody})
        body = obody
    merged = not any(item["kind"] == "body" or item["kind"] == "frontmatter" for item in conflicts)
    # List unions are not conflicts. Scalar disagreements and body
    # disagreements are. `merged` is true only when nothing needs a person.
    return {
        "merged": merged,
        "content": _emit(out, body) if merged else "",
        "ours": ours,
        "theirs": theirs,
        "conflicts": conflicts,
        "union_keys": unioned,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="page_merge.py")
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--ours", type=Path, required=True)
    parser.add_argument("--theirs", type=Path, required=True)
    args = parser.parse_args(argv)
    result = merge_texts(
        args.base.read_text(encoding="utf-8"),
        args.ours.read_text(encoding="utf-8"),
        args.theirs.read_text(encoding="utf-8"),
    )
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return 0 if result["merged"] else 1


if __name__ == "__main__":
    sys.exit(main())
