"""Anti-hallucination validation for generated jewelry copy.

Jewelry copy is a legal document. The FTC Jewelry Guides make an unqualified "diamond" mean a
*natural* diamond; a lab-grown or simulated stone described without its qualifier is a deceptive
practice, not a typo. Carat weight, metal purity, and stone origin are the three claims that
carry real liability, and they are exactly the claims a language model is most likely to smooth
over — "0.47 ctw" becomes "half-carat", "gold-plated" becomes "gold".

So generated copy is never trusted. It is checked, mechanically, against the extracted
:class:`~anzorlist.models.product.SpecRow` values — which are verbatim from the source page — and
any claim that cannot be traced back is a blocking error.

The check is deliberately conservative: a *number* in the copy that does not appear in the source
facts fails, even if it is arithmetically implied. "Two 0.25 ct stones" is a legitimate way to
express a 0.50 ctw setting, and it still fails here, because the alternative is trusting the
model to do arithmetic about a legal claim.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

import structlog

from anzorlist.generate.sanitize import has_promotional_language
from anzorlist.models.product import Product

log = structlog.get_logger(__name__)

# Amazon's field limits. Titles vary by category; 200 is the fine-jewelry ceiling and the
# practical display cut-off is far lower, which the style check enforces separately.
MAX_TITLE_LEN = 200
MAX_BULLET_LEN = 500
MAX_DESCRIPTION_LEN = 2000
MAX_SEARCH_TERMS_BYTES = 250

# Claims that require an explicit qualifier from the source. Mapping is
# {claim word: qualifiers that must NOT be contradicted}.
ORIGIN_TERMS = ("natural", "genuine", "real", "earth-mined", "earth mined", "mined")
SYNTHETIC_TERMS = (
    "lab-grown",
    "lab grown",
    "laboratory-grown",
    "synthetic",
    "created",
    "simulated",
    "imitation",
    "cubic zirconia",
    "cz",
    "moissanite",
)
PLATING_TERMS = ("plated", "filled", "vermeil", "overlay", "bonded", "electroplate")

GEM_WORDS = (
    "diamond",
    "sapphire",
    "ruby",
    "emerald",
    "pearl",
    "topaz",
    "amethyst",
    "garnet",
    "aquamarine",
    "opal",
    "tanzanite",
    "morganite",
    "citrine",
    "peridot",
    "tourmaline",
    "onyx",
    "turquoise",
    "jade",
)
METAL_WORDS = (
    "gold",
    "platinum",
    "silver",
    "palladium",
    "titanium",
    "tungsten",
    "stainless steel",
    "rhodium",
)

# Numbers that carry no product claim and so are exempt from traceability.
_BENIGN_NUMBER = re.compile(r"\b(?:1|2|3|4|5|6|7|8|9|10|100)\b")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_FORBIDDEN_TITLE_CHARS = re.compile(r"[!$?_{}^¬¦~]|\bASIN\b", re.IGNORECASE)


@dataclass
class CopyIssue:
    field_name: str
    code: str
    message: str
    severity: str = "ERROR"

    def __str__(self) -> str:
        return f"[{self.severity}] {self.field_name} / {self.code}: {self.message}"


@dataclass
class ValidationReport:
    issues: list[CopyIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(i.severity == "ERROR" for i in self.issues)

    @property
    def errors(self) -> list[CopyIssue]:
        return [i for i in self.issues if i.severity == "ERROR"]

    def add(self, field_name: str, code: str, message: str, severity: str = "ERROR") -> None:
        self.issues.append(CopyIssue(field_name, code, message, severity))

    def feedback(self) -> str:
        """Rendered for the retry prompt. The model is told what failed, not how to game it."""
        return "\n".join(f"- {i.field_name}: {i.message}" for i in self.errors)


class FactSheet:
    """The set of claims the source page actually supports.

    Built once per product from the verbatim spec rows plus the marketing title. Everything the
    validator allows must be present here.
    """

    def __init__(self, product: Product) -> None:
        self.product = product
        # The authoritative corpus: raw spec labels+values and the marketing title, lowercased.
        parts = [f"{r.label} {r.value}" for r in product.specs]
        parts.append(product.marketing_title_raw)
        if product.short_name:
            parts.append(product.short_name)
        self.corpus = " ".join(parts).lower()

        self.numbers: set[str] = {_norm_number(n) for n in _NUMBER.findall(self.corpus)}
        self.gems: set[str] = {g for g in GEM_WORDS if g in self.corpus}
        self.metals: set[str] = {m for m in METAL_WORDS if m in self.corpus}
        self.mentions_synthetic = any(t in self.corpus for t in SYNTHETIC_TERMS)
        self.mentions_natural = any(t in self.corpus for t in ORIGIN_TERMS)
        self.mentions_plating = any(t in self.corpus for t in PLATING_TERMS)

    def supports_number(self, value: str) -> bool:
        return _norm_number(value) in self.numbers

    def summary(self) -> str:
        return (
            f"gems={sorted(self.gems)} metals={sorted(self.metals)} "
            f"numbers={len(self.numbers)} synthetic={self.mentions_synthetic} "
            f"plated={self.mentions_plating}"
        )


def _norm_number(raw: str) -> str:
    """Normalise so 0.50, .5, and 0.5 compare equal — they are the same claim."""
    try:
        d = Decimal(raw).normalize()
    except Exception:  # noqa: BLE001 — a non-numeric token is simply not a number claim
        return raw
    return format(d, "f").rstrip("0").rstrip(".") or "0"


def validate_copy(
    *,
    product: Product,
    title: str,
    bullets: list[str],
    description: str,
    search_terms: str = "",
) -> ValidationReport:
    """Check every generated field against the source facts and Amazon's format rules."""
    report = ValidationReport()
    facts = FactSheet(product)

    _check_title(title, facts, report)
    for i, bullet in enumerate(bullets, start=1):
        _check_bullet(bullet, i, facts, report)
    _check_description(description, facts, report)
    _check_search_terms(search_terms, title, report)

    log.info(
        "copy.validated",
        sku=product.sku,
        ok=report.ok,
        errors=len(report.errors),
        facts=facts.summary(),
    )
    return report


