"""Versioned, language-pair-scoped thresholds for deterministic paragraph alignment QA.

Two decisions live here, and both are policy rather than fact.

The first is where a length ratio stops being literary variation and starts being evidence.  The
band this project ships was measured, not assumed: over the completed English-to-Persian pilot of
5,068 aligned pairs, requiring at least 25 source words and flagging a target/source word ratio
below 0.60 or above 1.60 selected exactly the three chunks an independent read-only audit had
confirmed to contain paragraph-boundary drift, and nothing else.  That is one book, one language
pair, one provider, so the numbers are attached to a named policy with its own version instead of
being written into the engine.  A second language pair gets its own policy and its own evidence.

The second is what a finding is allowed to *do*.  A lone out-of-band ratio is a warning: literary
Persian legitimately compresses an English sentence or expands an idiom, the band's edge is
genuinely close to ordinary translation (the pilot's nearest non-defective pair sits at 1.605
under a different word-counting rule), and turning a single-signal heuristic into a pipeline stop
would spend operator attention on prose that is fine.  A *complementary adjacent pair* — one
paragraph far below the band, its neighbour far above it, and the two together back inside the
band — is a different claim.  Content that leaves one paragraph and arrives in the next conserves
the total, and that conservation is the signature; ordinary translation variation does not produce
it.  That finding blocks acceptance, because the numbered paragraph contract cannot see it: a
provider can honour every marker and still put the wrong sentence under one.
"""

from dataclasses import dataclass

#: Version of the alignment QA engine: the measurements taken, the finding codes emitted, and the
#: rules that derive them.  Independent of any individual policy's thresholds.
ALIGNMENT_QA_VERSION = "1.0.0"

ADJACENT_BOUNDARY_DRIFT_SUSPECTED = "adjacent_boundary_drift_suspected"
PARAGRAPH_WORD_RATIO_OUT_OF_BAND = "paragraph_word_ratio_out_of_band"
NUMERAL_NOT_PRESERVED = "numeral_not_preserved"
DIALOGUE_MARKS_ABSENT = "dialogue_marks_absent"
ALIGNMENT_NOT_EVALUABLE = "alignment_not_evaluable"

__all__ = [
    "ADJACENT_BOUNDARY_DRIFT_SUSPECTED",
    "ALIGNMENT_NOT_EVALUABLE",
    "ALIGNMENT_QA_VERSION",
    "DIALOGUE_MARKS_ABSENT",
    "EN_FA_V1",
    "GENERIC_V1",
    "NUMERAL_NOT_PRESERVED",
    "PARAGRAPH_WORD_RATIO_OUT_OF_BAND",
    "AlignmentPolicy",
    "policy_for",
]


@dataclass(frozen=True, slots=True)
class AlignmentPolicy:
    """One language pair's thresholds and severities, versioned as a unit."""

    policy_id: str
    policy_version: str
    #: Below this source length a ratio says more about rounding than about content, so short
    #: paragraphs are measured and reported but never flagged.
    minimum_source_words: int
    low_word_ratio: float
    high_word_ratio: float
    ratio_severity: str
    drift_severity: str
    numeral_severity: str
    dialogue_severity: str

    def measurable(self, source_words: int) -> bool:
        return source_words >= self.minimum_source_words

    def in_band(self, ratio: float) -> bool:
        return self.low_word_ratio <= ratio <= self.high_word_ratio

    def severity_for(self, code: str) -> str:
        return {
            PARAGRAPH_WORD_RATIO_OUT_OF_BAND: self.ratio_severity,
            ADJACENT_BOUNDARY_DRIFT_SUSPECTED: self.drift_severity,
            NUMERAL_NOT_PRESERVED: self.numeral_severity,
            DIALOGUE_MARKS_ABSENT: self.dialogue_severity,
            ALIGNMENT_NOT_EVALUABLE: "warning",
        }.get(code, "error")


#: The English-to-Persian policy, carrying the pilot-measured band.
EN_FA_V1 = AlignmentPolicy(
    policy_id="en-fa",
    policy_version="1.0.0",
    minimum_source_words=25,
    low_word_ratio=0.60,
    high_word_ratio=1.60,
    ratio_severity="warning",
    drift_severity="error",
    numeral_severity="warning",
    dialogue_severity="warning",
)

#: The fallback for a target language nobody has measured yet.  It carries the same numbers,
#: because they are the only measured numbers that exist, but it says so in its own identifier so
#: a report can never claim English-to-Persian evidence for a pair that has none.
GENERIC_V1 = AlignmentPolicy(
    policy_id="unmeasured-pair",
    policy_version="1.0.0",
    minimum_source_words=25,
    low_word_ratio=0.60,
    high_word_ratio=1.60,
    ratio_severity="warning",
    drift_severity="error",
    numeral_severity="warning",
    dialogue_severity="warning",
)

_POLICIES: dict[str, AlignmentPolicy] = {"fa": EN_FA_V1}


def policy_for(target_language: str) -> AlignmentPolicy:
    """Return the alignment policy governing a target language."""
    key = target_language.lower().split("-")[0]
    return _POLICIES.get(key, GENERIC_V1)
