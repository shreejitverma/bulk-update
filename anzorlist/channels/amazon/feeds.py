"""Feeds API (2021-06-30) - the bulk path, for catalogs the per-SKU API would take too long on.

Listings Items is the default because it validates synchronously. But it is capped near 5
requests/second, so a 3,000-SKU catalog with size variations is roughly 20,000 writes: over an
hour of wall clock. ``JSON_LISTINGS_FEED`` submits the same message objects in one document.

The trade-off is honest and worth stating: a feed is asynchronous and opaque until it finishes,
and there is no preview mode - creating a feed writes. So the caller validates every message
locally against the product-type schema *and* spot-checks a sample through VALIDATION_PREVIEW
before a feed is ever built (see :mod:`anzorlist.channels.amazon.submit`).

Four steps, in order:

1. ``createFeedDocument`` - returns an upload URL.
2. PUT the document to that URL (no auth header - it is presigned; adding one makes S3 reject it).
3. ``createFeed`` - hand Amazon the document ID.
4. Poll ``getFeed``, then download and parse the result document, which may be gzipped.

**Reconciliation.** The processing report lists only messages that had issues, keyed by
``messageId`` - never by SKU - and a message with no ERROR issue was accepted. So the mapping
from message ID to SKU is written to disk as a :class:`FeedManifest` the moment Amazon returns a
feed ID, before anything else can fail. Without it a report cannot be attributed, and a feed whose
polling times out could never be reconciled into the ledger afterwards. If ``createFeed`` itself
fails ambiguously, the caller records the messages as failed: every message is an ``UPDATE``
upsert, so resending them is safe even if the feed did go through.
"""

from __future__ import annotations

import gzip
import io
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
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

# Chunking limits. Amazon rejects oversized feed documents outright; staying well under the
# documented ceilings keeps one bad chunk from taking the whole catalog with it.
MAX_MESSAGES_PER_FEED = 5_000
MAX_FEED_BYTES = 8 * 1024 * 1024


class FeedError(RuntimeError):
    """The feed itself failed, as opposed to individual messages inside it failing."""


class FeedPending(FeedError):
    """The feed has not finished processing yet. Nothing is wrong; check again later."""


@dataclass
class FeedMessage:
    message_id: int
    sku: str
    payload_hash: str


@dataclass
class FeedManifest:
    """What was sent in one feed, persisted as soon as Amazon assigns the feed ID."""

    feed_id: str
    marketplace_code: str
    marketplace_id: str
    run_id: str
    created_at: str
    messages: list[FeedMessage] = field(default_factory=list)
    reconciled: bool = False

    def path(self, feeds_dir: Path) -> Path:
        return feeds_dir / f"{self.feed_id}.json"

    def save(self, feeds_dir: Path) -> Path:
        feeds_dir.mkdir(parents=True, exist_ok=True)
        path = self.path(feeds_dir)
        path.write_text(
            json.dumps(
                {
                    "feedId": self.feed_id,
                    "marketplaceCode": self.marketplace_code,
                    "marketplaceId": self.marketplace_id,
                    "runId": self.run_id,
                    "createdAt": self.created_at,
                    "reconciled": self.reconciled,
                    "messages": [
                        {"messageId": m.message_id, "sku": m.sku, "payloadHash": m.payload_hash}
                        for m in self.messages
                    ],
                },
                indent=2,
            )
            + "\n"
        )
        return path

    @classmethod
    def unreconciled(cls, feeds_dir: Path) -> list[FeedManifest]:
        """Every saved feed whose result has not been recorded in the ledger, oldest first."""
        if not feeds_dir.exists():
            return []
        manifests = [cls.load(feeds_dir, p.stem) for p in feeds_dir.glob("*.json")]
        return sorted((m for m in manifests if not m.reconciled), key=lambda m: m.created_at)

    @classmethod
    def load(cls, feeds_dir: Path, feed_id: str) -> FeedManifest:
        path = feeds_dir / f"{feed_id}.json"
        if not path.exists():
            raise FeedError(
                f"No manifest for feed {feed_id} at {path}. Only feeds created by this tool, "
                f"from this data directory, can be reconciled."
            )
        data = json.loads(path.read_text())
        return cls(
            feed_id=str(data["feedId"]),
            marketplace_code=str(data["marketplaceCode"]),
            marketplace_id=str(data["marketplaceId"]),
            run_id=str(data["runId"]),
            created_at=str(data["createdAt"]),
            reconciled=bool(data.get("reconciled", False)),
            messages=[
                FeedMessage(int(m["messageId"]), str(m["sku"]), str(m["payloadHash"]))
                for m in data["messages"]
            ],
        )