# ---------------------------------------------------------------------------- field checks


def _check_title(title: str, facts: FactSheet, report: ValidationReport) -> None:
    if not title.strip():
        report.add("title", "Empty", "title is empty")
        return
    if len(title) > MAX_TITLE_LEN:
        report.add(
            "title",
            "TooLong",
            f"{len(title)} chars; Amazon's fine-jewelry limit is {MAX_TITLE_LEN}",
        )
    if len(title) < 20:
        report.add(
            "title",
            "TooShort",
            f"{len(title)} chars is too short to be informative",
            severity="WARNING",
        )
    if title.isupper():
        report.add("title", "AllCaps", "Amazon rejects all-caps titles")
    if _FORBIDDEN_TITLE_CHARS.search(title):
        report.add(
            "title",
            "ForbiddenChar",
            "contains a character Amazon disallows in titles (! $ ? _ { } ^ ~)",
        )
    _check_claims("title", title, facts, report)


def _check_bullet(bullet: str, index: int, facts: FactSheet, report: ValidationReport) -> None:
    name = f"bullet_{index}"
    if len(bullet) > MAX_BULLET_LEN:
        report.add(name, "TooLong", f"{len(bullet)} chars; the limit is {MAX_BULLET_LEN}")
    if bullet.isupper():
        report.add(name, "AllCaps", "Amazon rejects all-caps bullets")
    _check_claims(name, bullet, facts, report)


