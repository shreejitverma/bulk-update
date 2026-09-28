"""Planning and executing Amazon submissions from built artifacts.

The CLI is a thin shell over this module so that every decision about *what* gets sent is a pure,
testable function, and the network calls are exercised against a fake SP-API in the test suite.

Planning (:func:`plan_submission`) sorts every selected listing into exactly one bucket:

* ``send``       - will be written.
* ``blocked``    - the local checks already found a blocking problem (no image, schema error).
                   Sending it would create a listing Amazon suppresses, or fail anyway.
* ``orphaned``   - a variation child whose parent is blocked (Amazon cannot attach it), or a
                   parent all of whose children are blocked (it would be an empty page).
* ``unchanged``  - this exact payload was already accepted.
* ``in_flight``  - this exact payload is inside a feed that has not been reconciled yet.

Execution has two paths with identical safety gates:

* :meth:`AmazonSubmitter.submit_items` - Listings Items, one SKU at a time, each previewed with
  VALIDATION_PREVIEW immediately before the write. Synchronous and exact; about 2.5 listings/s.
* :meth:`AmazonSubmitter.submit_feed`  - the bulk path. Parents still go through Listings Items
  (they are few, and children cannot attach until they exist); everything else goes into
  ``JSON_LISTINGS_FEED`` documents per marketplace, after a VALIDATION_PREVIEW spot check.

Either way, a child whose parent failed in this run is not sent.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import structlog

from anzorlist.channels.amazon.client import ClientPool
from anzorlist.channels.amazon.feeds import (
    FeedManifest,
    FeedPending,
    FeedsClient,
    chunk_listings,
    reconcile,
)
from anzorlist.channels.amazon.listings import ListingsClient
from anzorlist.config import Settings
from anzorlist.ingest.row import ListingRow
from anzorlist.marketplaces import Marketplace, resolve, resolve_all
from anzorlist.models.listing import (
    BuiltListing,
    IssueSeverity,
    ListingIssue,
    ListingStatus,
    SubmissionOutcome,
)
from anzorlist.store.db import Ledger, SubmissionState

log = structlog.get_logger(__name__)

DEFAULT_PREVIEW_SAMPLE = 5


# ----------------------------------------------------------------------------- selection


def select_listings(
    listings: Iterable[BuiltListing],
    *,
    skus: list[str] | None = None,
    rows: list[ListingRow] | None = None,
    default_marketplaces: str = "US",
) -> list[BuiltListing]:
    """Choose which built listings an ``amazon`` command acts on.

    ``skus`` match either a website SKU (``R985`` selects the whole family) or an exact listing
    SKU (``R985-7``). Without ``skus``, ``rows`` - the workbook's included rows - decide: a SKU
    set to Include = N, or a marketplace removed from its row, is not acted on even though its
    old build artifacts are still on disk.
    """
    listings = list(listings)
    if skus:
        wanted = {s.strip().upper() for s in skus if s.strip()}
        return [x for x in listings if x.source_sku.upper() in wanted or x.sku.upper() in wanted]
    if rows is None:
        return listings
    allowed: dict[str, set[str]] = {}
    for row in rows:
        markets = resolve_all(row.marketplaces or default_marketplaces)
        allowed[row.sku.upper()] = {m.code for m in markets}
    return [x for x in listings if x.marketplace_code in allowed.get(x.source_sku.upper(), set())]


# ----------------------------------------------------------------------------- planning


@dataclass
class SubmissionPlan:
    send: list[BuiltListing] = field(default_factory=list)
    blocked: list[BuiltListing] = field(default_factory=list)
    orphaned: list[BuiltListing] = field(default_factory=list)
    unchanged: list[BuiltListing] = field(default_factory=list)
    in_flight: list[BuiltListing] = field(default_factory=list)


def plan_submission(
    listings: list[BuiltListing],
    state: Callable[[BuiltListing], SubmissionState],
) -> SubmissionPlan:
    plan = SubmissionPlan()
    blocked_parents: set[tuple[str, str] | None] = {
        (x.marketplace_id, x.sku) for x in listings if x.is_parent and not x.submittable
    }
    # A parent whose selected children are all blocked would be created empty: a detail page
    # with nothing to buy. It waits until at least one child can go with it.
    children: dict[tuple[str, str] | None, list[BuiltListing]] = {}
    for x in listings:
        if x.parent_sku:
            children.setdefault(_parent_key(x), []).append(x)
    childless_parents = {
        (x.marketplace_id, x.sku)
        for x in listings
        if x.is_parent
        and (kids := children.get((x.marketplace_id, x.sku)))
        and not any(k.submittable for k in kids)
    }
    for listing in _ordered(listings):
        if not listing.submittable:
            plan.blocked.append(listing)
        elif (
            _parent_key(listing) in blocked_parents
            or (listing.marketplace_id, listing.sku) in childless_parents
        ):
            plan.orphaned.append(listing)
        else:
            current = state(listing)
            if current == "accepted":
                plan.unchanged.append(listing)
            elif current == "in_flight":
                plan.in_flight.append(listing)
            else:
                plan.send.append(listing)
    return plan


def _ordered(listings: list[BuiltListing]) -> list[BuiltListing]:
    """Parents first: Amazon requires a variation parent to exist before a child names it."""
    return sorted(
        listings, key=lambda x: (not x.is_parent, x.marketplace_code, x.source_sku, x.sku)
    )


def _parent_key(listing: BuiltListing) -> tuple[str, str] | None:
    return (listing.marketplace_id, listing.parent_sku) if listing.parent_sku else None


# ----------------------------------------------------------------------------- execution


@dataclass
class FeedRun:
    """What the feed path did. ``pending`` feeds were created but have not finished."""

    outcomes: list[SubmissionOutcome] = field(default_factory=list)
    manifests: list[FeedManifest] = field(default_factory=list)
    pending: list[FeedManifest] = field(default_factory=list)


class AmazonSubmitter:
    """Runs validation and submission for one invocation, recording everything in the ledger."""

    def __init__(
        self,
        pool: ClientPool,
        settings: Settings,
        ledger: Ledger,
        run_id: str,
        *,
        feed_poll_interval_s: float = 30.0,
    ) -> None:
        self._pool = pool
        self._settings = settings
        self._ledger = ledger
        self._run_id = run_id
        self._poll_interval_s = feed_poll_interval_s

    @property
    def feeds_dir(self) -> Path:
        return self._settings.data_dir / "feeds"

    def _listings_client(self, market: Marketplace) -> ListingsClient:
        return ListingsClient(self._pool.for_region(market.region), self._settings)

    def _record(self, outcome: SubmissionOutcome) -> SubmissionOutcome:
        self._ledger.record_submission(outcome, self._run_id)
        return outcome

    # -- dry run --

    def validate(self, listings: list[BuiltListing]) -> list[SubmissionOutcome]:
        """Amazon's VALIDATION_PREVIEW for every listing. Creates nothing."""
        outcomes: list[SubmissionOutcome] = []
        for listing in _ordered(listings):
            market = resolve(listing.marketplace_code)
            client = self._listings_client(market)
            outcomes.append(self._record(client.put(listing, market, mode="VALIDATION_PREVIEW")))
        return outcomes

    # -- per-item writes --

    def submit_items(
        self, listings: list[BuiltListing], *, preview_first: bool = True
    ) -> list[SubmissionOutcome]:
        """Write each listing through Listings Items. Children of a failed parent are skipped."""
        outcomes: list[SubmissionOutcome] = []
        failed_parents: set[tuple[str, str] | None] = set()
        for listing in _ordered(listings):
            market = resolve(listing.marketplace_code)
            if _parent_key(listing) in failed_parents:
                outcomes.append(_skipped_child(listing))
                continue
            client = self._listings_client(market)
            outcome: SubmissionOutcome | None = None
            if preview_first:
                preview = self._record(client.put(listing, market, mode="VALIDATION_PREVIEW"))
                if not preview.accepted:
                    outcome = preview
            if outcome is None:
                outcome = self._record(client.put(listing, market, mode="SUBMIT", confirm=True))
            if listing.is_parent and not outcome.accepted:
                failed_parents.add((listing.marketplace_id, listing.sku))
            outcomes.append(outcome)
        return outcomes

    # -- bulk writes --

    def submit_feed(
        self,
        listings: list[BuiltListing],
        *,
        preview_sample: int = DEFAULT_PREVIEW_SAMPLE,
        wait: bool = True,
        timeout_s: float = 1800.0,
    ) -> FeedRun:
        run = FeedRun()
        parents = [x for x in listings if x.is_parent]
        others = [x for x in listings if not x.is_parent]

        parent_outcomes = self.submit_items(parents, preview_first=True)
        run.outcomes.extend(parent_outcomes)
        failed_parents: set[tuple[str, str] | None] = {
            (o.marketplace_id, o.sku) for o in parent_outcomes if not o.accepted
        }

        by_market: dict[str, list[BuiltListing]] = {}
        for listing in others:
            if _parent_key(listing) in failed_parents:
                run.outcomes.append(_skipped_child(listing))
                continue
            by_market.setdefault(listing.marketplace_code, []).append(listing)

        for code, group in by_market.items():
            market = resolve(code)
            previews = self.validate(_diverse_sample(group, preview_sample))
            failed = [p for p in previews if not p.accepted]
            if failed:
                # One systematic problem shows up on every SKU. Finding it in a five-listing
                # preview is the whole reason the preview exists, so the feed is not sent.
                run.outcomes.extend(failed)
                failed_skus = {p.sku for p in failed}
                run.outcomes.extend(
                    _not_sent(x, "FeedPreviewFailed", "spot-check preview failed; feed not sent")
                    for x in group
                    if x.sku not in failed_skus
                )
                log.error("submit.feed_preview_failed", marketplace=code, failed=len(failed))
                continue

            feeds = FeedsClient(
                self._pool.for_region(market.region), self._settings, raw_http=self._pool.raw_http
            )
            for chunk in chunk_listings(group):
                manifest = feeds.create(
                    chunk, market, run_id=self._run_id, feeds_dir=self.feeds_dir, confirm=True
                )
                run.manifests.append(manifest)
                for listing in chunk:
                    self._record(_in_flight(listing, manifest))
                if not wait:
                    run.pending.append(manifest)
                    continue
                try:
                    result = feeds.wait(
                        manifest.feed_id, timeout_s=timeout_s, interval_s=self._poll_interval_s
                    )
                except FeedPending:
                    run.pending.append(manifest)
                    continue
                outcomes = reconcile(result, manifest)
                run.outcomes.extend(self.record_feed_outcomes(manifest, outcomes))
        return run

    def record_feed_outcomes(
        self, manifest: FeedManifest, outcomes: list[SubmissionOutcome]
    ) -> list[SubmissionOutcome]:
        for outcome in outcomes:
            self._record(outcome)
        manifest.reconciled = True
        manifest.save(self.feeds_dir)
        return outcomes


