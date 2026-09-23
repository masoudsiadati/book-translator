"""Explicitly authorized target forms reach every provider-facing rendering and every matcher.

Terminology QA 1.8.0 made an authorized form count as the canonical. Everything that *tells* a
provider about a decision — the translation prompt's series tiers and registry block, the repair
request, the semantic judge and reviser, the chapter-title prompt, and the literary editor — and
everything that *counts* a canonical outside the validator must say the same thing, or a
translator is asked for one form while QA accepts three.

Two properties carry the weight. A decision with forms renders them beside its preferred target
with one shared rule, and never renders a competing or observed variant as one. A decision
without forms renders byte-identically to before, which is what keeps every frozen package,
request digest, and cross-run proof intact. All terms and Persian strings are synthetic.
"""

import json
from datetime import timedelta
from pathlib import Path

from book_translator.chapter_titles.rendering import render_title_terminology
from book_translator.final_qa.analysis import analyze_competing_variants
from book_translator.literary_editing.engine import terminology_differences, terminology_json
from book_translator.models import (
    CompetingVariantClass,
    DecisionSource,
    SeriesProvenanceKind,
    SeriesTermStatus,
    TerminologyType,
)
from book_translator.prompt_packaging.rendering import render_terminology
from book_translator.semantic_qa.engine import JudgeBatch, build_tasks
from book_translator.semantic_qa.rendering import render_user_message
from book_translator.semantic_revision.rendering import render_revision_request
from book_translator.series_terminology.context import (
    build_effective_context,
    render_effective_context,
)
from book_translator.terminology import set_decision
from book_translator.terminology.registry import AUTHORIZED_FORMS_RULE
from book_translator.translation_queue import load_run_summary
from book_translator.translation_repair import build_repair_request
from book_translator.translation_worker import run_translation_worker
from tests.test_terminology_target_forms import (
    PREFERRED,
    SOURCE,
    SOURCE_TERM,
    SPACED,
    VERBAL,
    _narrative,
    _policy,
    _registry,
    _term,
)
from tests.test_translation_queue import NOW
from tests.test_translation_reuse import _rendered_run, _write_registry
from tests.test_translation_snapshot import synthetic_project
from tests.test_translation_worker import SyntheticProvider, _options

RULE_LINE = f"    form_rule: {json.dumps(AUTHORIZED_FORMS_RULE, ensure_ascii=False)}"


def _forms_line(*forms: str) -> str:
    return f"    allowed_target_forms: {json.dumps(list(forms), ensure_ascii=False)}"


def _without_form_lines(text: str) -> str:
    return "\n".join(
        line
        for line in text.split("\n")
        if not line.lstrip().startswith(("allowed_target_forms:", "form_rule:"))
    )


# --- translation prompt: series policy tiers ----------------------------------------------------


def test_locked_and_strong_tiers_state_the_authorized_forms_beside_the_target() -> None:
    policy = _policy(
        _term(SOURCE_TERM, forms=(SPACED, VERBAL)),
        _term(
            "dawn",
            target_term="سپیده",
            status=SeriesTermStatus.STRONG_REFERENCE,
            kind=SeriesProvenanceKind.PROMOTED_REFERENCE,
            forms=("سپیده‌دم",),
        ),
    )

    rendered = render_effective_context(build_effective_context(policy, source_text=SOURCE))
    lines = rendered.split("\n")

    locked_target = lines.index(f"    target: {json.dumps(PREFERRED, ensure_ascii=False)}")
    assert lines[locked_target + 1] == _forms_line(SPACED, VERBAL)
    assert lines[locked_target + 2] == RULE_LINE
    strong_target = lines.index('    target: "سپیده"')
    assert lines[strong_target + 1] == _forms_line("سپیده‌دم")
    assert lines[strong_target + 2] == RULE_LINE
    assert rendered.index(_forms_line(SPACED, VERBAL)) < rendered.index("## 2. STRONG REFERENCE")


