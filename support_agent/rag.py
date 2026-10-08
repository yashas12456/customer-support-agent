"""Policy retrieval (the RAG half of the agent).

Markdown policies are chunked by `##` heading and indexed with BM25. It is
dependency-free so the project runs anywhere; to upgrade, replace `PolicyIndex.search`
with an embedding model + vector DB (the interface stays identical).
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .config import DATA_DIR
from .text import tokenize


@dataclass
class Chunk:
    source: str       # e.g. "Returns and Exchanges"
    heading: str      # e.g. "Return window"
    text: str
    tokens: list[str]


class PolicyIndex:
    K1 = 1.5
    B = 0.75
    MIN_SCORE = 1.0   # below this we say "I don't know" instead of guessing

    def __init__(self, policy_dir: Path | None = None):
        policy_dir = policy_dir or DATA_DIR / "policies"
        self.chunks: list[Chunk] = []
        for path in sorted(policy_dir.glob("*.md")):
            self.chunks.extend(self._chunk(path.read_text()))
        self._df: Counter = Counter()
        for c in self.chunks:
            self._df.update(set(c.tokens))
        self._avg_len = sum(len(c.tokens) for c in self.chunks) / max(len(self.chunks), 1)

    @staticmethod
    def _chunk(markdown: str) -> list[Chunk]:
        title_match = re.search(r"^# (.+)$", markdown, re.M)
        title = title_match.group(1).strip() if title_match else "Policy"
        chunks = []
        for block in re.split(r"^## ", markdown, flags=re.M)[1:]:
            heading, _, body = block.partition("\n")
            body = body.strip()
            # heading + title are indexed too, so "return window" finds the right section
            tokens = tokenize(f"{title} {heading} {heading} {body}")
            chunks.append(Chunk(title, heading.strip(), body, tokens))
        return chunks

    def _score(self, query_tokens: list[str], chunk: Chunk) -> float:
        n = len(self.chunks)
        tf = Counter(chunk.tokens)
        score = 0.0
        for term in set(query_tokens):
            if term not in tf:
                continue
            idf = math.log(1 + (n - self._df[term] + 0.5) / (self._df[term] + 0.5))
            freq = tf[term]
            denom = freq + self.K1 * (1 - self.B + self.B * len(chunk.tokens) / self._avg_len)
            score += idf * freq * (self.K1 + 1) / denom
        return score

    def search(self, query: str, k: int = 3) -> list[dict]:
        q = tokenize(query)
        if not q:
            return []
        scored = sorted(
            ((self._score(q, c), c) for c in self.chunks), key=lambda x: x[0], reverse=True
        )
        # A lone matching word (e.g. "price" in "do you price match competitors?") is not evidence
        # of an answer: a chunk must contain at least half of the distinct query terms.
        need = math.ceil(len(set(q)) / 2)
        scored = [(s, c) for s, c in scored if len(set(q) & set(c.tokens)) >= need]
        return [
            {
                "source": c.source,
                "section": c.heading,
                "text": c.text,
                "score": round(s, 2),
            }
            for s, c in scored[:k]
            if s >= self.MIN_SCORE
        ]
