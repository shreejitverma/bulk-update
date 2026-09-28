"""Channel-neutral listing artifacts.

A :class:`BuiltListing` is the fully-resolved, ready-to-submit payload for one SKU in one
marketplace — the output of extraction + copy + pricing + mapping, and the input to every channel
call. It is serialised to ``data/build/`` so the exact bytes that were sent can be diffed,
reviewed, and re-sent, which is what makes a submission auditable rather than a black box.

A :class:`SubmissionOutcome` is what came back. Both are persisted in the ledger.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ListingStatus(str, Enum):
    """Where a SKU stands. The progression is deliberately one-way and gated.

    ``BUILT`` and ``SCHEMA_OK`` require no credentials. ``VALIDATED`` means Amazon itself checked
    the payload and created nothing. ``SUBMITTED`` means a listing now exists. Only an explicit
    second action moves a listing to ``LIVE``.
    """

    PENDING = "pending"
    BUILT = "built"
    SCHEMA_OK = "schema_ok"
    SCHEMA_FAILED = "schema_failed"
    VALIDATED = "validated"
    VALIDATION_FAILED = "validation_failed"
    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    LIVE = "live"
    SUPPRESSED = "suppressed"
    ERROR = "error"


class IssueSeverity(str, Enum):
    ERROR = "ERROR"
    WARNING = "WARNING"
    INFO = "INFO"


class ListingIssue(BaseModel):
    """One problem with a listing, from any stage.

    ``source`` distinguishes a locally-detected problem from one Amazon reported, because they
    demand different responses: local issues are fixed in code or the spreadsheet, Amazon's may
    require a category approval or a support case.
    """

    code: str
    message: str
    severity: IssueSeverity = IssueSeverity.ERROR
    attribute_names: list[str] = Field(default_factory=list)
    source: Literal["local", "schema", "amazon", "policy"] = "local"

    @property
    def blocking(self) -> bool:
        return self.severity is IssueSeverity.ERROR

    def __str__(self) -> str:
        where = f" [{', '.join(self.attribute_names)}]" if self.attribute_names else ""
        return f"{self.severity.value} {self.code}{where}: {self.message}"


class OfferTerms(BaseModel):
    """The commercial half of a listing, kept separate from product data because Amazon models
    them separately and a shared-catalog seller may own only one of them."""

    price: Decimal
    currency: str = "USD"
    list_price: Decimal | None = None
    quantity: int = 1
    handling_time_days: int = 3
    condition: str = "new_new"


class BuiltListing(BaseModel):
    """A submission-ready listing for one SKU in one marketplace."""

    model_config = ConfigDict(extra="forbid")

    sku: str  # the seller SKU Amazon keys on — for children, the child SKU
    parent_sku: str | None = None  # set on children of a variation family
    source_sku: str  # the Anzor website SKU this came from
    marketplace_id: str
    marketplace_code: str
    product_type: str
    requirements: str = "LISTING"

    attributes: dict[str, Any]
    offer: OfferTerms | None = None  # None for a variation parent, which is not buyable

    is_parent: bool = False
    variation_theme: str | None = None
    child_skus: list[str] = Field(default_factory=list)

    issues: list[ListingIssue] = Field(default_factory=list)
    status: ListingStatus = ListingStatus.BUILT

    source_url: str = ""
    content_hash: str = ""  # of the source page; changes trigger a rebuild
    payload_hash: str = ""  # of the attributes; unchanged means nothing to resubmit
    built_at: datetime | None = None

    @property
    def blocking_issues(self) -> list[ListingIssue]:
        return [i for i in self.issues if i.blocking]

    @property
    def submittable(self) -> bool:
        return not self.blocking_issues

    def body(self) -> dict[str, Any]:
        """The exact JSON body for a Listings Items PUT."""
        return {
            "productType": self.product_type,
            "requirements": self.requirements,
            "attributes": self.attributes,
        }

    def compute_payload_hash(self) -> str:
        """Hash of exactly what is sent. Unchanged hash means there is nothing to resubmit.

        The product type is part of the body, so changing it alone is a real change.
        """
        canonical = json.dumps(self.body(), sort_keys=True, default=str, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class SubmissionOutcome(BaseModel):
    """What Amazon said. Recorded whether it succeeded or not."""

    sku: str
    marketplace_id: str
    marketplace_code: str
    mode: Literal["VALIDATION_PREVIEW", "SUBMIT", "DELETE"]
    status: ListingStatus
    submission_id: str | None = None
    request_id: str | None = None
    issues: list[ListingIssue] = Field(default_factory=list)
    http_status: int | None = None
    submitted_at: datetime | None = None
    payload_hash: str = ""

    @property
    def accepted(self) -> bool:
        return self.status in (
            ListingStatus.ACCEPTED,
            ListingStatus.VALIDATED,
            ListingStatus.SUBMITTED,
            ListingStatus.LIVE,
        )

    def summary(self) -> str:
        errs = [i for i in self.issues if i.blocking]
        warns = [i for i in self.issues if not i.blocking]
        bits = [f"{self.sku}@{self.marketplace_code}", self.status.value]
        if errs:
            bits.append(f"{len(errs)} error(s)")
        if warns:
            bits.append(f"{len(warns)} warning(s)")
        if self.submission_id:
            bits.append(f"submission={self.submission_id}")
        return " | ".join(bits)
