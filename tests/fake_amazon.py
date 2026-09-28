"""A fake Amazon: LWA, Listings Items, Feeds, and the presigned S3 URLs, behind one transport.

It implements the parts of SP-API the tool calls, with the response shapes Amazon documents, and
records every request so tests can assert on exactly what would have been sent. Nothing here
touches the network.
"""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

import httpx

LISTINGS_PREFIX = "/listings/2021-08-01/items/"
FEEDS_PREFIX = "/feeds/2021-06-30/"


@dataclass
class ListingCall:
    sku: str
    mode: str  # "VALIDATION_PREVIEW" or "SUBMIT"
    body: dict[str, Any]


@dataclass
class FakeAmazon:
    # SKUs Amazon rejects, by mode. A SKU in ``reject_submit`` passes preview but fails the write.
    reject_preview: set[str] = field(default_factory=set)
    reject_submit: set[str] = field(default_factory=set)
    # SKUs whose feed message the processing report marks as an ERROR.
    reject_in_feed: set[str] = field(default_factory=set)
    reject_delete: set[str] = field(default_factory=set)
    feed_status: str = "DONE"

    listing_calls: list[ListingCall] = field(default_factory=list)
    feed_documents: dict[str, dict[str, Any]] = field(default_factory=dict)
    feeds: dict[str, dict[str, Any]] = field(default_factory=dict)
    token_requests: int = 0
    deleted: list[str] = field(default_factory=list)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def writes(self) -> list[ListingCall]:
        return [c for c in self.listing_calls if c.mode == "SUBMIT"]

    def previews(self) -> list[ListingCall]:
        return [c for c in self.listing_calls if c.mode == "VALIDATION_PREVIEW"]

    # ------------------------------------------------------------------ routing

    def _handle(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.host == "api.amazon.com" and url.path == "/auth/o2/token":
            self.token_requests += 1
            return httpx.Response(200, json={"access_token": "Atza|fake", "expires_in": 3600})
        if url.host == "s3.test":
            return self._s3(request)
        if url.host == "sellingpartnerapi-na.amazon.com":
            if not request.headers.get("x-amz-access-token"):
                return httpx.Response(403, json={"errors": [{"code": "Unauthorized"}]})
            if url.path.startswith(LISTINGS_PREFIX):
                return self._listings(request)
            if url.path.startswith(FEEDS_PREFIX):
                return self._feeds(request)
        return httpx.Response(404, json={"errors": [{"code": "NotFound", "message": str(url)}]})

    def _listings(self, request: httpx.Request) -> httpx.Response:
        _seller, encoded_sku = request.url.path[len(LISTINGS_PREFIX) :].split("/", 1)
        sku = unquote(encoded_sku)
        if request.method == "DELETE":
            if sku in self.reject_delete:
                return httpx.Response(
                    200, json={"sku": sku, "status": "INVALID", "submissionId": "del", "issues": []}
                )
            self.deleted.append(sku)
            return httpx.Response(
                200, json={"sku": sku, "status": "ACCEPTED", "submissionId": "del", "issues": []}
            )
        mode = request.url.params.get("mode", "SUBMIT")
        body = json.loads(request.content)
        self.listing_calls.append(ListingCall(sku=sku, mode=mode, body=body))
        rejects = self.reject_preview if mode == "VALIDATION_PREVIEW" else self.reject_submit
        rejected = sku in rejects
        if rejected:
            return httpx.Response(
                200,
                json={
                    "sku": sku,
                    "status": "INVALID",
                    "submissionId": f"sub-{len(self.listing_calls)}",
                    "issues": [
                        {
                            "code": "90220",
                            "message": "'metal_type' is required but not supplied.",
                            "severity": "ERROR",
                            "attributeNames": ["metal_type"],
                        }
                    ],
                },
            )
        # Amazon answers a clean VALIDATION_PREVIEW with VALID and a clean write with ACCEPTED.
        return httpx.Response(
            200,
            json={
                "sku": sku,
                "status": "VALID" if mode == "VALIDATION_PREVIEW" else "ACCEPTED",
                "submissionId": f"sub-{len(self.listing_calls)}",
                "issues": [],
            },
        )

    def _feeds(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path[len(FEEDS_PREFIX) :]
        if request.method == "POST" and path == "documents":
            doc_id = f"doc-{len(self.feed_documents) + 1}"
            self.feed_documents[doc_id] = {}
            return httpx.Response(
                201, json={"feedDocumentId": doc_id, "url": f"https://s3.test/upload/{doc_id}"}
            )
        if request.method == "POST" and path == "feeds":
            body = json.loads(request.content)
            feed_id = f"5000{len(self.feeds) + 1}"
            self.feeds[feed_id] = {
                "request": body,
                "document": self.feed_documents[body["inputFeedDocumentId"]],
            }
            return httpx.Response(202, json={"feedId": feed_id})
        if request.method == "GET" and path.startswith("feeds/"):
            feed_id = path.split("/", 1)[1]
            info: dict[str, Any] = {"feedId": feed_id, "processingStatus": self.feed_status}
            if self.feed_status == "DONE":
                info["resultFeedDocumentId"] = f"report-{feed_id}"
            return httpx.Response(200, json=info)
        if request.method == "GET" and path.startswith("documents/report-"):
            report_id = path.split("/", 1)[1]
            return httpx.Response(
                200,
                json={
                    "feedDocumentId": report_id,
                    "url": f"https://s3.test/report/{report_id}",
                    "compressionAlgorithm": "GZIP",
                },
            )
        return httpx.Response(404, json={"errors": [{"code": "NotFound", "message": path}]})

    def _s3(self, request: httpx.Request) -> httpx.Response:
        kind, key = request.url.path.strip("/").split("/", 1)
        if kind == "upload" and request.method == "PUT":
            if "authorization" in request.headers or "x-amz-access-token" in request.headers:
                return httpx.Response(403, text="presigned URL must not carry auth")
            self.feed_documents[key] = json.loads(request.content)
            return httpx.Response(200)
        if kind == "report" and request.method == "GET":
            feed_id = key.removeprefix("report-")
            return httpx.Response(200, content=gzip.compress(self._report(feed_id)))
        return httpx.Response(404)

    def _report(self, feed_id: str) -> bytes:
        """A processing report in Amazon's documented shape: issues keyed by messageId only."""
        messages = self.feeds[feed_id]["document"]["messages"]
        issues = [
            {
                "messageId": m["messageId"],
                "code": "8541",
                "severity": "ERROR",
                "message": "not approved to list in this category",
            }
            for m in messages
            if m["sku"] in self.reject_in_feed
        ]
        report = {
            "header": {"sellerId": "A1SELLER", "version": "2.0", "feedId": feed_id},
            "issues": issues,
            "summary": {
                "errors": len(issues),
                "warnings": 0,
                "messagesProcessed": len(messages),
                "messagesAccepted": len(messages) - len(issues),
                "messagesInvalid": len(issues),
            },
        }
        return json.dumps(report).encode()
