"""Deterministic Release Assembly Engine public API.

Release assembly is the boundary between a finished translation run and a distributable book. It
consumes only immutable accepted translation outputs plus the frozen snapshot they answer, it
contacts no provider, and it mutates no run, snapshot, or registry. The canonical Markdown book
it publishes is the input a later format builder — EPUB, print, anything else — reads instead of
walking the run again.
"""

from book_translator.release_assembly.engine import (
    RELEASE_ASSEMBLY_ENGINE_VERSION,
    ReleaseAssemblyError,
    assemble_book,
    assemble_chapter,
    chapter_filename,
    chapter_heading,
    chunk_paragraphs,
    validate_chunk_coverage,
    validate_release_id,
    word_count,
)
from book_translator.release_assembly.identity import release_content_address
from book_translator.release_assembly.models import (
    ReleaseAssemblyPlan,
    ReleaseAssemblyResult,
    ReleaseFile,
)
from book_translator.release_assembly.reporting import (
    BOOK_FILENAME,
    CHAPTERS_DIRECTORY,
    LINEAGE_FILENAME,
    MANIFEST_FILENAME,
    METADATA_FILENAME,
    TERMINOLOGY_FILENAME,
    TRANSLATION_REPORT_FILENAME,
    serialize_release_json,
    serialize_release_yaml,
)
from book_translator.release_assembly.workflow import (
    EffectiveAcceptance,
    execute_release_assembly,
    load_effective_acceptance,
    plan_release_assembly,
    verify_release,
)

__all__ = [
    "BOOK_FILENAME",
    "CHAPTERS_DIRECTORY",
    "LINEAGE_FILENAME",
    "MANIFEST_FILENAME",
    "METADATA_FILENAME",
    "RELEASE_ASSEMBLY_ENGINE_VERSION",
    "TERMINOLOGY_FILENAME",
    "TRANSLATION_REPORT_FILENAME",
    "EffectiveAcceptance",
    "ReleaseAssemblyError",
    "ReleaseAssemblyPlan",
    "ReleaseAssemblyResult",
    "ReleaseFile",
    "assemble_book",
    "assemble_chapter",
    "chapter_filename",
    "chapter_heading",
    "chunk_paragraphs",
    "execute_release_assembly",
    "load_effective_acceptance",
    "plan_release_assembly",
    "release_content_address",
    "serialize_release_json",
    "serialize_release_yaml",
    "validate_chunk_coverage",
    "validate_release_id",
    "word_count",
    "verify_release",
]
