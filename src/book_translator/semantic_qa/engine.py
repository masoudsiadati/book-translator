"""Pure construction of semantic coverage judge tasks from an aligned chunk.

A task is one paragraph identity plus exactly the evidence needed to judge it: the English source
paragraph, the Persian candidate, the bounded neighbours on both sides, and only the canonical
terminology decisions that paragraph's own source text actually triggers. Everything about that
list is a deliberate exclusion. The whole registry would drown the question in irrelevant
constraints; another chapter's text would let a judge reason about material the translator never
saw; the translator's own reasoning would turn an independent check into a review of itself.

Identity is never inferred here. The caller supplies aligned pairs, and this module refuses a
chunk whose two sides do not correspond one to one rather than guessing an alignment, for the same
reason the marker contract exists.
"""

from dataclasses import dataclass

from book_translator.models import TerminologyRegistry
from book_translator.paragraph_markers import source_paragraphs
from book_translator.terminology.exemptions import paragraph_exemptions, paragraph_offsets
from book_translator.terminology.registry import ExemptOccurrences, applicable_decisions

#: How many neighbouring paragraphs on each side a first-look judge may see. One is enough to
#: resolve a pronoun, an unattributed line of dialogue, or a sentence that continues a thought,
#: and more would start to let the judge attribute a neighbour's content to the paragraph under
#: test. A second look widens this deliberately and once; see ``SECOND_LOOK_CONTEXT_RADIUS``.
CONTEXT_RADIUS = 1

#: The bounded wider window an INCONCLUSIVE paragraph is granted exactly once. Two paragraphs on
#: each side resolves a speaker introduced before the previous paragraph or a reference whose
#: antecedent sits one exchange back, which is where a first look most often runs out of
#: evidence; it is still far short of a chunk, so ownership of the judged paragraph stays clear.
SECOND_LOOK_CONTEXT_RADIUS = 2

#: The second-look context policy: what a re-ask may see and how often it may happen.
SECOND_LOOK_CONTEXT_POLICY_VERSION = "1.0.0"
MAX_SECOND_LOOKS_PER_PARAGRAPH = 1

__all__ = [
    "CONTEXT_RADIUS",
    "MAX_SECOND_LOOKS_PER_PARAGRAPH",
    "SECOND_LOOK_CONTEXT_POLICY_VERSION",
    "SECOND_LOOK_CONTEXT_RADIUS",
    "paragraph_identity",
    "JudgeBatch",
    "ParagraphTask",
    "SemanticTaskError",
    "aligned_paragraphs",
    "build_batches",
    "build_tasks",
]


class SemanticTaskError(ValueError):
    """Raised when a chunk cannot be turned into judgeable tasks."""


def paragraph_identity(index: int) -> str:
    """Return the judge-facing identity of the 1-based paragraph *index*.

    It is the same identity the numbered paragraph marker ``[[P0001]]`` names, written without
    the bracket syntax.  The brackets exist to survive a provider rewriting a translation and
    have no job in a judge prompt, where carrying them would only invite a model to echo marker
    syntax back into text that must never contain it.
    """
    if index < 1 or index > 9999:
        raise SemanticTaskError(f"paragraph index outside the judge contract: {index}")
    return f"P{index:04d}"


@dataclass(frozen=True, slots=True)
class TerminologyConstraint:
    """One canonical decision the paragraph's own source text triggers."""

    source_term: str
    target_term: str
    required: bool
    #: Further realizations the decision explicitly authorized; the judge must not read one as a
    #: terminology deviation. Empty for every decision that predates authorized forms.
    allowed_target_forms: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ParagraphTask:
    """One paragraph identity and everything the judge is allowed to see about it."""

    paragraph_id: str
    paragraph_index: int
    source_text: str
    target_text: str
    previous_source: str | None
    previous_target: str | None
    next_source: str | None
    next_target: str | None
    terminology: tuple[TerminologyConstraint, ...]

    @property
    def source_word_count(self) -> int:
        return len(self.source_text.split())

    @property
    def target_word_count(self) -> int:
        return len(self.target_text.split())


@dataclass(frozen=True, slots=True)
class JudgeBatch:
    """The paragraphs answered by one provider call."""

    tasks: tuple[ParagraphTask, ...]

    @property
    def paragraph_ids(self) -> tuple[str, ...]:
        return tuple(task.paragraph_id for task in self.tasks)


