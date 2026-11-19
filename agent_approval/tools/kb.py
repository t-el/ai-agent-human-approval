"""Tiny knowledge-base search: markdown files split by '## ' sections, ranked with BM25. No vector DB needed
for a help center this size; swap in pgvector/Pinecone behind the same search() signature when it grows."""

from __future__ import annotations

import math
import re
from collections import Counter
from pathlib import Path

_WORD = re.compile(r"[a-z0-9]+")
_STOP = set("a an the and or to of in on for is are be can if it my our we you your i how do does with at by "
            "from this that when what will get".split())


def _tok(s: str) -> list[str]:
    return [w for w in _WORD.findall(s.lower()) if w not in _STOP]


class KnowledgeBase:
    def __init__(self, kb_dir: Path):
        self.sections: list[dict] = []
        for f in sorted(Path(kb_dir).glob("*.md")):
            title = f.stem
            text = f.read_text()
            doc_title = text.splitlines()[0].lstrip("# ").strip() if text.startswith("# ") else title
            for chunk in re.split(r"\n(?=## )", text):
                lines = chunk.strip().splitlines()
                if not lines or lines[0].startswith("# ") and len(lines) == 1:
                    continue
                head = lines[0].lstrip("# ").strip()
                body = " ".join(l for l in lines[1:] if not l.startswith("# ")).strip()
                if body:
                    sid = f"{title}#{re.sub(r'[^a-z0-9]+', '-', head.lower()).strip('-')}"
                    self.sections.append({"id": sid, "title": head, "text": body, "tokens": _tok(f"{doc_title} {head} {body}")})
        n = len(self.sections)
        df = Counter(t for s in self.sections for t in set(s["tokens"]))
        self.idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}
        self.avg = sum(len(s["tokens"]) for s in self.sections) / max(n, 1)

    def search(self, query: str, top_k: int = 2) -> dict:
        q = _tok(query)
        scored = []
        for s in self.sections:
            tf = Counter(s["tokens"])
            score = sum(self.idf.get(t, 0) * tf[t] * 2.2 / (tf[t] + 1.2 * (0.25 + 0.75 * len(s["tokens"]) / self.avg))
                        for t in q)
            if score > 0:
                scored.append((score, s))
        scored.sort(key=lambda x: -x[0])
        return {"results": [{"id": s["id"], "title": s["title"], "text": s["text"], "score": round(sc, 2)}
                            for sc, s in scored[:top_k]]}
