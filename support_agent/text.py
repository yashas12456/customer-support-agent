"""Shared tokenisation used by the policy retriever and the product search."""

import re

STOPWORDS = frozenset(
    """a an and are as at be but by can could do does for from has have how i if in is it its
    me my of on or our please so that the their them there this to us was we what when where
    which who will with would you your any some about get want need tell show looking look
    find like just policy policies""".split()
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def stem(word: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def tokenize(text: str) -> list[str]:
    return [stem(t) for t in _TOKEN_RE.findall(text.lower()) if t not in STOPWORDS]
