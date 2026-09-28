"""Scrub the website description into something Amazon will accept.

``Product.long_description_raw`` is contaminated by design: the legacy store's description field
carries shipping promises, phone numbers, "click here" navigation, financing offers, and
competitor comparisons. Every one of those is an Amazon listing-policy violation that gets a
listing suppressed — not rejected at submission, which would at least be visible, but silently
delisted days later.

This module is **deterministic and runs before the LLM**, for two reasons: a regex cannot be
prompt-injected by the source text, and a removal that must always happen should not depend on a
model choosing to do it. The LLM then works from sanitised text plus the structured specs.

Nothing here invents text. It only deletes and normalises.
"""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass, field

import structlog

log = structlog.get_logger(__name__)

# Each rule is (name, pattern). Order matters: phone/email removal runs before sentence
# splitting so a stripped fragment does not leave a dangling half-sentence.
_BANNED_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("phone", re.compile(r"\(?\b\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b")),
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")),
    ("url", re.compile(r"\b(?:https?://|www\.)\S+", re.IGNORECASE)),
    (
        "navigation",
        re.compile(
            r"\b(?:click here|click on|see (?:our|the) (?:website|site|catalog)|"
            r"visit (?:us|our)|scroll down|add to cart|order now|call (?:us|today|now))\b",
            re.IGNORECASE,
        ),
    ),
    (
        "shipping_promise",
        re.compile(
            r"\b(?:free shipping|ships? (?:same|next) day|overnight (?:delivery|shipping)|"
            r"expedited shipping|delivery guaranteed|arrives? (?:by|in) \d+)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "guarantee",
        re.compile(
            r"\b(?:\d+[- ]day (?:money[- ]back|return|guarantee)|satisfaction guaranteed|"
            r"lifetime (?:warranty|guarantee)|risk[- ]free)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "price_claim",
        re.compile(
            r"\b(?:lowest price|best price|cheapest|price match|wholesale price|"
            r"below (?:retail|cost)|\d+% off|sale price|special offer|discount)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "competitor",
        re.compile(
            r"\b(?:compare (?:to|with)|unlike (?:other|competitors)|better than|"
            r"cheaper than|beat(?:s)? (?:any|the) )\b",
            re.IGNORECASE,
        ),
    ),
    (
        "financing",
        re.compile(r"\b(?:layaway|financing available|payment plan|credit terms)\b", re.IGNORECASE),
    ),
    (
        "contact_cta",
        re.compile(
            r"\b(?:contact us|email us|call for|inquire"
            r"|for more information,?\s*(?:call|email|visit))\b",
            re.IGNORECASE,
        ),
    ),
    ("html_entity_junk", re.compile(r"&[a-z]+;|&#\d+;", re.IGNORECASE)),
)

# Amazon rejects these outright in titles and bullets. Applied as a final guard on generated copy.
PROMOTIONAL_WORDS: tuple[str, ...] = (
    "best",
    "cheapest",
    "sale",
    "free",
    "bonus",
    "discount",
    "guarantee",
    "guaranteed",
    "hot",
    "new arrival",
    "limited time",
    "closeout",
    "clearance",
    "wholesale",
    "top rated",
    "#1",
    "number one",
    "must have",
    "amazing",
    "perfect gift",
)

_WHITESPACE = re.compile(r"[ \t ]+")
_MULTI_NEWLINE = re.compile(r"\n{2,}")
_ORPHAN_PUNCT = re.compile(r"(?:^|\s)[,;:.]+(?=\s|$)")
_REPEATED_PUNCT = re.compile(r"([!?.]){2,}")


@dataclass
class SanitizeResult:
    """Cleaned text plus a record of what was removed, so removals stay auditable."""

    text: str
    removed: dict[str, list[str]] = field(default_factory=dict)
    original_length: int = 0

    @property
    def removed_count(self) -> int:
        return sum(len(v) for v in self.removed.values())

    def report(self) -> str:
        if not self.removed:
            return "no policy-violating content found"
        return "; ".join(f"{kind} x{len(items)}" for kind, items in sorted(self.removed.items()))


def normalize_text(text: str) -> str:
    """Repair the legacy encoding artefacts before anything else looks at the text.

    The source is windows-1252 served as iso-8859-1, so smart quotes and dashes arrive as
    mojibake. NFKC folds them to ASCII-adjacent forms that Amazon's title validator accepts.
    """
    text = html.unescape(text)
    text = unicodedata.normalize("NFKC", text)
    replacements = {
        "‘": "'",
        "’": "'",
        "“": '"',
        "”": '"',
        "–": "-",
        "—": " - ",
        "…": "...",
        " ": " ",
        "�": "",  # replacement char from a failed decode — drop, never guess
    }
    for bad, good in replacements.items():
        text = text.replace(bad, good)
    return text


MIN_WORDS_AFTER_REDACTION = 4


def sanitize(raw: str) -> SanitizeResult:
    """Strip policy-violating content from the raw website description.

    Removal is applied **per sentence** so that thinness can be judged against what the sentence
    lost. An untouched short sentence ("Solid 14k gold.") is legitimate product copy and is kept;
    a sentence that was gutted down to a fragment ("Call .") is dropped, because shipping a
    half-sentence to a customer-facing field is worse than shipping nothing.
    """
    result = SanitizeResult(text="", original_length=len(raw))
    if not raw.strip():
        return result

    text = normalize_text(raw)
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    kept: list[str] = []

    for sentence in sentences:
        cleaned = sentence
        redacted = False
        for name, pattern in _BANNED_PATTERNS:
            hits = pattern.findall(cleaned)
            if not hits:
                continue
            redacted = True
            result.removed.setdefault(name, []).extend(
                [h if isinstance(h, str) else " ".join(h) for h in hits][:10]
            )
            cleaned = pattern.sub(" ", cleaned)

        cleaned = _tidy(cleaned)
        if not cleaned:
            if redacted:
                result.removed.setdefault("gutted_sentence", []).append(sentence[:60])
            continue

        # Only a sentence that actually lost content is judged for thinness. An untouched
        # sentence is the author's own wording and is never second-guessed on length.
        if redacted:
            words = [w for w in re.findall(r"[A-Za-z]{2,}", cleaned)]
            if len(words) < MIN_WORDS_AFTER_REDACTION:
                result.removed.setdefault("gutted_sentence", []).append(sentence[:60])
                continue

        kept.append(cleaned)

    text = _MULTI_NEWLINE.sub("\n", " ".join(kept)).strip()
    result.text = text
    log.info(
        "sanitize.done", original=result.original_length, cleaned=len(text), removed=result.report()
    )
    return result


def _tidy(text: str) -> str:
    """Collapse the whitespace and orphaned punctuation a redaction leaves behind."""
    text = _REPEATED_PUNCT.sub(r"\1", text)
    text = _ORPHAN_PUNCT.sub(" ", text)
    text = _WHITESPACE.sub(" ", text)
    return text.strip(" ,;:")


def has_promotional_language(text: str) -> list[str]:
    """Return promotional terms present in ``text``. Used as a hard gate on generated copy.

    Word-boundary matched so "bestseller" is caught but "the best-fitting bezel" is too — that is
    intentional. Amazon's own filter is at least this aggressive, and a suppressed listing costs
    far more than a reworded bullet.
    """
    lowered = text.lower()
    found = []
    for word in PROMOTIONAL_WORDS:
        if re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", lowered):
            found.append(word)
    return found


def strip_html(text: str) -> str:
    """Remove any HTML tags. Amazon's product_description accepts a small tag subset, but the
    legacy store emits unbalanced markup that renders as literal angle brackets on the detail
    page."""
    return _WHITESPACE.sub(" ", re.sub(r"<[^>]+>", " ", text)).strip()
