"""Deterministic context generation from preceding document paragraphs."""

from book_translator.document_chunking.models import ChunkContext, DocumentParagraph


def build_context(
    paragraphs: tuple[DocumentParagraph, ...], *, current_start: int, limit_words: int
) -> ChunkContext:
    if current_start <= 1 or limit_words == 0:
        return ChunkContext("", 0, None, None)
    remaining = limit_words
    parts: list[tuple[int, str]] = []
    for paragraph in reversed(paragraphs[: current_start - 1]):
        if paragraph.word_count <= remaining:
            parts.append((paragraph.sequence, paragraph.text))
            remaining -= paragraph.word_count
            if remaining == 0:
                break
            continue
        words = paragraph.text.split()
        if remaining:
            parts.append((paragraph.sequence, " ".join(words[-remaining:])))
        break
    parts.reverse()
    if not parts:
        return ChunkContext("", 0, None, None)
    text = "\n\n".join(part for _, part in parts) + "\n"
    return ChunkContext(
        text=text,
        word_count=len(text.split()),
        source_start_paragraph=parts[0][0],
        source_end_paragraph=parts[-1][0],
    )
