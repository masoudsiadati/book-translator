from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from book_translator.chapter_extraction import (
    ChapterExtractionError,
    ExtractionPlan,
    ExtractionResult,
    execute_extraction,
    plan_extraction,
)
from book_translator.config import ProjectDataError, load_book_config
from book_translator.normalization import NORMALIZATION_VERSION
from book_translator.normalization.workflow import (
    ChapterNormalizationError,
    NormalizationExecution,
    NormalizationPlan,
    execute_normalization,
    plan_normalization,
)
from book_translator.paths import ProjectPaths
from book_translator.pdf_extractor import extract_pages
from book_translator.pdf_inspector import PdfError, PdfInspectionResult, inspect_pdf
from book_translator.status import load_progress, render_status
from book_translator.toc_discovery import (
    TocDiscoveryError,
    TocDiscoveryResult,
    discover_embedded_toc,
)
from book_translator.toc_writer import write_toc

app = typer.Typer(help="Manage the local book translation pipeline.", no_args_is_help=True)
console = Console()
error_console = Console(stderr=True)


@app.callback()
def main() -> None:
    """Manage the local book translation pipeline."""


@app.command()
def status() -> None:
    """Display configuration and translation progress."""
    try:
        config = load_book_config(Path("config/book.yaml"))
        progress = load_progress(Path("state/progress.json"))
    except ProjectDataError as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    render_status(config, progress, console)


def _configured_or_explicit_pdf(pdf_path: Path | None) -> Path:
    if pdf_path is not None:
        return pdf_path
    return load_book_config(Path("config/book.yaml")).pdf_path


