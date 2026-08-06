"""Render logical source blocks as canonical Markdown paragraphs."""

from book_translator.normalization.models import ParsedDocument

PASS_NAME = "paragraph-rendering"
PASS_VERSION = "1.0"


def render_paragraphs(document: ParsedDocument) -> str:
    paragraphs = []
    for block in document.blocks:
        separator = "\n" if block.preserve_lines else ""
        paragraph = separator.join(line.text for line in block.lines).strip("\n")
        if paragraph:
            paragraphs.append(paragraph)
    return "\n\n".join(paragraphs) + "\n"