def _check_description(description: str, facts: FactSheet, report: ValidationReport) -> None:
    if len(description) > MAX_DESCRIPTION_LEN:
        report.add(
            "description", "TooLong", f"{len(description)} chars; keep under {MAX_DESCRIPTION_LEN}"
        )
    _check_claims("description", description, facts, report)


def _check_search_terms(terms: str, title: str, report: ValidationReport) -> None:
    if not terms:
        return
    size = len(terms.encode("utf-8"))
    if size > MAX_SEARCH_TERMS_BYTES:
        report.add(
            "search_terms", "TooLong", f"{size} bytes; the limit is {MAX_SEARCH_TERMS_BYTES}"
        )
    title_words = {w.lower().strip(",.") for w in title.split()}
    repeated = [w for w in terms.replace(",", " ").split() if w.lower() in title_words]
    if repeated:
        report.add(
            "search_terms",
            "Duplicated",
            f"repeats words already in the title ({', '.join(sorted(set(repeated))[:5])}); "
            f"Amazon ignores duplicates, so the bytes are wasted",
            severity="WARNING",
        )


# ---------------------------------------------------------------------------- claim checks


def _check_claims(field_name: str, text: str, facts: FactSheet, report: ValidationReport) -> None:
    """The core anti-hallucination pass, shared by every copy field."""
    if not text:
        return
    lowered = text.lower()

    promo = has_promotional_language(text)
    if promo:
        report.add(
            field_name,
            "Promotional",
            f"contains promotional language Amazon prohibits: {', '.join(promo)}",
        )

    # 1. Numeric claims must be traceable to the source specs.
    for number in _NUMBER.findall(text):
        if _BENIGN_NUMBER.fullmatch(number):
            continue
        if not facts.supports_number(number):
            report.add(
                field_name,
                "UnsupportedNumber",
                f"the figure {number!r} does not appear anywhere in the product's Item "
                f"Details. Use only measurements stated on the source page.",
            )

    # 2. Gemstones must be ones the source names.
    for gem in GEM_WORDS:
        if re.search(rf"\b{re.escape(gem)}s?\b", lowered) and gem not in facts.gems:
            report.add(
                field_name,
                "UnsupportedGemstone",
                f"mentions {gem!r}, which the source page does not list",
            )

    # 3. Metals must be ones the source names.
    for metal in METAL_WORDS:
        if re.search(rf"\b{re.escape(metal)}\b", lowered) and metal not in facts.metals:
            report.add(
                field_name,
                "UnsupportedMetal",
                f"mentions {metal!r}, which the source page does not list",
            )

    # 4. FTC: never upgrade a synthetic/simulated stone to natural or genuine.
    if facts.mentions_synthetic:
        for term in ORIGIN_TERMS:
            if re.search(rf"\b{re.escape(term)}\b", lowered):
                report.add(
                    field_name,
                    "FtcOriginUpgrade",
                    f"claims {term!r} while the source describes a lab-grown or simulated "
                    f"stone. Under the FTC Jewelry Guides this is a deceptive claim.",
                )

    # 5. FTC: never drop the plating qualifier — "gold plated" is not "gold".
    if facts.mentions_plating:
        mentions_metal = any(re.search(rf"\b{re.escape(m)}\b", lowered) for m in facts.metals)
        keeps_qualifier = any(t in lowered for t in PLATING_TERMS)
        if mentions_metal and not keeps_qualifier:
            report.add(
                field_name,
                "FtcPlatingOmitted",
                "names the metal without the plating qualifier the source states. "
                "'Gold plated' must never be shortened to 'gold'.",
            )

    # 6. Absolute superlatives are unverifiable claims regardless of category.
    for word in (
        "finest",
        "highest quality",
        "flawless",
        "investment grade",
        "certified",
        "appraised at",
        "conflict-free",
        "ethically sourced",
    ):
        if word in lowered and word not in facts.corpus:
            report.add(
                field_name,
                "UnverifiableClaim",
                f"claims {word!r}, which the source page does not substantiate",
            )
