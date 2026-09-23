"""Minimal non-executable placeholder parsing for the canonical prompt template."""

import re
from collections import Counter

REQUIRED_TEMPLATE_PLACEHOLDERS = frozenset(
    {"chapter_number", "part_number", "glossary", "previous_context", "source_text"}
)
_PLACEHOLDER = re.compile(r"{{\s*([a-z_][a-z0-9_]*)\s*}}")
_ANY_BRACES = re.compile(r"{{.*?}}", re.DOTALL)


class PromptTemplateError(ValueError):
    """Raised when the inert prompt template contract is violated."""


def validate_template(template: str) -> tuple[str, ...]:
    matches = list(_PLACEHOLDER.finditer(template))
    brace_matches = list(_ANY_BRACES.finditer(template))
    if [match.group(0) for match in matches] != [match.group(0) for match in brace_matches]:
        raise PromptTemplateError("prompt template contains an unsupported placeholder expression")
    names = tuple(match.group(1) for match in matches)
    counts = Counter(names)
    missing = REQUIRED_TEMPLATE_PLACEHOLDERS - set(names)
    unknown = set(names) - REQUIRED_TEMPLATE_PLACEHOLDERS
    duplicated = {name for name, count in counts.items() if count != 1}
    if missing:
        raise PromptTemplateError(f"prompt template is missing placeholders: {sorted(missing)}")
    if unknown:
        raise PromptTemplateError(f"prompt template has unknown placeholders: {sorted(unknown)}")
    if duplicated:
        raise PromptTemplateError(
            f"prompt template placeholders must occur exactly once: {sorted(duplicated)}"
        )
    return names


def render_template(template: str, values: dict[str, str]) -> str:
    validate_template(template)
    if set(values) != REQUIRED_TEMPLATE_PLACEHOLDERS:
        raise PromptTemplateError("template values do not match the required placeholder set")

    def substitute(match: re.Match[str]) -> str:
        return values[match.group(1)]

    rendered = _PLACEHOLDER.sub(substitute, template.replace("\r\n", "\n").replace("\r", "\n"))
    return rendered.rstrip("\n") + "\n"
