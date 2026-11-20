"""Evidence items that tie a step's physical reading to the Sentaurus manual or to a literature note.

An evidence block is ``{"manual": [{"chunk": "p0574", "quote": "..."}], "literature": [{"key": "name2020", "quote": "..."}]}``.
- A manual item must name a chunk of ``kb/manual`` and quote it verbatim (first 60 characters, whitespace and case
  normalized), as for model-patch proposals.
- A literature item must name a note ``kb/literature/<key>.md`` that records a DOI, and the quote must appear verbatim
  in that note. The note is where the agent recorded the source when it read it (skill literature-evidence).
Used by the run rule ``step_evidence`` (every accepted step after the baseline) and by the final ``reference_comparison``.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

QUOTE_CHARS = 60
DOI_IN_NOTE = re.compile(r"10\.\d{4,9}/\S+")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", str(s)).lower().strip(' ."“”')


def check(ws, block: Mapping[str, Any] | None, where: str, kb=None, min_items: int = 1) -> list[str]:
    """Errors for an evidence block (empty list = valid)."""
    block = block or {}
    if not isinstance(block, Mapping):
        return [f"{where}: evidence must be an object with 'manual' and/or 'literature' lists"]
    man, lit = block.get("manual") or [], block.get("literature") or []
    errs: list[str] = []
    if len(man) + len(lit) < min_items:
        errs.append(f"{where}: at least {min_items} evidence item(s) required: a Sentaurus manual chunk with a verbatim "
                    "quote, or a literature note (kb/literature/<key>.md with its DOI) with a verbatim quote")
    if man and kb is None:
        from .kb import BM25
        kb = BM25(ws.root / "kb" / "manual")
    for m in man:
        chunk, quote = m.get("chunk"), m.get("quote")
        if not chunk or not quote:
            errs.append(f"{where}: manual item needs 'chunk' and 'quote'")
            continue
        c = kb.get(chunk)
        if c is None:
            errs.append(f"{where}: unknown manual chunk {chunk}")
        elif _norm(quote)[:QUOTE_CHARS] not in _norm(c["text"]):
            errs.append(f"{where}: quote not found verbatim in manual chunk {chunk} (first {QUOTE_CHARS} characters must match)")
    for item in lit:
        key, quote = item.get("key"), item.get("quote")
        if not key or not quote:
            errs.append(f"{where}: literature item needs 'key' (note name) and 'quote'")
            continue
        note = ws.root / "kb" / "literature" / f"{key}.md"
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", str(key)) or not note.exists():
            errs.append(f"{where}: no literature note kb/literature/{key}.md")
            continue
        text = note.read_text(errors="ignore")
        if not DOI_IN_NOTE.search(text):
            errs.append(f"{where}: note {key}.md records no DOI")
        if _norm(quote)[:QUOTE_CHARS] not in _norm(text):
            errs.append(f"{where}: quote not found verbatim in note {key}.md (first {QUOTE_CHARS} characters must match)")
    return errs


def brief(block: Mapping[str, Any] | None) -> str:
    """One line for STEP_LOG.md: the cited chunks and notes."""
    block = block or {}
    refs = [f"manual {m.get('chunk')}" for m in block.get("manual") or []] + [str(l.get("key")) for l in block.get("literature") or []]
    return ", ".join(refs) or "none"
