"""Shared exact lexical-boundary rules for target terminology."""

import re
from functools import lru_cache

# Persian writes a pronominal clitic ("my", "your", "his/her", "our", "your", "their") directly onto
# the noun it belongs to, so a canonical carrying one is the canonical, not a rival spelling. Only
# these six closed forms attach, and the trailing boundary still applies after them: a longer token
# that merely begins with the canonical plus a clitic-shaped letter is a different word. The same
# clitics after a ZWNJ already sit behind a non-word boundary and need no alternative of their own.
#
# The indefinite marker ("a <noun>") is the attached ``ی`` after a consonant. After a stem ending in
# the long vowel ``ا`` or ``و`` Persian writes a hiatus glide before it, so the same marker is
# ``یی``; the lookbehind binds that allomorph to those two final letters and nothing else. After a
# final ``ه`` or ``ی`` the marker is ``‌ای``, already behind a ZWNJ. This is written-form
# recognition of one closed suffix, not stemming: the trailing boundary still applies after it.
PERSIAN_INFLECTION_PATTERN = (
    r"(?:(?<=[او])یی|ی|مان|تان|شان|م|ت|ش|‌(?:ها|های|ام|ات|اش|مان|تان|شان|ای|تر|ترین))"
)
_PERSIAN_SUFFIX = re.compile(PERSIAN_INFLECTION_PATTERN)


def contains_lexeme(text: str, term: str) -> bool:
    """Return whether *term* occurs as an exact boundary-safe lexeme in *text*."""
    # A ZWNJ or combining ezafe is non-word to Python and therefore permits natural inflection,
    # while another base letter is treated as a distinct lexical spelling rather than a match.
    return re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text) is not None


def lexeme_pattern(terms: tuple[str, ...], *, target_language: str) -> re.Pattern[str]:
    """Compile the boundary-safe pattern matching any of *terms* as a target-language lexeme.

    The alternation is ordered by the caller, and Python alternation is first-match, so a caller
    listing the longest form first gets the longest form preferred.  Sharing one builder between
    the "is it present" and "how many are there" questions is what keeps those two answers from
    drifting apart: a counter that recognized a different set of spellings than the presence check
    would make the two halves of a finding disagree.
    """

    alternation = "|".join(re.escape(term) for term in terms)
    if target_language.lower().split("-", maxsplit=1)[0] != "fa":
        return re.compile(r"(?<!\w)(?:" + alternation + r")(?!\w)")
    return re.compile(r"(?<!\w)(?:" + alternation + rf")(?:{_PERSIAN_SUFFIX.pattern})?(?!\w)")


def contains_canonical_lexeme(text: str, term: str, *, target_language: str) -> bool:
    """Match an exact canonical lexeme with only contracted target-language inflections."""
    return lexeme_pattern((term,), target_language=target_language).search(text) is not None


def count_canonical_lexemes(text: str, term: str, *, target_language: str) -> int:
    """Count non-overlapping exact canonical lexemes of *term* in *text*."""
    return sum(1 for _ in lexeme_pattern((term,), target_language=target_language).finditer(text))


