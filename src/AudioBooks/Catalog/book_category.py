"""
Shared book category classification used by both the catalog layer and the summarizer.

Four tiers map the Audible/Amazon genre taxonomy to distinct audiobook profile styles:

  dramatic     — fiction of all kinds: mystery, thriller, romance, sci-fi, fantasy, horror,
                 children's, young adult, comedy, drama, comics
  biographical — biography, memoir, history, true crime, travel writing, sports narratives
  analytical   — science, technology, architecture, philosophy, politics, law, medicine,
                 engineering, textbooks, arts criticism
  practical    — self-help, health & fitness, cookbooks, crafts, parenting, reference guides

Priority order when classifying: dramatic > analytical > biographical > practical > dramatic (default).
Most unlabelled Gutenberg books are fiction so the fallback is dramatic.
"""

from __future__ import annotations

import re

DRAMATIC = "dramatic"
BIOGRAPHICAL = "biographical"
ANALYTICAL = "analytical"
PRACTICAL = "practical"

# ── Subject keyword patterns ────────────────────────────────────────────────

_DRAMATIC_RE = re.compile(
    r"\b(?:"
    # core fiction labels
    r"fiction|novel|novella|drama|plays?"
    r"|mystery|detective"
    r"|romance|love\s+stor"
    r"|thriller|suspense"
    r"|horror|gothic|supernatural|ghost\s+stor"
    r"|fantasy|fairy\s+tales?|folk\s+tales?|fables?"
    r"|science\s+fiction|sci-fi"
    r"|satire|comedy|humou?r(?:ous)?"
    r"|allegory|fable|parable"
    r"|adventure(?:\s+stories?)?"
    r"|sea\s+stories?|war\s+stories?|western\s+stories?"
    r"|short\s+stories?|antholog"
    r"|bildungsroman|picaresque|epistolary"
    r"|dystopi|utopi"
    r"|young\s+adult"
    r"|historical\s+fiction|crime\s+fiction|domestic\s+fiction|social\s+fiction"
    r"|children.s\s+(?:fiction|literature|stories?)"
    r"|melodrama|tragedy|farce"
    r"|manga|comic|graphic\s+novel"
    r")\b",
    re.IGNORECASE,
)

_ANALYTICAL_RE = re.compile(
    r"\b(?:"
    # compound analytical subjects that must beat simple "history"
    r"natural\s+histor|art\s+histor|church\s+histor|literary\s+histor"
    r"|literary\s+criticism|music\s+theor|political\s+econom"
    r"|social\s+science|computer\s+science|political\s+science"
    r"|natural\s+philosoph"
    # single-word analytical domains
    r"|architecture|science|mathematics|math"
    r"|physics|chemistry|astronomy"
    r"|biology|botany|zoology|ecology|anatomy|physiology"
    r"|geology|mineralogy|paleontolog"
    r"|medicine|anatomy|surgery|pharmacolog"
    r"|engineering|technology"
    r"|philosophy|ethics|logic|metaphysics"
    r"|economics|econom"
    r"|politics|government"
    r"|law|jurisprudence"
    r"|sociology|anthropology"
    r"|linguistics|grammar|philolog|rhetoric"
    r"|theology|religion(?:\s+\w+)?|spirituality"
    r"|psychology|psychiatry"
    r"|archaeology"
    r"|geography|cartography"
    r"|textbook|treatise"
    r")\b",
    re.IGNORECASE,
)

_BIOGRAPHICAL_RE = re.compile(
    r"\b(?:"
    r"biograph|autobiograph"
    r"|memoir|memoirs"
    r"|diaries?|diary"
    r"|correspondence|letters"
    r"|reminiscence|anecdote|recollection"
    r"|personal\s+narrative"
    r"|true\s+crime"
    r"|histor(?:y|ies|ical)"
    r"|military"
    r"|expedition|exploration|explorer"
    r"|travel(?:\s+writing|\s+accounts?|\s+narratives?)?"
    r")\b",
    re.IGNORECASE,
)

_PRACTICAL_RE = re.compile(
    r"\b(?:"
    r"self.help|self\s+improvement|personal\s+development|motivational"
    r"|cooking|recipes?|cookbook|food\s+and\s+wine"
    r"|gardening|horticulture"
    r"|craft|sewing|knitting|needlework"
    r"|farming|agriculture|husbandry"
    r"|household|housekeeping|home\s+improvement"
    r"|parenting|childcare|child\s+rearing"
    r"|health|fitness|nutrition|diet|wellness|exercise"
    r"|relationships|dating"
    r"|career|job\s+hunting"
    r"|manual|handbook|reference|almanac|dictionary|encyclopedia|guide"
    r"|test\s+prep"
    r")\b",
    re.IGNORECASE,
)


def classify_book_strict(subjects: list[str]) -> str | None:
    """Return the category when a subject keyword matches, or None when nothing matches.

    Use this when you need to distinguish a genuine keyword match from the default
    fallback — for example, to decide whether to run an LLM classification instead.
    """
    if not subjects:
        return None

    joined = " | ".join(subjects)

    if _DRAMATIC_RE.search(joined):
        return DRAMATIC
    if _ANALYTICAL_RE.search(joined):
        return ANALYTICAL
    if _BIOGRAPHICAL_RE.search(joined):
        return BIOGRAPHICAL
    if _PRACTICAL_RE.search(joined):
        return PRACTICAL
    return None


def classify_book(subjects: list[str]) -> str:
    """Return the profile category for a book given its subject/genre list.

    Priority: dramatic > analytical > biographical > practical > dramatic (default).
    Most unlabelled Gutenberg books are fiction so the fallback is dramatic.
    """
    return classify_book_strict(subjects) or DRAMATIC