def test_a_term_without_forms_renders_exactly_as_before() -> None:
    with_forms = _policy(_term(SOURCE_TERM, forms=(SPACED, VERBAL)))
    without_forms = _policy(_term(SOURCE_TERM))

    rendered_with = render_effective_context(
        build_effective_context(with_forms, source_text=SOURCE)
    )
    rendered_without = render_effective_context(
        build_effective_context(without_forms, source_text=SOURCE)
    )

    assert "allowed_target_forms" not in rendered_without and "form_rule" not in rendered_without
    # The two lines are the entire difference: nothing else about the section moved.
    assert _without_form_lines(rendered_with) == rendered_without


def test_rival_and_observed_forms_are_never_rendered_as_permitted() -> None:
    rival = "بی‌اثر"
    policy = _policy(
        _term(SOURCE_TERM, forms=(VERBAL,), variants=(_narrative(rival), _narrative(VERBAL))),
        _term(
            "before",
            status=SeriesTermStatus.OBSERVED_VARIANTS,
            target_term=None,
            kind=SeriesProvenanceKind.UNRESOLVED,
            variants=(_narrative("پیش"), _narrative("قبل")),
        ),
    )

    rendered = render_effective_context(build_effective_context(policy, source_text=SOURCE))
    permitted = [line for line in rendered.split("\n") if "allowed_target_forms" in line]

    assert permitted == [_forms_line(VERBAL)]
    assert all(rival not in line and "پیش" not in line for line in permitted)


# --- translation prompt: legacy registry block and chapter titles --------------------------------


def test_registry_terminology_block_states_forms_only_for_decisions_that_have_them() -> None:
    registry = _registry(forms=(SPACED, VERBAL))
    rendered = render_terminology(registry, SOURCE, "")
    plain = render_terminology(_registry(forms=()), SOURCE, "")

    stored = registry.decisions[0].allowed_target_forms
    assert sorted(stored) == sorted((SPACED, VERBAL))
    assert _forms_line(*stored) in rendered and RULE_LINE in rendered
    assert "allowed_target_forms" not in plain
    assert _without_form_lines(rendered) == plain


def test_chapter_title_prompt_states_forms_only_for_decisions_that_have_them() -> None:
    title = "The Hushed"
    registry = _registry(forms=(VERBAL,), source_term="Hushed")
    plain = _registry(forms=(), source_term="Hushed")

    rendered = render_title_terminology(registry, title)

    assert _forms_line(VERBAL) in rendered
    assert _without_form_lines(rendered) == render_title_terminology(plain, title)


# --- semantic judge and bounded reviser ----------------------------------------------------------


def _judge_payload(forms: tuple[str, ...]) -> str:
    source = "He was hushed.\n\nThe night was long.\n"
    translation = f"او {PREFERRED} بود.\n\nشب بلند بود.\n"
    tasks = build_tasks(
        source, translation, registry=_registry(forms=forms), paragraph_indexes=(1,)
    )
    return render_user_message(JudgeBatch(tasks), chapter_id="001", chunk_id="001-001")


def test_judge_request_carries_forms_so_an_authorized_form_is_not_read_as_a_deviation() -> None:
    with_forms = _judge_payload((VERBAL,))
    without_forms = _judge_payload(())

    assert '"allowed_target_forms": ["' + VERBAL + '"]' in with_forms
    assert AUTHORIZED_FORMS_RULE in with_forms
    assert (
        "allowed_target_forms" not in without_forms and AUTHORIZED_FORMS_RULE not in without_forms
    )


def test_reviser_request_carries_the_same_forms() -> None:
    source = "He was hushed.\n"
    translation = f"او {PREFERRED} بود.\n"
    (task,) = build_tasks(source, translation, registry=_registry(forms=(VERBAL,)))

    message = render_revision_request(
        chapter_id="001",
        chunk_id="001-001",
        window_indexes=(1,),
        source_blocks=("He was hushed.",),
        target_blocks=(f"او {PREFERRED} بود.",),
        findings={},
        terminology=task.terminology,
        previous_source=None,
        previous_target=None,
        next_source=None,
        next_target=None,
    )

    assert '"allowed_target_forms": ["' + VERBAL + '"]' in message


