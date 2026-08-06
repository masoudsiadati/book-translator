"""Conservative physical-line-boundary hyphen repair."""

from book_translator.normalization.models import ChangeRecord, ParsedDocument, PassResult

PASS_NAME = "end-of-line-hyphen-repair"
PASS_VERSION = "1.0"


def repair_hyphenation(document: ParsedDocument) -> PassResult:
    blocks = []
    changes: list[ChangeRecord] = []
    for block in document.blocks:
        lines = list(block.lines)
        repaired = []
        index = 0
        while index < len(lines):
            current = lines[index]
            while current.text.endswith("-") and index + 1 < len(lines) and lines[index + 1].text:
                continuation = lines[index + 1]
                current = type(current)(
                    current.text + continuation.text.lstrip(),
                    current.source_page,
                    current.block_index,
                    current.line_index,
                )
                changes.append(
                    ChangeRecord(
                        PASS_NAME,
                        "hyphen_repair",
                        current.source_page,
                        current.block_index,
                        current.line_index,
                        "retained ASCII hyphen while removing one physical line boundary",
                    )
                )
                index += 1
            repaired.append(current)
            index += 1
        blocks.append(block.with_lines(tuple(repaired)))
    return PassResult(
        ParsedDocument(
            tuple(blocks), document.page_transitions, document.source_physical_line_count
        ),
        tuple(changes),
    )
