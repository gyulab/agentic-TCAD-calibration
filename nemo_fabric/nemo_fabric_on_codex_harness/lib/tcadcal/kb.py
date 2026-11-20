"""Knowledge base over the Sentaurus Device User Guide (W-2024.09-SP1): page chunks + BM25.
"""
from __future__ import annotations

import json
import math
import re
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

from . import config as C

TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_]*|\d+(?:\.\d+)?")
STOP = set("""a an and are as at be by for from has have in is it its of on or that the this to was were will with
which can not no if then than these those into only also such using used use may each per""".split())


def tokenize(text: str) -> list[str]:
    out = []
    for t in TOKEN.findall(text):
        t = t.lower()
        if t in STOP or len(t) < 2:
            continue
        out.append(t)
    return out


def build(pdf: Path, out_dir: Path) -> dict[str, Any]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    txt = subprocess.run(["pdftotext", "-layout", str(pdf), "-"], capture_output=True, text=True, check=True).stdout
    pages = txt.split("\f")
    chunks = []
    for i, page in enumerate(pages, start=1):
        lines = [l.rstrip() for l in page.splitlines()]
        body = [l for l in lines if l.strip()]
        if len(body) < 3:
            continue
        chapter = body[0].strip() if body[0].strip().startswith("Chapter") else ""
        section = body[1].strip() if chapter and len(body) > 1 else ""
        text = "\n".join(lines)
        text = re.sub(r"Sentaurus™ Device User Guide\s+\d+\s*\n\s*W-2024\.09\s*$", "", text.strip())
        chunks.append({"id": f"p{i:04d}", "pdf_page": i, "chapter": chapter, "section": section, "text": text.strip()})
    with open(out_dir / "chunks.jsonl", "w") as fh:
        for c in chunks:
            fh.write(json.dumps(c) + "\n")
    meta = {"source": str(pdf), "version": C.SENTAURUS_VERSION, "n_chunks": len(chunks), "chunking": "one PDF page per chunk",
            "ranker": "BM25 (k1=1.5, b=0.75), title fields weighted x3"}
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    return meta


class BM25:
    def __init__(self, kb_dir: Path, k1: float = 1.5, b: float = 0.75):
        self.chunks = [json.loads(s) for s in (Path(kb_dir) / "chunks.jsonl").read_text().splitlines() if s.strip()]
        self.k1, self.b = k1, b
        self.docs = []
        for c in self.chunks:
            toks = tokenize(c["text"]) + 3 * tokenize(c["chapter"] + " " + c["section"])
            self.docs.append(Counter(toks))
        self.lens = [sum(d.values()) for d in self.docs]
        self.avg = sum(self.lens) / len(self.lens)
        df = Counter()
        for d in self.docs:
            df.update(d.keys())
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def search(self, query: str, k: int = 8) -> list[dict[str, Any]]:
        q = tokenize(query)
        scores = []
        for i, d in enumerate(self.docs):
            s = 0.0
            for t in q:
                f = d.get(t)
                if not f:
                    continue
                s += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * self.lens[i] / self.avg))
            if s > 0:
                scores.append((s, i))
        scores.sort(reverse=True)
        out = []
        for s, i in scores[:k]:
            c = self.chunks[i]
            out.append({"id": c["id"], "pdf_page": c["pdf_page"], "chapter": c["chapter"], "section": c["section"],
                        "score": round(s, 3), "snippet": snippet(c["text"], q)})
        return out

    def get(self, chunk_id: str) -> dict[str, Any] | None:
        return next((c for c in self.chunks if c["id"] == chunk_id), None)


def snippet(text: str, q: list[str], width: int = 420) -> str:
    low = text.lower()
    pos = [low.find(t) for t in q if low.find(t) >= 0]
    start = max(0, min(pos) - 120) if pos else 0
    s = re.sub(r"\s+", " ", text[start:start + width])
    return s.strip()
