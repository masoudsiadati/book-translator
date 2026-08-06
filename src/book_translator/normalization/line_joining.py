"""Conservative wrapped-line joining within extracted source blocks only."""

from book_translator.normalization.models import ChangeRecord, ParsedDocument, PassResult

PASS_NAME = "wrapped-line-joining"
PASS_VERSION = "1.0"


def _looks_like_verse(lengths: list[int]) -> bool:
    return len(lengths) >= 3 and sum(length <= 40 for length in lengths) / len(lengths) >= 0.75


def join_wrapped_lines(document: ParsedDocument) -> PassResult:
    blocks = []
    changes: list[ChangeRecord] = []
    warnings: list[str] = []
    for block in document.blocks:
        lengths = [len(line.text.strip()) for line in block.lines]
        preserve = _looks_like_verse(lengths)
        ambiguous = len(lengths) >= 2 and not preserve and sum(x <= 40 for x in lengths) >= 2
        if ambiguous:
            warning = (
                f"ambiguous short-line block {block.block_index} on source page {block.source_page}"
            )
            warnings.append(warning)
            changes.append(
                ChangeRecord(
                    PASS_NAME,
                    "ambiguous_block",
                    block.source_page,
                    block.block_index,
                    description="short-line structure was joined conservatively",
                )
            )
        if preserve or len(block.lines) <= 1:
            blocks.append(block.with_lines(block.lines, preserve_lines=preserve))
            continue
        first = block.lines[0]
        text = first.text.rstrip()
        for following in block.lines[1:]:
            text = f"{text} {following.text.lstrip()}" if text else following.text.lstrip()
            changes.append(
                ChangeRecord(
                    PASS_NAME,
                    "wrapped_line_join",
                    following.source_page,
                    block.block_index,
                    following.line_index,
                    "replaced one physical line boundary with one space",
                )
            )
        blocks.append(
            block.with_lines(
                (type(first)(text, first.source_page, first.block_index, first.line_index),)
            )
        )
    return PassResult(
        ParsedDocument(
            tuple(blocks), document.page_transitions, document.source_physical_line_count
        ),
        tuple(changes),
        tuple(warnings),
    )