def _diverse_sample(listings: list[BuiltListing], n: int) -> list[BuiltListing]:
    """One listing per source SKU first, so the preview covers different products, not five
    sizes of the same ring."""
    seen: set[str] = set()
    first: list[BuiltListing] = []
    rest: list[BuiltListing] = []
    for listing in listings:
        (rest if listing.source_sku in seen else first).append(listing)
        seen.add(listing.source_sku)
    return (first + rest)[: max(0, n)]


def _not_sent(listing: BuiltListing, code: str, message: str) -> SubmissionOutcome:
    return SubmissionOutcome(
        sku=listing.sku,
        marketplace_id=listing.marketplace_id,
        marketplace_code=listing.marketplace_code,
        mode="SUBMIT",
        status=ListingStatus.ERROR,
        issues=[ListingIssue(code=code, message=message, severity=IssueSeverity.ERROR)],
        submitted_at=datetime.now(timezone.utc),
        payload_hash=listing.payload_hash,
    )


def _skipped_child(listing: BuiltListing) -> SubmissionOutcome:
    return _not_sent(
        listing,
        "ParentNotCreated",
        f"parent {listing.parent_sku} was not accepted in this run, so this child was not sent",
    )


def _in_flight(listing: BuiltListing, manifest: FeedManifest) -> SubmissionOutcome:
    return SubmissionOutcome(
        sku=listing.sku,
        marketplace_id=listing.marketplace_id,
        marketplace_code=listing.marketplace_code,
        mode="SUBMIT",
        status=ListingStatus.PENDING,
        submission_id=manifest.feed_id,
        submitted_at=datetime.now(timezone.utc),
        payload_hash=listing.payload_hash,
    )
