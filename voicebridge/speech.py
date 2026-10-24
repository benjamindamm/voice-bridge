"""Sprech-Normalisierer: macht aus Markdown-Text eine hörbare Fassung.

Kein LLM-Aufruf – deterministisch und schnell:

- Code-Blöcke, Backticks, Links, URLs, Tabellen, Überschriften, Aufzählungen
  und Hervorhebungen werden entfernt bzw. ersetzt
- Absätze werden zu zusammenhängenden Sätzen verbunden
- lange Antworten werden an einer Satzgrenze gekürzt, mit Hinweis
"""

from __future__ import annotations

import re

FENCE = re.compile(r"```.*?```", re.S)
IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
URL = re.compile(r"https?://\S+")
HTML = re.compile(r"<[^>]+>")
INLINE_CODE = re.compile(r"`([^`]*)`")
TABLE_SEP = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$", re.M)
HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+", re.M)
QUOTE = re.compile(r"^\s*>\s?", re.M)
EMPHASIS = re.compile(r"(\*\*|__|\*|_|~~)")


def to_speech(
    text: str,
    *,
    max_chars: int = 350,
    hint: str = "Details stehen im Terminal.",
) -> str:
    """Liefert eine sprechbare Fassung von ``text``."""
    cleaned = text.replace("\r\n", "\n")
    cleaned = FENCE.sub(" ", cleaned)
    cleaned = IMAGE.sub(" ", cleaned)
    cleaned = LINK.sub(r"\1", cleaned)
    cleaned = URL.sub("Link im Terminal", cleaned)
    cleaned = HTML.sub(" ", cleaned)
    cleaned = INLINE_CODE.sub(r"\1", cleaned)
    cleaned = TABLE_SEP.sub("", cleaned)
    cleaned = cleaned.replace("|", ", ")
    cleaned = HEADING.sub("", cleaned)
    cleaned = BULLET.sub("", cleaned)
    cleaned = QUOTE.sub("", cleaned)
    cleaned = EMPHASIS.sub("", cleaned)

    paragraphs = []
    for block in cleaned.split("\n\n"):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        merged = lines[0]
        for line in lines[1:]:
            separator = " " if merged.endswith((".", "!", "?", ":")) else ". "
            merged += separator + line
        paragraphs.append(merged)

    spoken = ""
    for paragraph in paragraphs:
        if not spoken:
            spoken = paragraph
        elif spoken.endswith((".", "!", "?", ":")):
            spoken += " " + paragraph
        else:
            spoken += ". " + paragraph

    spoken = re.sub(r"\s+", " ", spoken).strip()
    spoken = re.sub(r"([.!?])\s*\1+", r"\1", spoken)
    spoken = re.sub(r"\s+([,.;:!?])", r"\1", spoken)

    if len(spoken) <= max_chars:
        return spoken

    window = spoken[:max_chars]
    cut = max(window.rfind(". "), window.rfind("! "), window.rfind("? "))
    if cut < max_chars * 0.4:
        cut = window.rfind(" ")
    if cut <= 0:
        cut = max_chars
    else:
        cut += 1

    shortened = spoken[:cut].rstrip(" ,;:-")
    if shortened and shortened[-1] not in ".!?":
        shortened += "."
    return (shortened + " " + hint).strip()
