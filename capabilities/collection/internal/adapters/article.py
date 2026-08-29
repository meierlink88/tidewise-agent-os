"""Bounded, dependency-free readable text extraction for RSS article pages."""

from html.parser import HTMLParser

_IGNORED_TAGS = {"aside", "footer", "form", "header", "nav", "noscript", "script", "style", "svg"}
_CONTENT_TAGS = {"article", "main"}
_TEXT_BLOCK_TAGS = {"blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "li", "p"}


class _ArticleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self._content_depth = 0
        self._block_stack: list[tuple[str, bool, list[str]]] = []
        self.content_blocks: list[str] = []
        self.fallback_blocks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized = tag.lower()
        if normalized in _IGNORED_TAGS:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if normalized in _CONTENT_TAGS:
            self._content_depth += 1
        if normalized in _TEXT_BLOCK_TAGS:
            self._block_stack.append((normalized, self._content_depth > 0, []))

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if normalized in _IGNORED_TAGS:
            if self._ignored_depth:
                self._ignored_depth -= 1
            return
        if self._ignored_depth:
            return
        if normalized in _TEXT_BLOCK_TAGS:
            self._finish_block(normalized)
        if normalized in _CONTENT_TAGS and self._content_depth:
            self._content_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._ignored_depth or not self._block_stack:
            return
        self._block_stack[-1][2].append(data)

    def _finish_block(self, tag: str) -> None:
        for index in range(len(self._block_stack) - 1, -1, -1):
            block_tag, preferred, parts = self._block_stack[index]
            if block_tag != tag:
                continue
            del self._block_stack[index]
            text = " ".join("".join(parts).split())
            if not text:
                return
            self.fallback_blocks.append(text)
            if preferred:
                self.content_blocks.append(text)
            return


def extract_readable_article_text(payload: str) -> str:
    """Prefer semantic article/main blocks and remove common navigation chrome."""
    parser = _ArticleTextParser()
    parser.feed(payload)
    parser.close()
    blocks = parser.content_blocks or parser.fallback_blocks
    unique: list[str] = []
    for block in blocks:
        if not unique or unique[-1] != block:
            unique.append(block)
    return "\n\n".join(unique).strip()
