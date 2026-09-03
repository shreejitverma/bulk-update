"""Feeds API (2021-06-30) — the bulk path, for catalogs the per-SKU API would take too long on.

Listings Items is the default because it validates synchronously. But it is capped near 5
requests/second, so a 3,000-SKU catalog with size variations is roughly 20,000 writes: over an
hour of wall clock. ``JSON_LISTINGS_FEED`` submits the same message objects in one document.

The trade-off is honest and worth stating: a feed is asynchronous and opaque until it finishes.
So the pipeline validates every message locally against the product-type schema *and* spot-checks
a sample through VALIDATION_PREVIEW before a feed is ever built. The feed carries only payloads
already known to be well-formed.

Four steps, in order:

1. ``createFeedDocument`` — returns an upload URL.
2. PUT the document to that URL (no auth header — it is presigned; adding one makes S3 reject it).
3. ``createFeed`` — hand Amazon the document ID.
4. Poll ``getFeed``, then download and parse the result document, which is gzipped.
"""

from __future__ import annotations

import gzip
import io
import json
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx
import structlog

from anzorlist.channels.amazon.client import SpApiClient
from anzorlist.config import Settings
from anzorlist.marketplaces import Marketplace
from anzorlist.models.listing import (
    BuiltListing,
    IssueSeverity,
    ListingIssue,
    ListingStatus,
    SubmissionOutcome,
)

log = structlog.get_logger(__name__)

FEEDS_BASE = "/feeds/2021-06-30"
JSON_LISTINGS_FEED = "JSON_LISTINGS_FEED"
FEED_CONTENT_TYPE = "application/json; charset=UTF-8"

TERMINAL_STATUSES = {"DONE", "CANCELLED", "FATAL"}


class FeedError(RuntimeError):
    """The feed itself failed, as opposed to individual messages inside it failing."""


@dataclass
class FeedResult:
    feed_id: str
    processing_status: str
    outcomes: list[SubmissionOutcome] = field(default_factory=list)
    summary: dict[str, int] = field(default_factory=dict)
    raw_report: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.processing_status == "DONE" and self.summary.get("messagesWithError", 0) == 0


