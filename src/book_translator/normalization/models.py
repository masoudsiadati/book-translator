"""Immutable normalization structures and structured audit records."""

from dataclasses import dataclass, replace


@dataclass(frozen=True, slots=True)
class PhysicalLine:
    text: str
    source_page: int
    block_index: int
    line_index: int


@dataclass(frozen=True, slots=True)
class TextBlock:
    source_page: int
    block_index: int
    lines: tuple[PhysicalLine, ...]
    original_text: str
    preserve_lines: bool = False

    def with_lines(
        self, lines: tuple[PhysicalLine, ...], *, preserve_lines: bool | None = None
    ) -> "TextBlock":
        return replace(
            self,
            lines=lines,
            preserve_lines=self.preserve_lines if preserve_lines is None else preserve_lines,
        )


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    blocks: tuple[TextBlock, ...]
    page_transitions: tuple[int, ...]
    source_physical_line_count: int


@dataclass(frozen=True, slots=True)
class ChangeRecord:
    pass_name: str
    change_type: str
    source_page: int | None = None
    block_index: int | None = None
    line_index: int | None = None
    description: str = ""


@dataclass(frozen=True, slots=True)
class PassResult:
    document: ParsedDocument
    changes: tuple[ChangeRecord, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class NormalizationResult:
    cleaned_text: str
    document: ParsedDocument
    changes: tuple[ChangeRecord, ...]
    warnings: tuple[str, ...]
    passes_run: tuple[str, ...]

    @property
    def wrapped_line_joins(self) -> int:
        return sum(change.change_type == "wrapped_line_join" for change in self.changes)

    @property
    def hyphen_repairs(self) -> int:
        return sum(change.change_type == "hyphen_repair" for change in self.changes)

    @property
    def page_markers_removed(self) -> int:
        return sum(change.change_type == "page_marker_removed" for change in self.changes)

    @property
    def page_continuation_joins(self) -> int:
        return sum(
            change.change_type == "page_boundary_continuation_join" for change in self.changes
        )

    @property
    def ambiguous_blocks(self) -> int:
        return sum(change.change_type == "ambiguous_block" for change in self.changes)
