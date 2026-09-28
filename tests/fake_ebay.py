"""A fake eBay: OAuth and the Inventory API endpoints the publisher calls, in eBay's shapes."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

INV = "/sell/inventory/v1"


@dataclass
class FakeEbay:
    reject_publish: set[str] = field(default_factory=set)  # group keys or SKUs
    items: dict[str, dict[str, Any]] = field(default_factory=dict)
    groups: dict[str, dict[str, Any]] = field(default_factory=dict)
    offers: dict[str, dict[str, Any]] = field(default_factory=dict)  # offerId -> body
    published: list[str] = field(default_factory=list)  # group keys / skus
    calls: list[tuple[str, str]] = field(default_factory=list)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _offer_for(self, sku: str) -> str | None:
        return next((oid for oid, o in self.offers.items() if o["sku"] == sku), None)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        if path == "/identity/v1/oauth2/token":
            assert request.headers["authorization"].startswith("Basic ")
            return httpx.Response(200, json={"access_token": "v^1.1#t", "expires_in": 7200})
        assert request.headers.get("authorization", "").startswith("Bearer ")
        body = json.loads(request.content) if request.content else None

        if path == f"{INV}/bulk_create_or_replace_inventory_item":
            for r in body["requests"]:
                assert r["product"]["imageUrls"] and len(r["product"]["title"]) <= 80
                self.items[r["sku"]] = r
            return httpx.Response(
                207,
                json={
                    "responses": [{"statusCode": 200, "sku": r["sku"]} for r in body["requests"]]
                },
            )
        if path.startswith(f"{INV}/inventory_item_group/") and request.method == "PUT":
            key = path.rsplit("/", 1)[1]
            missing = [s for s in body["variantSKUs"] if s not in self.items]
            assert not missing, f"group names unknown items {missing}"
            self.groups[key] = body
            return httpx.Response(204)
        if path == f"{INV}/offer" and request.method == "GET":
            oid = self._offer_for(request.url.params["sku"])
            if oid is None:
                return httpx.Response(
                    404,
                    json={
                        "errors": [{"errorId": 25713, "message": "This Offer is not available."}]
                    },
                )
            return httpx.Response(200, json={"offers": [{"offerId": oid, **self.offers[oid]}]})
        if path == f"{INV}/bulk_create_offer":
            responses = []
            for offer in body["requests"]:
                assert self._offer_for(offer["sku"]) is None, "duplicate offer (eBay error 25002)"
                oid = f"OFF{len(self.offers) + 1}"
                self.offers[oid] = offer
                responses.append({"statusCode": 200, "sku": offer["sku"], "offerId": oid})
            return httpx.Response(207, json={"responses": responses})
        if path.startswith(f"{INV}/offer/") and request.method == "PUT":
            self.offers[path.rsplit("/", 1)[1]] = body
            return httpx.Response(204)
        if path == f"{INV}/offer/publish_by_inventory_item_group":
            return self._publish(body["inventoryItemGroupKey"])
        if path.startswith(f"{INV}/offer/") and path.endswith("/publish"):
            oid = path.split("/")[-2]
            return self._publish(self.offers[oid]["sku"])
        return httpx.Response(404, json={"errors": [{"errorId": 0, "message": path}]})

    def _publish(self, key: str) -> httpx.Response:
        if key in self.reject_publish:
            return httpx.Response(
                400,
                json={"errors": [{"errorId": 25002, "message": "Item specific Metal is missing."}]},
            )
        self.published.append(key)
        return httpx.Response(200, json={"listingId": f"11000{len(self.published)}"})