class FeedsClient:
    """Bulk listing submission. Same safety gates as the per-SKU path."""

    def __init__(self, client: SpApiClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    # -- document upload --

    def _create_document(self) -> tuple[str, str]:
        payload = self._client.request(
            "POST", f"{FEEDS_BASE}/documents", operation="createFeedDocument",
            json_body={"contentType": FEED_CONTENT_TYPE},
        ).json
        return str(payload["feedDocumentId"]), str(payload["url"])

    @staticmethod
    def _upload(url: str, body: bytes) -> None:
        # Presigned URL: send exactly the Content-Type that was declared to createFeedDocument,
        # and no Authorization header. A mismatch on either produces a 403 from S3.
        with httpx.Client(timeout=120.0) as raw:
            resp = raw.put(url, content=body, headers={"Content-Type": FEED_CONTENT_TYPE})
            resp.raise_for_status()

    # -- feed lifecycle --

    def submit(
        self,
        listings: Iterable[BuiltListing],
        marketplaces: list[Marketplace],
        *,
        confirm: bool = False,
        poll: bool = True,
        poll_timeout_s: float = 900.0,
    ) -> FeedResult:
        """Build, upload, and submit a JSON_LISTINGS_FEED.

        There is no validation-preview equivalent for feeds: submitting one writes. Both safety
        gates therefore apply unconditionally.
        """
        from anzorlist.channels.amazon.listings import LiveWriteBlocked

        listings = list(listings)
        if not (confirm and self._settings.allow_live):
            raise LiveWriteBlocked(
                f"a feed of {len(listings)} listing(s)",
                confirm=confirm, allow_live=self._settings.allow_live,
            )
        if not listings:
            raise FeedError("no listings to submit")

        document = build_feed_document(listings, self._client.seller_id)
        body = json.dumps(document, ensure_ascii=False).encode("utf-8")
        log.info("feeds.building", messages=len(document["messages"]), bytes=len(body))

        doc_id, url = self._create_document()
        self._upload(url, body)

        feed = self._client.request(
            "POST", f"{FEEDS_BASE}/feeds", operation="createFeed",
            json_body={
                "feedType": JSON_LISTINGS_FEED,
                "marketplaceIds": [m.marketplace_id for m in marketplaces],
                "inputFeedDocumentId": doc_id,
            },
        ).json
        feed_id = str(feed["feedId"])
        log.warning("feeds.submitted", feed_id=feed_id, messages=len(document["messages"]),
                    marketplaces=[m.code for m in marketplaces])

        if not poll:
            return FeedResult(feed_id=feed_id, processing_status="IN_QUEUE")
        return self.wait(feed_id, marketplaces[0], timeout_s=poll_timeout_s)

    def wait(
        self, feed_id: str, marketplace: Marketplace, *, timeout_s: float = 900.0,
        interval_s: float = 20.0,
    ) -> FeedResult:
        """Poll until the feed reaches a terminal state, then parse its report."""
        deadline = time.monotonic() + timeout_s
        status = "IN_QUEUE"
        result_doc_id: str | None = None

        while time.monotonic() < deadline:
            info = self._client.get(f"{FEEDS_BASE}/feeds/{feed_id}", operation="getFeed")
            status = str(info.get("processingStatus", "UNKNOWN"))
            result_doc_id = info.get("resultFeedDocumentId")
            log.info("feeds.polling", feed_id=feed_id, status=status)
            if status in TERMINAL_STATUSES:
                break
            time.sleep(interval_s)
        else:
            raise FeedError(
                f"Feed {feed_id} did not finish within {timeout_s:.0f}s (last status {status}). "
                f"It is still processing — check with `anzorlist amazon feed-status {feed_id}`."
            )

        if status != "DONE" or not result_doc_id:
            return FeedResult(feed_id=feed_id, processing_status=status)

        report = self._download_report(result_doc_id)
        outcomes = _outcomes_from_report(report, marketplace)
        return FeedResult(
            feed_id=feed_id,
            processing_status=status,
            outcomes=outcomes,
            summary=report.get("summary", {}) if isinstance(report, dict) else {},
            raw_report=report if isinstance(report, dict) else None,
        )

    def _download_report(self, document_id: str) -> dict[str, Any]:
        info = self._client.get(f"{FEEDS_BASE}/documents/{document_id}",
                                operation="getFeedDocument")
        url = str(info["url"])
        compression = str(info.get("compressionAlgorithm", "") or "")
        with httpx.Client(timeout=120.0) as raw:
            resp = raw.get(url)
            resp.raise_for_status()
            content = resp.content
        if compression.upper() == "GZIP":
            content = gzip.GzipFile(fileobj=io.BytesIO(content)).read()
        try:
            return json.loads(content.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise FeedError(f"could not parse feed report {document_id}: {exc}") from exc


def build_feed_document(listings: list[BuiltListing], seller_id: str) -> dict[str, Any]:
    """Assemble the JSON_LISTINGS_FEED body.

    Message IDs must be unique within a feed and are what the result report keys errors on, so
    the mapping from message ID back to SKU is kept deterministic (1-based index order).
    """
    messages: list[dict[str, Any]] = []
    for i, listing in enumerate(listings, start=1):
        messages.append({
            "messageId": i,
            "sku": listing.sku,
            "operationType": "UPDATE",
            "productType": listing.product_type,
            "requirements": listing.requirements,
            "attributes": listing.attributes,
        })
    return {
        "header": {
            "sellerId": seller_id,
            "version": "2.0",
            "issueLocale": "en_US",
        },
        "messages": messages,
    }


def _outcomes_from_report(report: Any, marketplace: Marketplace) -> list[SubmissionOutcome]:
    """Turn a processing report into per-SKU outcomes.

    The report lists only messages that had issues; anything absent processed cleanly. That
    inversion is easy to get backwards, so accepted SKUs are reconstructed explicitly by the
    caller from the submitted set rather than inferred here.
    """
    if not isinstance(report, dict):
        return []
    now = datetime.now(timezone.utc)
    outcomes: list[SubmissionOutcome] = []
    for result in report.get("issues", []) or []:
        if not isinstance(result, dict):
            continue
        sku = str(result.get("sku", "") or result.get("messageId", ""))
        severity = _severity(result.get("severity"))
        outcomes.append(SubmissionOutcome(
            sku=sku,
            marketplace_id=marketplace.marketplace_id,
            marketplace_code=marketplace.code,
            mode="SUBMIT",
            status=(ListingStatus.REJECTED if severity is IssueSeverity.ERROR
                    else ListingStatus.SUBMITTED),
            issues=[ListingIssue(
                code=str(result.get("code", "FeedIssue")),
                message=str(result.get("message", "")),
                severity=severity,
                attribute_names=[str(a) for a in result.get("attributeNames", []) or []],
                source="amazon",
            )],
            submitted_at=now,
        ))
    return outcomes


def _severity(raw: object) -> IssueSeverity:
    try:
        return IssueSeverity(str(raw).upper())
    except ValueError:
        return IssueSeverity.ERROR
