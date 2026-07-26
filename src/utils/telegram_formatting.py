import html
import re
from typing import List


def markdown_to_plain_text(text: str) -> str:
    text = re.sub(r"```[a-zA-Z0-9_+-]*\n(.*?)```", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"`([^`\n]+)`", r"\1", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", r"\1: \2", text)
    text = re.sub(r"^\s*[-*]\s+", "• ", text, flags=re.MULTILINE)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    return html.unescape(text).strip()


def _split_plain_block(block: str, limit: int) -> list[str]:
    """Split one block at stable boundaries while enforcing a hard limit."""
    result: list[str] = []
    remaining = block
    while len(remaining) > limit:
        newline = remaining.rfind("\n", 0, limit + 1)
        space = remaining.rfind(" ", 0, limit + 1)
        boundary = max(newline, space)
        if boundary <= 0:
            boundary = limit
        piece = remaining[:boundary].rstrip()
        if not piece:
            piece = remaining[:limit]
            boundary = limit
        result.append(piece)
        remaining = remaining[boundary:].lstrip(" \n")
    if remaining:
        result.append(remaining)
    return result


def _split_markdown_block(block: str, limit: int) -> list[str]:
    """Split a paragraph/code block without emitting oversized chunks."""
    lines = block.split("\n")
    if (
        len(lines) >= 2
        and lines[0].startswith("```")
        and lines[-1].strip() == "```"
    ):
        opening = lines[0]
        body = "\n".join(lines[1:-1])
        overhead = len(opening) + len("```") + 2
        body_limit = limit - overhead
        if body_limit > 0:
            pieces = _split_plain_block(body, body_limit) or [""]
            return [f"{opening}\n{piece}\n```" for piece in pieces]
    return _split_plain_block(block, limit)


def split_markdown_for_telegram(text: str, limit: int = 3000) -> list[str]:
    """Split Markdown into non-empty chunks whose raw length never exceeds limit."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("Telegram Markdown split limit must be a positive integer")
    if not text:
        return [""]

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    blocks: list[str] = []
    current_lines: list[str] = []
    in_code_block = False
    for line in normalized.split("\n"):
        if line.startswith("```"):
            in_code_block = not in_code_block
        current_lines.append(line)
        if not in_code_block and line.strip() == "":
            block = "\n".join(current_lines).strip()
            current_lines = []
            if block:
                blocks.append(block)
    if current_lines:
        block = "\n".join(current_lines).strip()
        if block:
            blocks.append(block)

    pieces = [
        piece
        for block in blocks
        for piece in _split_markdown_block(block, limit)
        if piece
    ]
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        candidate = current + ("\n\n" if current else "") + piece
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        current = piece
    if current:
        chunks.append(current)
    return chunks or [""]


def preprocess_markdown_lines(text: str) -> str:
    lines = []

    for line in text.split("\n"):
        stripped = line.strip()

        if stripped.startswith("### "):
            line = f"**{stripped[4:]}**"
        elif stripped.startswith("## "):
            line = f"**{stripped[3:]}**"
        elif stripped.startswith("# "):
            line = f"**{stripped[2:]}**"
        elif re.match(r"^\s*[-*]\s+", line):
            line = re.sub(r"^\s*[-*]\s+", "• ", line)
        elif re.match(r"^\s*\d+\.\s+", line):
            line = stripped

        lines.append(line)

    return "\n".join(lines)


def markdown_to_telegram_html(text: str) -> str:
    """
    Преобразует базовый Markdown от LLM в Telegram HTML.

    Поддерживает:
    - **bold**
    - *italic*
    - `inline code`
    - ```code blocks```
    - [links](url)
    - # headings
    - списки через -, *, 1.
    """

    if not text:
        return ""

    text = text.replace("\r\n", "\n").replace("\r", "\n")

    placeholders: list[str] = []

    def stash(value: str) -> str:
        token = f"§§TG_PLACEHOLDER_{len(placeholders)}§§"
        placeholders.append(value)
        return token

    def replace_code_block(match: re.Match) -> str:
        language = match.group(1) or ""
        code = match.group(2) or ""

        escaped_code = html.escape(code.strip())

        if language:
            escaped_language = html.escape(language.strip())
            return stash(
                f'<pre><code class="language-{escaped_language}">'
                f"{escaped_code}</code></pre>"
            )

        return stash(f"<pre><code>{escaped_code}</code></pre>")

    text = re.sub(
        r"```([a-zA-Z0-9_+-]*)?\n(.*?)```",
        replace_code_block,
        text,
        flags=re.DOTALL,
    )

    def replace_inline_code(match: re.Match) -> str:
        code = match.group(1)
        return stash(f"<code>{html.escape(code)}</code>")

    text = re.sub(r"`([^`\n]+)`", replace_inline_code, text)

    def replace_link(match: re.Match) -> str:
        label = html.escape(match.group(1))
        url = html.escape(match.group(2), quote=True)
        return stash(f'<a href="{url}">{label}</a>')

    text = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", replace_link, text)

    text = preprocess_markdown_lines(text)
    text = html.escape(text)

    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.DOTALL)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text, flags=re.DOTALL)

    text = re.sub(
        r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)",
        r"<i>\1</i>",
        text,
        flags=re.DOTALL,
    )

    lines = []

    for line in text.split("\n"):
        stripped = line.strip()

        if stripped.startswith("### "):
            line = f"<b>{stripped[4:]}</b>"
        elif stripped.startswith("## "):
            line = f"<b>{stripped[3:]}</b>"
        elif stripped.startswith("# "):
            line = f"<b>{stripped[2:]}</b>"
        elif re.match(r"^\s*[-*]\s+", line):
            line = re.sub(r"^\s*[-*]\s+", "• ", line)
        elif re.match(r"^\s*\d+\.\s+", line):
            line = stripped

        lines.append(line)

    text = "\n".join(lines)

    for index, value in enumerate(placeholders):
        text = text.replace(f"§§TG_PLACEHOLDER_{index}§§", value)

    return text.strip()


def split_telegram_message(text: str, limit: int = 3900) -> List[str]:
    """
    Делит длинное сообщение на куски.
    3900 вместо 4096 — с запасом под HTML-теги.
    """

    if len(text) <= limit:
        return [text]

    chunks = []
    current = ""

    paragraphs = text.split("\n\n")

    for paragraph in paragraphs:
        candidate = current + ("\n\n" if current else "") + paragraph

        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
                current = ""

            if len(paragraph) <= limit:
                current = paragraph
            else:
                for i in range(0, len(paragraph), limit):
                    chunks.append(paragraph[i:i + limit])

    if current:
        chunks.append(current)

    return chunks
