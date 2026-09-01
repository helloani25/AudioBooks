import re


_INLINE_MATH_PLACEHOLDER = re.compile(r"@xmath\d+")
_LABEL_PLACEHOLDER = re.compile(r"\blabeleq\d*\b")
_MATH_OPERATOR_SEP = re.compile(r"\s*&\s*([=+\-*/])\s*&\s*")
_DOUBLE_AMPERSANDS = re.compile(r"\s*&{2,}\s*")
_LATEX_MATH_SPAN = re.compile(
    r"""
    (?P<span>
        [=\+\-*/^_()\[\]{},\s']*
        \\(?:frac|int|delta|nabla|mathbf|hat|left|right)
        [^.!?;:]*
    )
    """,
    re.VERBOSE,
)
_RUN_OF_OPEN_BRACKETS = re.compile(r"\[\s*(?:\[\s*)+")
_RUN_OF_CLOSE_BRACKETS = re.compile(r"\]\s*(?:\]\s*)+")
_SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+([,.;:!?])")
_REPEATED_PUNCTUATION = re.compile(r"([!?])\1{2,}")
_LATEX_LIKE_IDENTIFIERS = (
    (re.compile(r"frac12"), r"\\frac{1}{2}"),
    (re.compile(r"\bleft\("), r"\\left("),
    (re.compile(r"\bright\)"), r"\\right)"),
    (re.compile(r"\bleft\["), r"\\left["),
    (re.compile(r"\bright\]"), r"\\right]"),
    (re.compile(r"\bleft\{"), r"\\left{"),
    (re.compile(r"\bright\}"), r"\\right}"),
    (re.compile(r"nonumber"), r""),
    (re.compile(r"endaligned"), r""),
    (re.compile(r"nabla"), r"\\nabla"),
    (re.compile(r"delta_ij"), r"\\delta_{ij}"),
    (re.compile(r"(?<!\\)delta\("), r"\\delta("),
    (re.compile(r"mathbfx"), r"\\mathbf{x}"),
    (re.compile(r"mathbfy"), r"\\mathbf{y}"),
    (re.compile(r"mathbfn"), r"\\mathbf{n}"),
    (re.compile(r"hatbf\s*([a-zA-Z])"), r"\\hat{\\mathbf{\1}}"),
    (re.compile(r"int_v"), r"\\int_V"),
    (re.compile(r"intlimits_0p"), r"\\int_{0}^{p}"),
)
_SPACED_MATH_DELIM_OPEN = re.compile(r"\\\s*\[")
_SPACED_MATH_DELIM_CLOSE = re.compile(r"\\\s*\]")
# Headers that mark the start of the model's actual answer; we slice AFTER these.
_ANSWER_HEADER_RE = re.compile(
    r"(?im)^\s*#{3,}\s*(?:summary|character\s+profiles|narrator\s+profile|category)\s*:?\s*"
)
# Prompt section labels emitted by the prompt builders. We cut BEFORE the first
# of these to drop leaked scaffolding — but only these known labels, so a
# legitimate markdown heading such as a character-name '### Oliver Twist' in a
# profile is preserved instead of being truncated away.
_SCAFFOLD_LABELS = (
    r"instruction|constraints?|excerpt|input\s+summaries|response|book|"
    r"section|scope|chunk|story\s+so\s+far|chapter\s+summaries|"
    r"for\s+each\s+person|categories|author|reply\b"
)
_PROMPT_MARKER_RE = re.compile(r"(?im)^\s*#{3,}\s*(?:" + _SCAFFOLD_LABELS + r")")


def truncate_at_prompt_scaffold(text: str) -> str:
    """Cut a raw completion at the first leaked prompt-scaffolding header.

    Preserves legitimate markdown headings (e.g. a character-name '### Oliver
    Twist' in a profile), unlike a naive cut at the first '###'.
    """
    text = text.replace("\xa0", " ")
    marker = _PROMPT_MARKER_RE.search(text)
    return (text[: marker.start()] if marker else text).strip()


