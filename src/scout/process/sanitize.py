"""Treat every fetched string as untrusted: strip markup, drop invisible characters, truncate,
and delimit before it goes anywhere near a prompt."""

import html
import re
import unicodedata
from html.parser import HTMLParser

from scout.models import Item, content_hash

DEFAULT_MAX_BODY_CHARS = 8000

_SKIP_TAGS = {"script", "style", "noscript", "iframe", "template", "svg", "head"}
_BLOCK_TAGS = {"p", "div", "br", "li", "ul", "ol", "tr", "pre", "blockquote", "section", "article"}
_PARAGRAPH_TAGS = {"p", "pre", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6"}

# Zero-width, bidi-override and BOM characters are a common way to hide instructions.
_INVISIBLE = re.compile("[​-‏‪-‮⁠-⁤⁦-⁩﻿]")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS or tag in _PARAGRAPH_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in _PARAGRAPH_TAGS:
            self.parts.append("\n\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


def sanitize_text(text: str) -> str:
    """Remove control and invisible characters; keep newlines and tabs."""
    text = _INVISIBLE.sub("", text)
    return "".join(
        ch for ch in text if ch in "\n\t" or not unicodedata.category(ch).startswith("C")
    ).strip()


def html_to_text(markup: str) -> str:
    """Plain text from HTML: scripts and styles dropped, entities decoded, paragraphs kept."""
    parser = _TextExtractor()
    parser.feed(markup)
    parser.close()
    lines = (" ".join(line.split()) for line in "".join(parser.parts).splitlines())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def truncate(text: str, max_chars: int) -> str:
    """Deterministic cut on a word boundary, marked with an ellipsis."""
    if len(text) <= max_chars:
        return text
    head = text[: max_chars - 1]
    if " " in head or "\n" in head:
        head = re.split(r"\s", head[::-1], maxsplit=1)[1][::-1]
    return head.rstrip() + "…"


def wrap_document(text: str, *, doc_id: str, source: str) -> str:
    """Delimit untrusted text for a prompt. A literal <document> tag inside the text is
    escaped so the content cannot close the block early."""
    safe = re.sub(r"<(/?)\s*document", r"&lt;\1document", text, flags=re.IGNORECASE)
    attrs = f'id="{html.escape(doc_id)}" source="{html.escape(source)}"'
    return f"<document {attrs}>\n{safe}\n</document>"


_INJECTION_PATTERNS = {
    "ignore_previous": r"ignore\s+(?:all\s+|any\s+)?(?:the\s+)?(?:previous|prior|above)\s+"
    r"(?:instructions|prompts?|rules)",
    "disregard_above": r"disregard\s+(?:all\s+|the\s+)?(?:above|previous|prior)",
    "role_override": r"\byou\s+are\s+now\b",
    "fake_role_line": r"^\s*(?:system|assistant)\s*:",
    "reveal_prompt": r"(?:reveal|print|show|repeat)\b.{0,30}\b(?:system\s+prompt|instructions)",
    "tool_call": r"\b(?:call|invoke|execute|run)\s+the\s+\w+\s+(?:tool|function)\b",
    "new_instructions": r"\bnew\s+instructions\b",
}
_INJECTION_RE = {
    name: re.compile(pattern, re.IGNORECASE | re.MULTILINE)
    for name, pattern in _INJECTION_PATTERNS.items()
}


def injection_signals(text: str) -> list[str]:
    """Names of instruction-like patterns found in text. A signal for logs and the dashboard,
    never a gate: the real defense is delimiting plus a system prompt that says it's data."""
    return [name for name, rx in _INJECTION_RE.items() if rx.search(text)]


def sanitize_item(item: Item, *, max_body_chars: int = DEFAULT_MAX_BODY_CHARS) -> Item:
    """Return a cleaned copy of an item with its content hash recomputed. Parsers are
    responsible for turning markup into text (see `html_to_text`)."""
    title = " ".join(sanitize_text(item.title).split())
    body = truncate(sanitize_text(item.body), max_body_chars)
    return item.model_copy(
        update={"title": title, "body": body, "content_hash": content_hash(title, body)}
    )


def is_http_url(url: str) -> bool:
    """Only http(s) links are ever stored or rendered (blocks javascript:, data:, file:)."""
    return re.match(r"https?://[^\s/]+", url, re.IGNORECASE) is not None