# --- literary editor -----------------------------------------------------------------------------


def test_literary_editor_sees_forms_and_counts_every_authorized_realization() -> None:
    registry = _registry(forms=(SPACED, VERBAL))
    faithful = f"او {PREFERRED} بود.\n"

    payload = json.loads(terminology_json(registry, SOURCE))
    assert payload[0]["allowed_target_forms"] == registry.decisions[0].allowed_target_forms
    assert (
        "allowed_target_forms" not in json.loads(terminology_json(_registry(forms=()), SOURCE))[0]
    )
    # Swapping the preferred target for an authorized form keeps the canonical count.
    assert terminology_differences(SOURCE, faithful, f"او {VERBAL} بود.\n", registry) == []
    (lost,) = terminology_differences(SOURCE, faithful, "او آرام بود.\n", registry)
    assert (lost.before_count, lost.after_count) == (1, 0)
    # Without forms the same swap is a real terminology change, exactly as before.
    (changed,) = terminology_differences(
        SOURCE, faithful, f"او {VERBAL} بود.\n", _registry(forms=())
    )
    assert (changed.before_count, changed.after_count) == (1, 0)


# --- final-QA analysis ---------------------------------------------------------------------------


def test_a_variant_another_decision_authorizes_is_that_decisions_claim() -> None:
    rival = "بی‌اثر"
    registry = set_decision(
        _registry(forms=(), competing=(rival,), locked=False, decision_source=DecisionSource.AI),
        source_term="muted",
        target_term="خاموش",
        classification=TerminologyType.STORY_TERM,
        decision_source=DecisionSource.USER,
        locked=True,
        allowed_target_forms=(rival,),
    )

    (analysis,) = analyze_competing_variants(
        source_text=SOURCE, translation_text=f"او {rival} ماند.\n", frozen_registry=registry
    )

    assert analysis.rival_candidate_id is not None
    assert analysis.variant_class is CompetingVariantClass.LOCKED_CANONICAL_CONFLICT


# --- repair request ------------------------------------------------------------------------------


def _rejected_job(root: Path, forms: tuple[str, ...]) -> tuple[Path, object]:
    root.mkdir()
    synthetic_project(root)
    _write_registry(
        root,
        [
            {
                "source_term": "Alpha",
                "target_term": "آلفا",
                "classification": TerminologyType.STORY_TERM,
                "decision_source": DecisionSource.USER,
                "locked": True,
                "allowed_target_forms": forms,
            }
        ],
    )
    run_id = _rendered_run(root)
    run_translation_worker(
        run_id,
        project_root=root,
        options=_options(),
        provider_factory=lambda _job: SyntheticProvider("الفا نخست.\n\nترجمه دوم.\n"),
        now=lambda: NOW + timedelta(seconds=10),
    )
    state = load_run_summary(run_id, project_root=root)
    return state.path, state.jobs[0]


def test_repair_request_carries_forms_only_when_the_decision_has_them(tmp_path: Path) -> None:
    with_path, with_job = _rejected_job(tmp_path / "with", ("آلفای",))
    without_path, without_job = _rejected_job(tmp_path / "without", ())

    with_forms = build_repair_request(with_path, with_job, project_root=tmp_path / "with")  # type: ignore[arg-type]
    without_forms = build_repair_request(
        without_path,
        without_job,  # type: ignore[arg-type]
        project_root=tmp_path / "without",
    )

    assert '"allowed_target_forms":["آلفای"]' in with_forms.user
    assert "allowed_target_forms" not in without_forms.user
    assert "form_rule" not in without_forms.user