def aligned_paragraphs(source_text: str, translation_text: str) -> tuple[tuple[str, str], ...]:
    """Return the aligned source/target block pairs, or refuse when there is no alignment."""
    source = source_paragraphs(source_text)
    target = source_paragraphs(translation_text)
    if not source:
        raise SemanticTaskError("source carries no paragraphs")
    if len(source) != len(target):
        raise SemanticTaskError(
            f"source carries {len(source)} paragraphs and the translation {len(target)},"
            " so no paragraph identity can be established"
        )
    return tuple(zip(source, target, strict=True))


def _constraints(
    registry: TerminologyRegistry | None,
    source_text: str,
    exemptions: ExemptOccurrences | None = None,
) -> tuple[TerminologyConstraint, ...]:
    if registry is None:
        return ()
    return tuple(
        TerminologyConstraint(
            source_term=candidate.source_term,
            target_term=decision.target_term,
            required=decision.required,
            allowed_target_forms=tuple(decision.allowed_target_forms),
        )
        # The paragraph is passed as both source and context because ``applicable_decisions``
        # selects on either, and here the paragraph is the whole world: a decision triggered only
        # by a neighbour is not a constraint on this paragraph's rendering.
        for candidate, decision in applicable_decisions(registry, source_text, "", exemptions)
    )


def _joined(pairs: tuple[tuple[str, str], ...], side: int) -> str | None:
    """Join several context paragraphs into one block, or ``None`` when there are none.

    Joining with a blank line keeps the rendered request byte-identical for a radius of one, so
    widening the window for a second look changes what a judge sees and nothing else.
    """
    if not pairs:
        return None
    return "\n\n".join(pair[side] for pair in pairs)


def build_tasks(
    source_text: str,
    translation_text: str,
    *,
    registry: TerminologyRegistry | None = None,
    paragraph_indexes: tuple[int, ...] | None = None,
    context_radius: int = CONTEXT_RADIUS,
    exemptions: ExemptOccurrences | None = None,
) -> tuple[ParagraphTask, ...]:
    """Build one task per selected paragraph of an aligned chunk.

    ``paragraph_indexes`` selects 1-based paragraphs; ``None`` selects all of them. A selected
    paragraph still receives its real neighbours, because narrowing what is *judged* must not
    narrow what the judge can see when resolving a pronoun. ``context_radius`` is how many
    neighbours on each side those are; the judged identity never widens with it.
    ``exemptions`` are the chunk's proven occurrence exemptions, rebased onto each paragraph.
    """
    if context_radius < 0:
        raise SemanticTaskError("context radius must not be negative")
    pairs = aligned_paragraphs(source_text, translation_text)
    selected = (
        tuple(range(1, len(pairs) + 1)) if paragraph_indexes is None else tuple(paragraph_indexes)
    )
    if any(index < 1 or index > len(pairs) for index in selected):
        raise SemanticTaskError("paragraph selection falls outside the chunk")
    offsets = paragraph_offsets(source_text) if exemptions is not None else ()
    tasks: list[ParagraphTask] = []
    for index in selected:
        source, target = pairs[index - 1]
        paragraph_exempt = (
            paragraph_exemptions(
                exemptions,
                source_text=source_text,
                block_start=offsets[index - 1][0],
                block=source,
            )
            if exemptions is not None
            else None
        )
        before = pairs[max(0, index - 1 - context_radius) : index - 1]
        after = pairs[index : index + context_radius]
        tasks.append(
            ParagraphTask(
                paragraph_id=paragraph_identity(index),
                paragraph_index=index,
                source_text=source,
                target_text=target,
                previous_source=_joined(before, 0),
                previous_target=_joined(before, 1),
                next_source=_joined(after, 0),
                next_target=_joined(after, 1),
                terminology=_constraints(registry, source, paragraph_exempt),
            )
        )
    return tuple(tasks)


def build_batches(tasks: tuple[ParagraphTask, ...], *, batch_size: int) -> tuple[JudgeBatch, ...]:
    """Group tasks into provider calls of at most *batch_size* paragraphs, in source order."""
    if batch_size < 1:
        raise SemanticTaskError("batch size must be at least one")
    ordered = sorted(tasks, key=lambda task: task.paragraph_index)
    return tuple(
        JudgeBatch(tuple(ordered[start : start + batch_size]))
        for start in range(0, len(ordered), batch_size)
    )
