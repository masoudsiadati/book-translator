"""Canonical Markdown paragraph parsing and rendering."""

from book_translator.document_chunking.models import DocumentParagraph

SCENE_SEPARATORS = frozenset({"***", "* * *", "# # #", "— — —"})


class DocumentParseError(ValueError):
    """Raised when cleaned Markdown is not in canonical paragraph form."""


def render_paragraph_texts(paragraphs: tuple[str, ...]) -> str:
    if not paragraphs:
        raise DocumentParseError("document contains no paragraphs")
    return "\n\n".join(paragraphs) + "\n"


def parse_document(text: str) -> tuple[DocumentParagraph, ...]:
    if not text.strip():
        raise DocumentParseError("cleaned document is empty")
    if "\r" in text:
        raise DocumentParseError("cleaned document contains CR characters")
    if not text.endswith("\n") or text.endswith("\n\n"):
        raise DocumentParseError("cleaned document must end with exactly one newline")
    body = text[:-1]
    raw_paragraphs = tuple(body.split("\n\n"))
    if any(
        not paragraph or paragraph.startswith("\n") or paragraph.endswith("\n")
        for paragraph in raw_paragraphs
    ):
        raise DocumentParseError("cleaned document has noncanonical paragraph spacing")
    if render_paragraph_texts(raw_paragraphs) != text:
        raise DocumentParseError("cleaned document is not canonical Markdown")
    return tuple(
        DocumentParagraph(
            sequence=index,
            text=paragraph,
            word_count=len(paragraph.split()),
            character_count=len(paragraph),
            byte_count=len(paragraph.encode("utf-8")),
            is_scene_separator=paragraph in SCENE_SEPARATORS,
            is_multiline="\n" in paragraph,
        )
        for index, paragraph in enumerate(raw_paragraphs, start=1)
    )


def render_paragraphs(paragraphs: tuple[DocumentParagraph, ...]) -> str:
    return render_paragraph_texts(tuple(paragraph.text for paragraph in paragraphs))
