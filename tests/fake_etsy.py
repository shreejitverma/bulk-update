"""A fake Etsy: OAuth with rotating refresh tokens, taxonomy, listings, images, inventory."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass
class FakeEtsy:
    fail_inventory_once: bool = False
    listings: dict[int, dict[str, Any]] = field(default_factory=dict)
    # listing_id -> {listing_image_id: (file name, content type)}: the photos live on each listing
    images: dict[int, dict[int, tuple[str, str]]] = field(default_factory=dict)
    uploads: int = 0
    inventory: dict[int, dict[str, Any]] = field(default_factory=dict)
    refresh_tokens_seen: list[str] = field(default_factory=list)
    valid_refresh: str = "etsy-refresh-0"
    api_keys: set[str] = field(default_factory=set)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/v3/application/"):
            self.api_keys.add(request.headers.get("x-api-key", ""))
        if path == "/v3/public/oauth/token":
            form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
            self.refresh_tokens_seen.append(form["refresh_token"])
            if form["refresh_token"] != self.valid_refresh:
                return httpx.Response(400, json={"error": "invalid_grant"})
            n = len(self.refresh_tokens_seen)
            self.valid_refresh = f"etsy-refresh-{n}"  # rotation: the old one is now dead
            return httpx.Response(
                200,
                json={
                    "access_token": f"123.access{n}",
                    "refresh_token": self.valid_refresh,
                    "expires_in": 3600,
                },
            )
        if path == "/v3/application/seller-taxonomy/nodes":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": 68887420,
                            "name": "Jewelry",
                            "children": [
                                {"id": 1250, "name": "Rings"},
                                {"id": 1219, "name": "Earrings"},
                                {"id": 1223, "name": "Necklaces"},
                                {"id": 1210, "name": "Bracelets"},
                            ],
                        },
                    ]
                },
            )
        assert request.headers["authorization"].startswith("Bearer 123.access")
        parts = path.strip("/").split("/")
        if request.method == "POST" and path.endswith("/listings"):
            body = json.loads(request.content)
            for key in (
                "title",
                "price",
                "taxonomy_id",
                "who_made",
                "when_made",
                "shipping_profile_id",
                "return_policy_id",
                "quantity",
            ):
                assert key in body, key
            listing_id = 900 + len(self.listings)
            self.listings[listing_id] = {**body, "state": "draft"}
            return httpx.Response(201, json={"listing_id": listing_id})
        if request.method == "PATCH" and "listings" in parts:
            listing_id = int(parts[-1])
            self.listings[listing_id].update(json.loads(request.content))
            return httpx.Response(200, json={"listing_id": listing_id})
        if request.method == "POST" and path.endswith("/images"):
            listing_id = int(parts[-2])
            part = re.search(
                rb'name="image"; filename="([^"]+)"\r\nContent-Type: (\S+)', request.content
            )
            assert part, "no image part"
            self.uploads += 1
            image_id = 5000 + self.uploads
            photo = (part[1].decode(), part[2].decode())
            self.images.setdefault(listing_id, {})[image_id] = photo
            return httpx.Response(201, json={"listing_image_id": image_id})
        if request.method == "DELETE" and "images" in parts:
            live = self.images.get(int(parts[-3]), {})
            if live.pop(int(parts[-1]), None) is None:
                return httpx.Response(404, json={"error": "no such image"})
            return httpx.Response(204)
        if request.method == "PUT" and path.endswith("/inventory"):
            if self.fail_inventory_once:
                self.fail_inventory_once = False
                return httpx.Response(400, json={"error": "price_on_property mismatch"})
            self.inventory[int(parts[-2])] = json.loads(request.content)
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"error": path})
