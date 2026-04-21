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