@dataclass
class FeedResult:
    feed_id: str
    processing_status: str
    summary: dict[str, int] = field(default_factory=dict)
    report: dict[str, Any] | None = None


class FeedsClient:
    """Bulk listing submission. Same safety gates as the per-SKU path."""

    def __init__(
        self,
        client: SpApiClient,
        settings: Settings,
        *,
        raw_http: httpx.Client | None = None,
    ) -> None:
        self._client = client
        self._settings = settings
        # Presigned S3 URLs take no SP-API auth, so they go through a plain client.
        self._owns_raw = raw_http is None
        self._raw = raw_http or httpx.Client(timeout=120.0)

    def close(self) -> None:
        if self._owns_raw:
            self._raw.close()

    # -- creation --

    def create(
        self,
        listings: list[BuiltListing],
        marketplace: Marketplace,
        *,
        run_id: str,
        feeds_dir: Path,
        confirm: bool = False,
    ) -> FeedManifest:
        """Upload and create one JSON_LISTINGS_FEED for one marketplace. This writes.

        The manifest is saved as soon as Amazon returns a feed ID, before anything else can fail.
        """
        from anzorlist.channels.amazon.listings import LiveWriteBlocked

        if not (confirm and self._settings.allow_live):
            raise LiveWriteBlocked(
                f"a feed of {len(listings)} listing(s)",
                confirm=confirm,
                allow_live=self._settings.allow_live,
            )
        if not listings:
            raise FeedError("no listings to submit")
        foreign = [x.sku for x in listings if x.marketplace_id != marketplace.marketplace_id]
        if foreign:
            raise FeedError(
                f"a feed carries one marketplace; {len(foreign)} listing(s) belong elsewhere "
                f"(e.g. {foreign[0]})"
            )

        document = build_feed_document(listings, self._client.seller_id, marketplace)
        body = json.dumps(document, ensure_ascii=False).encode("utf-8")
        if len(body) > MAX_FEED_BYTES:
            raise FeedError(
                f"feed document is {len(body)} bytes, over the {MAX_FEED_BYTES} byte limit; "
                f"split it with chunk_listings()"
            )

        doc = self._client.request(
            "POST",
            f"{FEEDS_BASE}/documents",
            operation="createFeedDocument",
            json_body={"contentType": FEED_CONTENT_TYPE},
        ).json
        doc_id, url = str(doc["feedDocumentId"]), str(doc["url"])
        # Presigned URL: send exactly the Content-Type declared to createFeedDocument and no
        # Authorization header. A mismatch on either produces a 403 from S3.
        upload = self._raw.put(url, content=body, headers={"Content-Type": FEED_CONTENT_TYPE})
        if upload.status_code >= 300:
            raise FeedError(
                f"uploading the feed document failed with {upload.status_code}: {upload.text[:300]}"
            )

        feed = self._client.request(
            "POST",
            f"{FEEDS_BASE}/feeds",
            operation="createFeed",
            json_body={
                "feedType": JSON_LISTINGS_FEED,
                "marketplaceIds": [marketplace.marketplace_id],
                "inputFeedDocumentId": doc_id,
            },
        ).json
        manifest = FeedManifest(
            feed_id=str(feed["feedId"]),
            marketplace_code=marketplace.code,
            marketplace_id=marketplace.marketplace_id,
            run_id=run_id,
            created_at=datetime.now(timezone.utc).isoformat(),
            messages=[
                FeedMessage(int(m["messageId"]), str(m["sku"]), x.payload_hash)
                for m, x in zip(document["messages"], listings, strict=True)
            ],
        )
        manifest.save(feeds_dir)
        log.warning(
            "feeds.created",
            feed_id=manifest.feed_id,
            messages=len(listings),
            marketplace=marketplace.code,
        )
        return manifest

    # -- status --

    def check(self, feed_id: str) -> FeedResult:
        """One status read. Raises :class:`FeedPending` while Amazon is still processing."""
        info = self._client.get(f"{FEEDS_BASE}/feeds/{feed_id}", operation="getFeed")
        status = str((info or {}).get("processingStatus", "UNKNOWN"))
        if status not in TERMINAL_STATUSES:
            raise FeedPending(f"feed {feed_id} is {status}")
        result_doc_id = (info or {}).get("resultFeedDocumentId")
        if not result_doc_id:
            return FeedResult(feed_id=feed_id, processing_status=status)
        report = self._download_report(str(result_doc_id))
        raw_summary = report.get("summary")
        summary = (
            {k: int(v) for k, v in raw_summary.items() if isinstance(v, int | float)}
            if isinstance(raw_summary, dict)
            else {}
        )
        return FeedResult(
            feed_id=feed_id,
            processing_status=status,
            summary=summary,
            report=report,
        )

    def wait(
        self,
        feed_id: str,
        *,
        timeout_s: float = 1800.0,
        interval_s: float = 30.0,
    ) -> FeedResult:
        """Poll until the feed reaches a terminal state. Raises :class:`FeedPending` on timeout."""
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                return self.check(feed_id)
            except FeedPending:
                if time.monotonic() >= deadline:
                    raise FeedPending(
                        f"feed {feed_id} did not finish within {timeout_s:.0f}s. It is still "
                        f"processing; run `anzorlist amazon feed-status {feed_id}` later."
                    ) from None
                log.info("feeds.polling", feed_id=feed_id)
                time.sleep(interval_s)

    def _download_report(self, document_id: str) -> dict[str, Any]:
        info = self._client.get(
            f"{FEEDS_BASE}/documents/{document_id}", operation="getFeedDocument"
        )
        url = str(info["url"])
        compression = str(info.get("compressionAlgorithm", "") or "")
        resp = self._raw.get(url)
        if resp.status_code >= 300:
            raise FeedError(f"downloading feed report {document_id} failed: {resp.status_code}")
        content = resp.content
        if compression.upper() == "GZIP":
            content = gzip.GzipFile(fileobj=io.BytesIO(content)).read()
        try:
            report = json.loads(content.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise FeedError(f"could not parse feed report {document_id}: {exc}") from exc
        if not isinstance(report, dict):
            raise FeedError(f"feed report {document_id} is not a JSON object")
        return report


def build_feed_document(
    listings: list[BuiltListing], seller_id: str, marketplace: Marketplace
) -> dict[str, Any]:
    """Assemble the JSON_LISTINGS_FEED body.

    Message IDs are 1-based positions; the manifest maps them back to SKUs.
    """
    messages: list[dict[str, Any]] = [
        {
            "messageId": i,
            "sku": listing.sku,
            "operationType": "UPDATE",
            "productType": listing.product_type,
            "requirements": listing.requirements,
            "attributes": listing.attributes,
        }
        for i, listing in enumerate(listings, start=1)
    ]
    return {
        "header": {"sellerId": seller_id, "version": "2.0", "issueLocale": marketplace.locale},
        "messages": messages,
    }


def chunk_listings(listings: list[BuiltListing]) -> list[list[BuiltListing]]:
    """Split into feed-sized chunks by message count and by serialized size."""
    chunks: list[list[BuiltListing]] = []
    current: list[BuiltListing] = []
    size = 0
    for listing in listings:
        cost = len(json.dumps(listing.attributes, ensure_ascii=False).encode("utf-8")) + 256
        if current and (len(current) >= MAX_MESSAGES_PER_FEED or size + cost > MAX_FEED_BYTES):
            chunks.append(current)
            current, size = [], 0
        current.append(listing)
        size += cost
    if current:
        chunks.append(current)
    return chunks


def reconcile(result: FeedResult, manifest: FeedManifest) -> list[SubmissionOutcome]:
    """One outcome per submitted message.

    A feed that did not finish (``FATAL``/``CANCELLED``) fails every message. Otherwise a message
    is rejected when the report carries an ERROR for its ``messageId`` and accepted when it does
    not; warnings ride along on accepted messages.
    """
    now = datetime.now(timezone.utc)
    by_id: dict[int, list[ListingIssue]] = {}
    unattributed: list[ListingIssue] = []
    for raw in (result.report or {}).get("issues", []) or []:
        if not isinstance(raw, dict):
            continue
        issue = ListingIssue(
            code=str(raw.get("code", "FeedIssue")),
            message=str(raw.get("message", "")),
            severity=_severity(raw.get("severity")),
            attribute_names=[str(a) for a in raw.get("attributeNames", []) or []],
            source="amazon",
        )
        message_id = raw.get("messageId")
        if isinstance(message_id, int):
            by_id.setdefault(message_id, []).append(issue)
        else:
            unattributed.append(issue)

    feed_failed = result.processing_status != "DONE" or result.report is None
    rejects_all = feed_failed or any(i.blocking for i in unattributed)
    mismatch = "" if rejects_all else _summary_mismatch(result, manifest, by_id)
    outcomes: list[SubmissionOutcome] = []
    for message in manifest.messages:
        issues = list(by_id.get(message.message_id, []))
        if feed_failed:
            issues.append(
                ListingIssue(
                    code=f"Feed{result.processing_status.title()}",
                    message=f"feed {manifest.feed_id} ended {result.processing_status} "
                    f"without a processing report; nothing in it can be assumed applied",
                    source="amazon",
                )
            )
        # A feed-level error with no messageId applies to every message in the feed.
        issues.extend(unattributed)
        rejected = any(i.blocking for i in issues)
        unknown = bool(mismatch) and not rejected
        final = ListingStatus.REJECTED if rejected else ListingStatus.SUBMITTED
        if unknown:
            issues.append(
                ListingIssue(code="FeedReportMismatch", message=mismatch, source="amazon")
            )
        outcomes.append(
            SubmissionOutcome(
                sku=message.sku,
                marketplace_id=manifest.marketplace_id,
                marketplace_code=manifest.marketplace_code,
                mode="SUBMIT",
                status=ListingStatus.ERROR if unknown else final,
                submission_id=manifest.feed_id,
                issues=issues,
                submitted_at=now,
                payload_hash=message.payload_hash,
            )
        )
    return outcomes


def _summary_mismatch(
    result: FeedResult, manifest: FeedManifest, by_id: dict[int, list[ListingIssue]]
) -> str:
    """Why the report's own counts contradict its issue list, or "" when they agree.

    A message absent from the issues is taken as accepted. That inference is only safe when the
    summary confirms it: every message processed, and exactly as many invalid as have errors.
    """
    processed = result.summary.get("messagesProcessed")
    invalid = result.summary.get("messagesInvalid")
    with_errors = sum(1 for issues in by_id.values() if any(i.blocking for i in issues))
    if processed is not None and processed != len(manifest.messages):
        return (
            f"the report says {processed} of {len(manifest.messages)} messages were processed; "
            f"this one's outcome is unknown and it will be resent"
        )
    if invalid is not None and invalid != with_errors:
        return (
            f"the report counts {invalid} invalid messages but attributes errors to "
            f"{with_errors}; this one's outcome is unknown and it will be resent"
        )
    return ""


def _severity(raw: object) -> IssueSeverity:
    try:
        return IssueSeverity(str(raw).upper())
    except ValueError:
        return IssueSeverity.ERROR  # unknown severity is treated as blocking, never ignored