def _render_pdf_inspection(result: PdfInspectionResult) -> None:
    from rich.table import Table

    table = Table(title="PDF Inspection", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Resolved path", str(result.path))
    table.add_row("File size", f"{result.file_size:,} bytes")
    table.add_row("Total pages", str(result.page_count))
    table.add_row("Encrypted", "yes" if result.encrypted else "no")
    for label, value in (
        ("Title", result.metadata.title),
        ("Author", result.metadata.author),
        ("Subject", result.metadata.subject),
        ("Creator", result.metadata.creator),
    ):
        if value:
            table.add_row(label, value)
    table.add_row("Pages with text", str(result.pages_with_text))
    table.add_row("Pages without text", str(result.pages_without_text))
    table.add_row("Total words", str(result.total_word_count))
    table.add_row("Average words/page", f"{result.average_words_per_page:.2f}")
    table.add_row("Minimum words/page", str(result.minimum_words_per_page))
    table.add_row("Maximum words/page", str(result.maximum_words_per_page))
    table.add_row(
        "First page with text",
        str(result.first_page_with_text) if result.first_page_with_text else "none",
    )
    table.add_row(
        "Last page with text",
        str(result.last_page_with_text) if result.last_page_with_text else "none",
    )
    table.add_row("Classification", result.classification)
    console.print(table)


@app.command("inspect-pdf")
def inspect_pdf_command(
    pdf_path: Annotated[Path | None, typer.Argument()] = None,
) -> None:
    """Inspect whether a PDF contains extractable text."""
    try:
        _render_pdf_inspection(inspect_pdf(_configured_or_explicit_pdf(pdf_path)))
    except (ProjectDataError, PdfError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc


@app.command("extract-pages")
def extract_pages_command(
    from_page: Annotated[int, typer.Option("--from-page")],
    to_page: Annotated[int, typer.Option("--to-page")],
    output: Annotated[Path, typer.Option("--output")],
    pdf_path: Annotated[Path | None, typer.Argument()] = None,
    force: Annotated[bool, typer.Option("--force")] = False,
) -> None:
    """Extract an inclusive, 1-based PDF page range as UTF-8 text."""
    try:
        result = extract_pages(
            _configured_or_explicit_pdf(pdf_path),
            from_page=from_page,
            to_page=to_page,
            output_path=output,
            force=force,
        )
    except (ProjectDataError, PdfError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    console.print(f"[bold]Output path:[/bold] {result.output_path}")
    console.print(f"[bold]Extracted pages:[/bold] {result.page_count}")
    console.print(f"[bold]Extracted words:[/bold] {result.word_count}")
    console.print(f"[bold]Output size:[/bold] {result.byte_size:,} bytes")


def _render_toc_summary(result: TocDiscoveryResult, output: str) -> None:
    from collections import Counter

    from rich.table import Table

    counts = Counter(entry.kind.value for entry in result.document.entries)
    chapters = [entry for entry in result.document.entries if entry.kind.value == "chapter"]
    table = Table(title="TOC Discovery", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("PDF path", str(result.path))
    table.add_row("PDF page count", str(result.page_count))
    table.add_row("TOC source", "embedded")
    table.add_row("Raw entries", str(result.raw_entry_count))
    table.add_row("Normalized entries", str(len(result.document.entries)))
    table.add_row("Front matter", str(counts["front_matter"]))
    table.add_row("Prologue", str(counts["prologue"]))
    table.add_row("Chapters", str(counts["chapter"]))
    table.add_row("Appendices", str(counts["appendix"]))
    table.add_row("Unknown", str(counts["unknown"]))
    table.add_row("First chapter", str(chapters[0].number) if chapters else "none")
    table.add_row("Last chapter", str(chapters[-1].number) if chapters else "none")
    table.add_row("Output", output)
    console.print(table)


@app.command("discover-toc")
def discover_toc_command(
    pdf_path: Annotated[Path | None, typer.Argument()] = None,
    output: Annotated[Path | None, typer.Option("--output")] = None,
    force: Annotated[bool, typer.Option("--force")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Discover and normalize a PDF's embedded table of contents."""
    output_path = output if output is not None else ProjectPaths(Path.cwd()).toc
    try:
        configured_path = _configured_or_explicit_pdf(pdf_path)
        if output_path.exists() and not force and not dry_run:
            raise TocDiscoveryError(
                f"output file already exists: {output_path} (use --force to replace it)"
            )
        result = discover_embedded_toc(configured_path, source_pdf=configured_path)
        if not dry_run:
            write_toc(result.document, output_path, force=force)
    except (ProjectDataError, TocDiscoveryError, OSError, FileExistsError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    _render_toc_summary(result, "not written (dry run)" if dry_run else str(output_path))


def _render_extraction_summary(
    plan: ExtractionPlan, result: ExtractionResult | None, *, dry_run: bool
) -> None:
    from rich.table import Table

    planned = [item for item in plan.entries if item.action != "skip"]
    skipped = [item for item in plan.entries if item.action == "skip"]
    extracted_count = len(result.extracted_ids) if result is not None else 0
    failed_count = len(result.failed_ids) if result is not None else 0
    table = Table(title="Chapter Extraction", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("PDF path", str(plan.pdf_path))
    table.add_row("TOC path", str(plan.toc_path))
    table.add_row("Selected entries", str(len(plan.entries)))
    table.add_row(
        "Pages covered",
        str(sum(item.entry.end_page - item.entry.start_page + 1 for item in plan.entries)),
    )
    table.add_row("Entries planned", str(len(planned)))
    table.add_row("Entries extracted", str(extracted_count))
    table.add_row("Entries skipped", str(len(skipped)))
    table.add_row("Entries failed", str(failed_count))
    table.add_row("Total extracted words", str(result.total_words if result else 0))
    table.add_row("Total output bytes", str(result.total_bytes if result else 0))
    table.add_row("Workspace path", str(plan.workspace_path))
    table.add_row("Dry run", "yes" if dry_run else "no")
    if len(plan.entries) == 1:
        item = plan.entries[0]
        table.add_row("Canonical entry ID", item.entry.entry_id)
        table.add_row("Title", item.entry.title_en)
        table.add_row("Page range", f"{item.entry.start_page}-{item.entry.end_page}")
        table.add_row("Destination", str(item.paths.directory))
    console.print(table)


@app.command("extract-chapters")
def extract_chapters_command(
    pdf_path: Annotated[Path | None, typer.Argument()] = None,
    toc: Annotated[Path | None, typer.Option("--toc")] = None,
    chapter: Annotated[str | None, typer.Option("--chapter")] = None,
    include_front_matter: Annotated[bool, typer.Option("--include-front-matter")] = False,
    force: Annotated[bool, typer.Option("--force")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Extract immutable chapter source artifacts from the canonical TOC."""
    project_root = Path.cwd().resolve()
    toc_path = toc if toc is not None else ProjectPaths(project_root).toc
    try:
        selected_pdf = _configured_or_explicit_pdf(pdf_path)
        plan = plan_extraction(
            selected_pdf,
            toc_path,
            project_root=project_root,
            selector=chapter,
            include_front_matter=include_front_matter,
            force=force,
        )
        result = None if dry_run else execute_extraction(plan, project_root=project_root)
    except (ProjectDataError, ChapterExtractionError, OSError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    _render_extraction_summary(plan, result, dry_run=dry_run)


def _render_normalization_summary(
    plan: NormalizationPlan,
    execution: NormalizationExecution | None,
    *,
    dry_run: bool,
    show_diff_summary: bool,
) -> None:
    from rich.table import Table

    normalized = len(execution.normalized_ids) if execution else 0
    skipped = sum(item.action == "skip" for item in plan.chapters)
    table = Table(title="Chapter Normalization", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    rows = (
        ("Selected chapters", len(plan.chapters)),
        (
            "Chapters planned",
            sum(item.action in {"normalize", "replace"} for item in plan.chapters),
        ),
        ("Chapters normalized", normalized),
        ("Chapters skipped", skipped),
        ("Chapters stale", sum(item.action == "stale" for item in plan.chapters)),
        ("Chapters failed", sum(item.action == "conflict" for item in plan.chapters)),
        ("Source words", sum(item.metadata.source_word_count or 0 for item in plan.chapters)),
        ("Cleaned words", sum(item.cleaned_word_count for item in plan.chapters)),
        (
            "Source physical lines",
            sum(item.result.document.source_physical_line_count for item in plan.chapters),
        ),
        ("Wrapped line joins", sum(item.result.wrapped_line_joins for item in plan.chapters)),
        ("Hyphen repairs", sum(item.result.hyphen_repairs for item in plan.chapters)),
        ("Page markers removed", sum(item.result.page_markers_removed for item in plan.chapters)),
        ("Warnings", sum(len(item.result.warnings) for item in plan.chapters)),
        (
            "Total output bytes",
            execution.total_bytes
            if execution
            else sum(
                len(item.result.cleaned_text.encode("utf-8"))
                for item in plan.chapters
                if item.action in {"normalize", "replace"}
            ),
        ),
        ("Normalization version", plan.normalization_version),
        ("Dry run", "yes" if dry_run else "no"),
    )
    for label, value in rows:
        table.add_row(label, str(value))
    if len(plan.chapters) == 1 and not show_diff_summary:
        item = plan.chapters[0]
        table.add_row("Canonical chapter ID", item.metadata.chapter_id)
        table.add_row("Title", item.metadata.title_en)
        table.add_row("Source hash", item.source_sha256)
        table.add_row("Cleaned hash", item.cleaned_sha256)
        table.add_row("Output path", str(item.paths.cleaned))
    console.print(table)


@app.command("normalize-chapters")
def normalize_chapters_command(
    chapter: Annotated[str | None, typer.Option("--chapter")] = None,
    force: Annotated[bool, typer.Option("--force")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    show_diff_summary: Annotated[bool, typer.Option("--show-diff-summary")] = False,
    normalization_version: Annotated[
        str, typer.Option("--normalization-version", hidden=True)
    ] = NORMALIZATION_VERSION,
) -> None:
    """Create deterministic cleaned chapter artifacts without semantic rewriting."""
    try:
        plan = plan_normalization(
            project_root=Path.cwd().resolve(),
            selector=chapter,
            force=force,
            normalization_version=normalization_version,
        )
        execution = None if dry_run else execute_normalization(plan)
    except (ChapterNormalizationError, OSError, UnicodeError) as exc:
        error_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    _render_normalization_summary(
        plan, execution, dry_run=dry_run, show_diff_summary=show_diff_summary
    )