def _wrap_latex_math_spans(text: str) -> str:
    def replacer(match: re.Match[str]) -> str:
        raw_span = match.group("span")
        prefix = " " if raw_span[:1].isspace() else ""
        span = raw_span.strip()
        if span.startswith(r"\(") and span.endswith(r"\)"):
            return prefix + span
        return prefix + rf"\({span}\)"

    return _LATEX_MATH_SPAN.sub(replacer, text)


def normalize_summary_text(summary: str) -> str:
    """Clean up common model-output artifacts in generated summaries.

    Long-form generation can occasionally emit repeated bracket tokens around
    math expressions. This keeps the summary readable without changing the
    underlying meaning.
    """

    text = summary.replace("\xa0", " ")
    text = _INLINE_MATH_PLACEHOLDER.sub("", text)
    text = _LABEL_PLACEHOLDER.sub("", text)
    text = text.replace("&=&", " = ")
    text = _MATH_OPERATOR_SEP.sub(r" \1 ", text)
    text = _DOUBLE_AMPERSANDS.sub(" ", text)
    text = _SPACED_MATH_DELIM_OPEN.sub(r"\\[", text)
    text = _SPACED_MATH_DELIM_CLOSE.sub(r"\\]", text)
    for pattern, replacement in _LATEX_LIKE_IDENTIFIERS:
        text = pattern.sub(replacement, text)
    text = text.replace(".:", ". ")
    text = _RUN_OF_OPEN_BRACKETS.sub("[ ", text)
    text = _RUN_OF_CLOSE_BRACKETS.sub("] ", text)
    text = _REPEATED_PUNCTUATION.sub(r"\1", text)
    text = _wrap_latex_math_spans(text)
    text = re.sub(r"\s+", " ", text).strip()
    text = _SPACE_BEFORE_PUNCTUATION.sub(r"\1", text)
    text = re.sub(r"\.\s*:", ". ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


_MARKDOWN_HEADING_RE = re.compile(r"(?m)^\s*#{1,6}\s+\S")
_BULLET_FIELD_RE = re.compile(r"(?m)^\s*[-*]\s+\S")
_LEADING_RULE_RE = re.compile(r"^\s*[-*_]{3,}\s*")


def _looks_like_profile(text: str) -> bool:
    """Detect character/figure profile output (headings or repeated bullet fields).

    Profiles use per-person '###' headings and '- Field:' bullets, whereas the
    summaries this module also cleans are flowing prose.
    """
    if _MARKDOWN_HEADING_RE.search(text):
        return True
    return len(_BULLET_FIELD_RE.findall(text)) >= 2


def normalize_profile_text(profile: str) -> str:
    """Clean profile output while preserving its line structure.

    Unlike normalize_summary_text (which flattens prose to a single line), this
    keeps the per-character headings and bullet fields so profiles stay readable:
    it drops a leading horizontal rule, collapses only intra-line whitespace, and
    caps consecutive blank lines.
    """
    text = profile.replace("\xa0", " ")
    text = _LEADING_RULE_RE.sub("", text)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    cleaned = "\n".join(lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def extract_primary_summary_text(generated_text: str) -> str:
    """Extract the model's answer span and drop leaked prompt scaffolding.

    Works for both summaries and character/narrator profiles: slice after the
    answer header when present, then trim any leaked prompt section. Markdown
    headings that are part of the answer (e.g. '### <Character Name>') are kept
    because only the known scaffolding labels trigger truncation. Profile-shaped
    output keeps its line structure; prose summaries are flattened to one block.
    """
    text = generated_text.replace("\xa0", " ")

    answer_header = _ANSWER_HEADER_RE.search(text)
    if answer_header:
        text = text[answer_header.end() :]

    prompt_marker = _PROMPT_MARKER_RE.search(text)
    if prompt_marker:
        text = text[: prompt_marker.start()]

    extra_answer = _ANSWER_HEADER_RE.search(text)
    if extra_answer:
        text = text[: extra_answer.start()]

    if _looks_like_profile(text):
        return normalize_profile_text(text)
    return normalize_summary_text(text)
