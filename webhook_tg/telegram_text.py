"""Lossless splitting of plain/HTML Telegram messages into bounded parts."""

import html
from html.parser import HTMLParser

MESSAGE_LIMIT = 4096
CHUNK_LIMIT = 4000  # Reserve room for a part number outside the original HTML.


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _prefix_length(text: str, budget: int) -> int:
    used = end = 0
    for char in text:
        width = 2 if ord(char) > 0xFFFF else 1
        if used + width > budget:
            break
        used += width
        end += 1
    if end == len(text):
        return end
    # Prefer a paragraph/word boundary without losing its whitespace.
    for separator in ("\n", " "):
        boundary = text.rfind(separator, 0, end) + 1
        if boundary >= max(1, end // 2):
            return boundary
    return end


class _HTMLChunks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.current = []
        self.parts = []
        self.used = 0
        self.total = 0

    def flush(self):
        if self.used:
            closers = [f"</{tag}>" for tag, _ in reversed(self.stack)]
            self.parts.append("".join(self.current + closers))
        self.current = [opener for _, opener in self.stack]
        self.used = 0

    def handle_starttag(self, tag, attrs):
        if self.used == CHUNK_LIMIT:
            self.flush()
        opener = self.get_starttag_text()
        self.current.append(opener)
        self.stack.append((tag, opener))

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1][0] != tag:
            raise ValueError("unbalanced Telegram HTML")
        self.stack.pop()
        self.current.append(f"</{tag}>")

    def handle_startendtag(self, tag, attrs):
        raise ValueError("self-closing tags are not supported in Telegram HTML")

    def handle_comment(self, data):
        raise ValueError("comments are not supported in Telegram HTML")

    def handle_data(self, data):
        self.total += utf16_length(data)
        while data:
            count = _prefix_length(data, CHUNK_LIMIT - self.used)
            if not count:
                self.flush()
                continue
            piece, data = data[:count], data[count:]
            self.current.append(html.escape(piece, quote=False))
            self.used += utf16_length(piece)
            if data:
                self.flush()


def split_message_text(text: str, parse_mode: str | None = None) -> list[str]:
    """Keep short messages byte-for-byte; count rendered HTML, not its tags."""
    if utf16_length(text) <= MESSAGE_LIMIT:
        return [text]
    if (parse_mode or "").upper() == "HTML":
        parser = _HTMLChunks()
        parser.feed(text)
        parser.close()
        if parser.stack:
            raise ValueError("unclosed Telegram HTML tag")
        if parser.total <= MESSAGE_LIMIT:
            return [text]
        parser.flush()
        return parser.parts
    if parse_mode:
        # Other parse modes need their own entity-aware splitting rules.
        return [text]
    parts = []
    while text:
        count = _prefix_length(text, CHUNK_LIMIT)
        parts.append(text[:count])
        text = text[count:]
    return parts