def _longest_first(forms: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    # Alternation is first-match, so the longest spelling must be tried first or a shorter form
    # that prefixes it would claim the match and count one realization where the text has one
    # longer one. The sort is stable, so an already longest-first sequence is left untouched.
    return tuple(sorted(dict.fromkeys(forms), key=len, reverse=True))


# A canonical target is a lexeme, and Persian writes a lexeme with regular surface morphology that
# the contracted inflections above do not cover. Each rule below is a closed grammatical suffix or
# paradigm applied to the authorized spelling itself, never a stem guessed from the surface: a
# realization always begins with the authorized form's own letters, so no different word can
# satisfy a decision through it. They widen what proves the canonical present, and only that;
# competing variants are still matched by :func:`lexeme_pattern` alone.
#
# The attached plural: ``ان`` after a consonant or ``ی``, and ``یان`` (hiatus glide) after the
# long vowels ``ا`` / ``و``. A heh-final stem changes its own letter before ``ان`` (``گان``), which
# is not a suffix on the written form and is deliberately not recognized. The plural behind a ZWNJ
# (``‌ها``) already sits behind a non-word boundary. Either plural may carry the ezafe/indefinite
# ``ی`` or an attached pronominal clitic.
_PERSIAN_ATTACHED_PLURAL = r"(?:(?<=[او])یان|(?<![\Wاوه])ان)(?:ی|مان|تان|شان|م|ت|ش)?"
# The head of a multi-word Persian noun phrase is its first word, so that is where a plural goes
# (``فرزند نور`` -> ``فرزندان نور``); a plural on the final word of an ezafe phrase is not Persian.
_PERSIAN_HEAD_PLURAL = r"(?:(?<=[او])یان|(?<![\Wاوه])ان|‌ها(?:ی)?)"
# The ezafe between the words of a phrase is usually unwritten; when it is written after a
# consonant it is a kasra, and after a final heh a hamza above.
_PERSIAN_EZAFE_MARK = "[\u0650\u0654]?"
# A compound verb is a non-verbal element plus a light verb, and a light verb conjugates. A form
# the decision authorized as ``<element><separator><light verb>`` is realized by the same element
# with any finite or non-finite form of that light verb or of its transitive/intransitive partner
# (``کردن`` <-> ``شدن``: "was gentled" and "gentled him" are one compound in two voices), written
# with a separator the decision's own forms use. A pronominal object clitic may sit on the element
# (``...‌اش کرد``), after which the words are spaced. The element alone, or the element with any
# other verb, is not the compound.
_LIGHT_VERB_FORM = re.compile(r"^(?P<element>.+?)(?P<separator>[ ‌])(?:شده|شدن|کرده|کردن)$")
_PERSIAN_ELEMENT_CLITIC = r"(?:‌(?:ام|ات|اش|مان|تان|شان)|مان|تان|شان|م|ت|ش)"
_PERSIAN_LIGHT_VERB = (
    r"(?:ن|ب|نمی[ ‌]|می[ ‌])?"
    r"(?:(?:کرد|شد)(?:ه|ن|م|ی|یم|ید|ند)?|(?:کن|شو)(?:م|ی|د|یم|ید|ند)?)"
)
# Number is the one inflection a canonical can already carry. A canonical written in the plural
# (``مطرودین``) has a singular, and Persian needs it for a single referent; it is never guessed by
# stripping letters, because that turns names into other words (``ساییدین`` -> ``سایید``). It is
# admitted only where the decision itself records that exact singular, as a spelling that differs
# from the canonical by one plural suffix and nothing else.
_PERSIAN_PLURAL_SUFFIXES = ("‌ها", "یان", "ین", "ان", "ها")


def _is_persian(target_language: str) -> bool:
    return target_language.lower().split("-", maxsplit=1)[0] == "fa"


def is_singular_of(canonical_target: str, variant: str) -> bool:
    """Return whether *canonical_target* is exactly *variant* plus one Persian plural suffix."""
    return bool(variant) and any(
        canonical_target == f"{variant}{suffix}" for suffix in _PERSIAN_PLURAL_SUFFIXES
    )


def recorded_singulars(
    forms: tuple[str, ...] | list[str], variants: tuple[str, ...] | list[str]
) -> tuple[str, ...]:
    """The recorded spellings that are the singular of one of *forms* (see the rule above)."""
    return tuple(
        sorted({variant for variant in variants for form in forms if is_singular_of(form, variant)})
    )


def _phrase_alternative(form: str) -> str:
    words = form.split(" ")
    if len(words) == 1:
        return re.escape(form)
    head, *rest = words
    # The plural goes on the head only, and a written ezafe mark may follow every non-final word.
    parts = [rf"{re.escape(head)}{_PERSIAN_HEAD_PLURAL}?{_PERSIAN_EZAFE_MARK}"]
    parts.extend(rf"\ {re.escape(word)}{_PERSIAN_EZAFE_MARK}" for word in rest[:-1])
    parts.append(rf"\ {re.escape(rest[-1])}")
    return "(?:" + "".join(parts) + ")"


def _light_verb_alternatives(forms: tuple[str, ...]) -> tuple[str, ...]:
    separators: dict[str, set[str]] = {}
    for form in forms:
        match = _LIGHT_VERB_FORM.match(form)
        if match is not None:
            separators.setdefault(match.group("element"), set()).add(match.group("separator"))
    alternatives = []
    for element in sorted(separators, key=lambda item: (-len(item), item)):
        joined = "".join(sorted(separators[element]))
        alternatives.append(
            rf"{re.escape(element)}(?:{_PERSIAN_ELEMENT_CLITIC}\ |[{joined}]){_PERSIAN_LIGHT_VERB}"
        )
    return tuple(alternatives)


@lru_cache(maxsize=4096)
def _realization_pattern(
    forms: tuple[str, ...], singulars: tuple[str, ...], target_language: str
) -> re.Pattern[str]:
    if not _is_persian(target_language):
        return lexeme_pattern(forms, target_language=target_language)
    phrases = "|".join(_phrase_alternative(form) for form in forms)
    suffix = rf"(?:{_PERSIAN_ATTACHED_PLURAL}|{_PERSIAN_SUFFIX.pattern})?"
    alternatives = [*_light_verb_alternatives(forms), rf"(?:{phrases}){suffix}"]
    if singulars:
        # A singular carries the contracted inflections only: a second plural suffix on it would
        # be a rival plural, not this canonical in the other number.
        alternation = "|".join(re.escape(form) for form in singulars)
        alternatives.append(rf"(?:{alternation})(?:{_PERSIAN_SUFFIX.pattern})?")
    return re.compile(r"(?<!\w)(?:" + "|".join(alternatives) + r")(?!\w)")


def realization_pattern(
    forms: tuple[str, ...] | list[str],
    *,
    target_language: str,
    singulars: tuple[str, ...] | list[str] = (),
) -> re.Pattern[str]:
    """Compile the pattern matching any grammatical realization of any authorized form.

    This is :func:`lexeme_pattern` plus the regular Persian morphology documented above: the
    attached plural, the plural on a phrase's head, a written ezafe inside a phrase, the
    conjugation of an authorized compound verb, and the recorded singular of a plural canonical
    (*singulars*, from :func:`recorded_singulars`). Forms are tried longest first.
    """
    return _realization_pattern(_longest_first(forms), _longest_first(singulars), target_language)


def contains_canonical_realization(
    text: str,
    forms: tuple[str, ...] | list[str],
    *,
    target_language: str,
    singulars: tuple[str, ...] | list[str] = (),
) -> bool:
    """Match any one explicitly authorized realization, with regular target morphology.

    *forms* is a decision's preferred target plus the further forms it authorized. Each is matched
    with exactly the boundaries and morphology a lone canonical gets, so authorizing a form widens
    what counts as the canonical by that form's own grammatical realizations and nothing else.
    """
    pattern = realization_pattern(forms, target_language=target_language, singulars=singulars)
    return pattern.search(text) is not None


def count_canonical_realizations(
    text: str,
    forms: tuple[str, ...] | list[str],
    *,
    target_language: str,
    singulars: tuple[str, ...] | list[str] = (),
) -> int:
    """Count non-overlapping realizations of any authorized form in *text*."""
    pattern = realization_pattern(forms, target_language=target_language, singulars=singulars)
    return sum(1 for _ in pattern.finditer(text))


# Derivational suffixes are stripped from the final word only, longest first, while the remaining
# stem stays a plausible lexeme.  This is deliberately narrower than the contracted inflection set
# above: it relaxes what counts as canonical *identity*, not merely canonical *spelling*.
_PERSIAN_DERIVATIONAL = ("‌های", "‌ها", "های", "ها", "یان", "ان", "ی")
_MIN_STEM_LENGTH = 5
_MAX_STEM_DEPTH = 3


def canonical_stems(term: str) -> tuple[str, ...]:
    """Return *term* and each progressively de-derived form of its final word.

    Every intermediate form is kept because a canonical target may itself already carry morphology:
    stripping straight to the shortest root would miss the form actually written.
    """

    head, separator, stem = term.rpartition(" ")
    forms = [term]
    for _ in range(_MAX_STEM_DEPTH):
        for suffix in _PERSIAN_DERIVATIONAL:
            if stem.endswith(suffix) and len(stem) - len(suffix) >= _MIN_STEM_LENGTH:
                stem = stem[: -len(suffix)].rstrip("‌")
                forms.append(f"{head}{separator}{stem}")
                break
        else:
            break
    return tuple(forms)


def _morphological_pattern(
    terms: tuple[str, ...] | list[str], *, target_language: str
) -> re.Pattern[str]:
    persian = _is_persian(target_language)
    forms = [form for term in terms for form in (canonical_stems(term) if persian else (term,))]
    return realization_pattern(forms, target_language=target_language)


def contains_morphological_lexeme(text: str, term: str, *, target_language: str) -> bool:
    """Match the canonical lexeme or one of its derivational stems, still boundary-safe."""
    return _morphological_pattern((term,), target_language=target_language).search(text) is not None


def count_morphological_lexemes(text: str, term: str, *, target_language: str) -> int:
    """Count non-overlapping realizations of *term* or one of its derivational stems."""
    return sum(
        1 for _ in _morphological_pattern((term,), target_language=target_language).finditer(text)
    )


def contains_morphological_realization(
    text: str, forms: tuple[str, ...] | list[str], *, target_language: str
) -> bool:
    """Match any authorized realization or one of that realization's own derivational stems."""
    return _morphological_pattern(forms, target_language=target_language).search(text) is not None


def count_morphological_realizations(
    text: str, forms: tuple[str, ...] | list[str], *, target_language: str
) -> int:
    """Count non-overlapping morphological realizations of any authorized form."""
    pattern = _morphological_pattern(forms, target_language=target_language)
    return sum(1 for _ in pattern.finditer(text))


def valid_competing_variants(
    canonical_target: str, variants: tuple[str, ...] | list[str]
) -> tuple[str, ...]:
    """Keep only alternatives that cannot occur naturally inside the canonical target."""
    return tuple(variant for variant in variants if not contains_lexeme(canonical_target, variant))


def carries_canonical_identity(
    canonical_target: str, variant: str, *, target_language: str
) -> bool:
    """Return whether *variant* is the canonical target's own lexeme rather than a rival form.

    A "competing" variant only means something if the text could carry it *instead of* the
    canonical target.  Two shapes fail that test outright and are lexical accidents rather than
    evidence of a contested decision:

    * the canonical target occurs as a boundary-safe lexeme inside the variant, so any text the
      variant matches necessarily also matches the canonical (``Tallanvor`` inside
      ``Martyn Tallanvor``); and
    * the variant is a target-language derivation whose stem is the canonical target, so the two
      are one lexeme written with different morphology (a Persian plural of the canonical); and
    * the variant is, as a whole, one grammatical realization of the canonical target, which a
      translation writing it has therefore rendered canonically; and
    * the canonical is the variant plus one plural suffix, so the variant is the canonical's own
      singular (see :func:`recorded_singulars`).

    This is the mirror of :func:`valid_competing_variants`, which already refuses the containment
    in the other direction.  Neither direction can evidence a rival rendering.
    """
    if contains_canonical_lexeme(variant, canonical_target, target_language=target_language):
        return True
    if not _is_persian(target_language):
        return False
    if is_singular_of(canonical_target, variant):
        return True
    if realization_pattern((canonical_target,), target_language=target_language).fullmatch(variant):
        return True
    return canonical_target in canonical_stems(variant)[1:]


def enforceable_competing_variants(
    canonical_target: str,
    variants: tuple[str, ...] | list[str],
    *,
    target_language: str,
    authorized_forms: tuple[str, ...] | list[str] = (),
) -> tuple[str, ...]:
    """Keep only the recorded variants that can evidence a rival rendering of the canonical.

    Registry storage keeps whatever a decision recorded; QA enforcement must not, because a
    variant carrying the canonical's own identity produces a finding no translation can clear.
    An explicitly authorized target form is the canonical too, so a variant inside one, or one
    carrying its identity, is refused on exactly the same grounds.
    """
    kept = tuple(variants)
    for form in (canonical_target, *authorized_forms):
        kept = tuple(
            variant
            for variant in valid_competing_variants(form, kept)
            if not carries_canonical_identity(form, variant, target_language=target_language)
        )
    return kept
