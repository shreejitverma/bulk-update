"""Parse a decoded product page into a :class:`Product`.

Anchoring strategy (never positional/nth-child):
  * schema.org microdata: ``itemprop="sku" | "name" | "price" | "image"``, and
    ``itemprop="additionalProperty"`` blocks for the Item Details key/values.
  * Legacy CSS classes as secondary anchors (``CPprodLPriceV``, ``CPinStock`` ...).
  * Label text for option groups (``DESidOptionNN``) and breadcrumbs ("Related Products").

The raw Item Details rows (:class:`SpecRow`) are the authoritative factual record used by the
anti-hallucination checker. :class:`Attributes` is a best-effort normalized view on top; any gap
is recorded as an :class:`ExtractionWarning`, never fabricated.
"""

from __future__ import annotations

import contextlib
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urljoin

import structlog
from bs4 import BeautifulSoup, Tag

from anzorlist.extract.client import FetchResult
from anzorlist.models.product import (
    SKU_PREFIX_FAMILY,
    AppraisalOption,
    Attributes,
    ExtractionWarning,
    Gemstone,
    MediaAsset,
    Money,
    Pricing,
    Product,
    ProductFamily,
    ProvenanceEntry,
    SpecRow,
    Variation,
)

log = structlog.get_logger(__name__)

_MONEY_RE = re.compile(r"\$\s*([\d,]+(?:\.\d{1,2})?)")
_PERCENT_RE = re.compile(r"\(?\s*(\d+(?:\.\d+)?)\s*%\)?")
_SIZE_NUM_RE = re.compile(r"(\d+(?:\.\d+)?)")
_GRAMS_RE = re.compile(r"([\d.]+)\s*gr", re.IGNORECASE)
_ID_PRODUCT_RE = re.compile(r"idProduct=(\d+)")
_ID_PRODUCT_INPUT_RE = re.compile(r'name=["\']?idProduct["\']?\s+value=["\']?(\d+)', re.IGNORECASE)
_CARAT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:ct|cts|cwt|carat)", re.IGNORECASE)

GEM_TYPES = ("Diamond", "Sapphire", "Emerald", "Ruby", "Pearl", "Topaz", "Amethyst", "Garnet")


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"))


def _parse_money(text: str | None) -> Money | None:
    if not text:
        return None
    m = _MONEY_RE.search(text)
    if not m:
        return None
    try:
        return Money(amount=_quantize(Decimal(m.group(1).replace(",", ""))))
    except InvalidOperation:
        return None


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


class ProductParser:
    def __init__(self, fetch: FetchResult) -> None:
        self.fetch = fetch
        self.sku = fetch.sku
        self.url = fetch.url
        self.html = fetch.html
        self.soup = BeautifulSoup(fetch.html, "lxml")
        self.now = datetime.now(timezone.utc)
        self.provenance: dict[str, ProvenanceEntry] = {}
        self.warnings: list[ExtractionWarning] = []

    # -- provenance / warning helpers --

    def _prov(self, key: str, anchor: str, snippet: str) -> str:
        self.provenance[key] = ProvenanceEntry(
            source_url=self.url,
            anchor=anchor,
            raw_snippet=_collapse(snippet)[:400],
            extracted_at=self.now,
        )
        return key

    def _warn(self, kind: str, field: str, detail: str) -> None:
        self.warnings.append(ExtractionWarning(kind=kind, field=field, detail=detail))  # type: ignore[arg-type]

    # -- individual fields --

    def _sku(self) -> str:
        el = self.soup.find(attrs={"itemprop": "sku"})
        if el and el.get_text(strip=True):
            val = el.get_text(strip=True)
            self._prov("sku", 'itemprop="sku"', val)
            return val
        self._warn("MissingField", "sku", "no itemprop=sku; falling back to requested SKU")
        return self.sku

    def _internal_id(self) -> int | None:
        m = _ID_PRODUCT_RE.search(self.html) or _ID_PRODUCT_INPUT_RE.search(self.html)
        if m:
            self._prov("internal_product_id", "emailToFriend idProduct", m.group(0))
            return int(m.group(1))
        self._warn("MissingField", "internal_product_id", "no idProduct in page")
        return None

    def _title_element(self) -> Tag | None:
        """The marketing title: itemprop=name that is NOT inside an additionalProperty block."""
        for el in self.soup.find_all(attrs={"itemprop": "name"}):
            if el.find_parent(attrs={"itemprop": "additionalProperty"}) is None:
                return el
        return self.soup.select_one("b.CPprodDescDet")

    def _marketing_title(self) -> str:
        el = self._title_element()
        if not el:
            self._warn("MissingField", "marketing_title_raw", "no product itemprop=name")
            return ""
        # Preserve literal <br> as newlines; keep the trademark/patent text verbatim.
        for br in el.find_all("br"):
            br.replace_with("\n")
        raw = el.get_text().strip()
        raw = re.sub(r"[ \t]+", " ", raw)
        self._prov("marketing_title_raw", 'itemprop="name"', raw)
        return raw

    def _short_name(self) -> str | None:
        img = self.soup.select_one("img.mainProductImage")
        alt = img.get("alt") if isinstance(img, Tag) else None
        if alt:
            alt = _collapse(str(alt))
            self._prov("short_name", "img.mainProductImage[alt]", alt)
            return alt
        self._warn("MissingField", "short_name", "no mainProductImage alt")
        return None

    def _description(self) -> str:
        el = self.soup.find(attrs={"itemprop": "description"})
        if not el:
            self._warn("MissingField", "long_description_raw", "no itemprop=description")
            return ""
        for br in el.find_all("br"):
            br.replace_with("\n")
        # Drop the embedded <video>/<img> tags' non-text; get_text ignores them anyway.
        text = el.get_text("\n")
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n", text).strip()
        self._prov("long_description_raw", 'itemprop="description"', text[:200])
        return text

    def _family(self, breadcrumbs: list[list[str]]) -> tuple[ProductFamily, bool]:
        prefix = self.sku[0].upper() if self.sku else ""
        inferred = SKU_PREFIX_FAMILY.get(prefix)
        crumb_text = " ".join(w.lower() for path in breadcrumbs for w in path)
        verified = False
        if inferred is not None:
            # Verify against breadcrumb taxonomy words (e.g. "Rings", "Earrings").
            token = inferred.value.lower().rstrip("s")
            verified = token in crumb_text
        if inferred is None:
            self._warn("FamilyMismatch", "family", f"unknown SKU prefix {prefix!r}")
            return ProductFamily.SET, False
        if not verified:
            self._warn(
                "FamilyMismatch",
                "family",
                f"prefix->{inferred.value} not confirmed in breadcrumbs",
            )
        self._prov("family", "SKU prefix + breadcrumb", f"{prefix}->{inferred.value}")
        return inferred, verified

    # -- pricing / stock --

    def _pricing(self) -> Pricing:
        def cls_text(cls: str) -> str | None:
            el = self.soup.find(class_=cls)
            return el.get_text(" ", strip=True) if el else None

        list_txt = cls_text("CPprodLPriceV")
        our_txt = cls_text("CPprodPriceV")
        save_txt = cls_text("CPprodSPriceV")

        list_price = _parse_money(list_txt)
        our_price = _parse_money(our_txt)
        if our_price is None:
            price_el = self.soup.find(attrs={"itemprop": "price"})
            if price_el:
                our_price = _parse_money("$" + price_el.get_text(strip=True))
        you_save = _parse_money(save_txt)
        pct = None
        if save_txt:
            pm = _PERCENT_RE.search(save_txt)
            if pm:
                pct = Decimal(pm.group(1))

        if list_txt:
            self._prov("pricing.list_price", "del.CPprodLPriceV", list_txt)
        if our_txt:
            self._prov("pricing.our_price", "b.CPprodPriceV", our_txt)
        if save_txt:
            self._prov("pricing.you_save", "span.CPprodSPriceV", save_txt)
        if our_price is None:
            self._warn("MissingField", "pricing.our_price", "no selling price found")
        return Pricing(
            list_price=list_price,
            our_price=our_price,
            you_save=you_save,
            you_save_percent=pct,
        )

    def _stock(self) -> tuple[bool | None, bool | None]:
        in_stock = self.soup.find(class_="CPinStock")
        free_ship = self.soup.find(class_="CPfreeShipMsg")
        in_stock_val = None
        if in_stock is not None:
            in_stock_val = "in stock" in in_stock.get_text(" ", strip=True).lower()
            self._prov("in_stock", "b.CPinStock", in_stock.get_text(" ", strip=True))
        free_val = None
        if free_ship is not None:
            free_val = "free" in free_ship.get_text(" ", strip=True).lower()
            self._prov("free_shipping", "b.CPfreeShipMsg", free_ship.get_text(" ", strip=True))
        return in_stock_val, free_val

    # -- item details (specs) + normalized attributes --

    def _specs(self) -> list[SpecRow]:
        rows: list[SpecRow] = []
        for block in self.soup.find_all(attrs={"itemprop": "additionalProperty"}):
            name_el = block.find(attrs={"itemprop": "name"})
            val_el = block.find(attrs={"itemprop": "value"})
            if not name_el or not val_el:
                continue
            for br in val_el.find_all("br"):
                br.replace_with(" \n ")
            label = _collapse(name_el.get_text(" ")).rstrip(":")
            value = _collapse(val_el.get_text(" "))
            if not label or not value:
                continue
            key = self._prov(
                f"specs[{label}]", 'itemprop="additionalProperty"', f"{label}: {value}"
            )
            rows.append(SpecRow(label=label, value=value, provenance_key=key))
        if not rows:
            self._warn("MissingField", "specs", "no additionalProperty rows")
        return rows

    def _attributes(self, specs: list[SpecRow], title: str) -> Attributes:
        by_label = {r.label.lower(): r for r in specs}

        metal_type = metal_purity = metal_color = None
        metal = next((r for r in specs if "metal" in r.label.lower()), None)
        if metal:
            v = metal.value
            pm = re.search(r"(\d+)\s*k", v, re.IGNORECASE)
            if pm:
                metal_purity = f"{pm.group(1)}k"
            colors = [c for c in ("Yellow", "White", "Rose") if re.search(c, v, re.IGNORECASE)]
            if len(colors) >= 2:
                metal_color = "Two-Tone (" + " & ".join(colors) + ")"
            elif colors:
                metal_color = colors[0]
            if re.search(r"platinum", v, re.IGNORECASE):
                metal_type = "Platinum"
            elif re.search(r"gold", v, re.IGNORECASE):
                metal_type = "Gold"

        gross_weight_g = None
        weight = next((r for r in specs if "weight" in r.label.lower()), None)
        if weight:
            gm = _GRAMS_RE.search(weight.value)
            if gm:
                with contextlib.suppress(InvalidOperation):
                    gross_weight_g = Decimal(gm.group(1))

        measurements: dict[str, str] = {}
        size_row = by_label.get("size")
        if size_row:
            measurements["raw"] = size_row.value

        gemstones = self._gemstones(specs, title)

        if metal_purity is None:
            self._warn(
                "MissingField", "attributes.metal_purity", "purity not parsed from Metal row"
            )
        if not gemstones:
            self._warn("MissingField", "attributes.gemstones", "no gemstone parsed (see raw specs)")

        return Attributes(
            metal_type=metal_type,
            metal_purity=metal_purity,
            metal_color=metal_color,
            gross_weight_g=gross_weight_g,
            measurements=measurements,
            gemstones=gemstones,
        )

    def _gemstones(self, specs: list[SpecRow], title: str) -> list[Gemstone]:
        """Best-effort. The authoritative record is the raw specs; this never invents a spec."""
        gem_specs = [
            r for r in specs if any(k in r.label.lower() for k in ("gem", "stone", "shape", "cut"))
        ]
        blob = " ".join(r.value for r in gem_specs)
        blob_key = gem_specs[0].provenance_key if gem_specs else "specs"
        title_blob = f"{title} {blob}"
        gems: list[Gemstone] = []
        for gtype in GEM_TYPES:
            if not re.search(rf"\b{gtype}", title_blob, re.IGNORECASE):
                continue
            # carat: nearest carat figure mentioned alongside this gem type, if any
            carat = None
            near = re.search(
                rf"(\d+(?:\.\d+)?)\s*(?:ct|cts|cwt|carat)[^.]*?{gtype}"
                rf"|{gtype}[^.]*?(\d+(?:\.\d+)?)\s*(?:ct|cts|cwt|carat)",
                title_blob,
                re.IGNORECASE,
            )
            if near:
                num = near.group(1) or near.group(2)
                try:
                    carat = Decimal(num)
                except (InvalidOperation, TypeError):
                    carat = None
            genuine = bool(re.search(r"genuine|natural", blob, re.IGNORECASE)) or None
            origin = None
            if re.search(r"\bnatural\b|\bgenuine\b|ceylon|sri lanka|origin", blob, re.IGNORECASE):
                origin = "natural"
            elif re.search(r"lab[- ]?grown|synthetic|created", blob, re.IGNORECASE):
                origin = "lab_grown"
            treatment = None
            if re.search(r"be\s*heated|heat\s*treat", blob, re.IGNORECASE):
                treatment = "heat treated"
            shape = None
            sm = re.search(r"(round|princess|oval|emerald cut|cushion|pear|marquise)", blob, re.I)
            if sm:
                shape = sm.group(1).title()
            gems.append(
                Gemstone(
                    type=gtype,
                    genuine=genuine,
                    origin=origin,  # type: ignore[arg-type]
                    treatment=treatment,
                    carat_weight=carat,
                    shape_cut=shape,
                    provenance_key=blob_key,
                )
            )
        return gems

    # -- option groups: sizes + appraisals --

    @staticmethod
    def _axis_for(des: str) -> str | None:
        d = des.lower()
        if "appraisal" in d:
            return None  # handled as appraisal, not a size variation
        if "finger size" in d or "ring size" in d or re.search(r"\bsize\b", d):
            return "ring_size"
        if "chain" in d and "length" in d:
            return "chain_length"
        if "bracelet" in d and "length" in d:
            return "bracelet_length"
        if "length" in d:
            return "length"
        return "option:" + re.sub(r"[^a-z0-9]+", "_", d).strip("_")

    def _option_groups(self) -> tuple[list[Variation], list[AppraisalOption]]:
        variations: list[Variation] = []
        appraisals: list[AppraisalOption] = []

        for des_input in self.soup.find_all("input", attrs={"name": re.compile(r"DESidOption\d+")}):
            name = des_input.get("name", "")
            gm = re.search(r"DESidOption(\d+)", name)
            if not gm:
                continue
            gid = gm.group(1)
            des = _collapse(str(des_input.get("value", "")))
            select = self.soup.find("select", attrs={"name": f"OPTidOption{gid}"})
            if not isinstance(select, Tag):
                continue
            is_appraisal = "appraisal" in des.lower()
            axis = self._axis_for(des)
            for opt in select.find_all("option"):
                text = _collapse(opt.get_text(" "))
                opt_id = opt.get("value") or None
                if not text or not opt_id:
                    continue
                delta = _parse_money(text) or Money(amount=Decimal("0.00"))
                if is_appraisal:
                    key = self._prov(
                        f"appraisal_options[{opt_id}]", f"OPTidOption{gid} ({des})", text
                    )
                    appraisals.append(
                        AppraisalOption(
                            name=re.sub(r"\s*\+?\s*\$[\d,.]+\s*$", "", text).strip(),
                            price_delta=delta,
                            option_value_id=opt_id,
                            provenance_key=key,
                        )
                    )
                else:
                    label_wo_price = re.sub(r"\s*\+?\s*\$[\d,.]+\s*$", "", text).strip()
                    sm = _SIZE_NUM_RE.search(label_wo_price)
                    value = None
                    if sm:
                        try:
                            value = Decimal(sm.group(1))
                        except InvalidOperation:
                            value = None
                    unit = "in" if axis in {"chain_length", "bracelet_length", "length"} else None
                    key = self._prov(
                        f"size_options[{opt_id}]", f"OPTidOption{gid} ({des})", text
                    )
                    variations.append(
                        Variation(
                            axis=axis or "option",
                            label=label_wo_price,
                            value=value,
                            unit=unit,
                            price_delta=delta,
                            option_value_id=opt_id,
                            child_sku=f"{self.sku}-{opt_id}",
                            provenance_key=key,
                        )
                    )
        return variations, appraisals

    # -- media --

    def _media(self) -> list[MediaAsset]:
        assets: list[MediaAsset] = []
        seen: set[str] = set()

        main = self.soup.select_one("img.mainProductImage")
        if isinstance(main, Tag) and main.get("src"):
            abs_url = urljoin(self.url, str(main["src"]))
            seen.add(abs_url)
            key = self._prov("media[main]", "img.mainProductImage", abs_url)
            assets.append(
                MediaAsset(role="main", kind="image", source_url=abs_url, provenance_key=key)
            )
        else:
            self._warn("MissingField", "media.main", "no mainProductImage")

        # Alternate Views: thumbnails after the "Alternate Views" header.
        for img in self.soup.find_all("img"):
            name = str(img.get("name", ""))
            if not name.startswith("thethumb"):
                continue
            src = img.get("src")
            if not src:
                continue
            abs_url = urljoin(self.url, str(src))
            if abs_url in seen:
                continue
            seen.add(abs_url)
            key = self._prov(f"media[alt:{len(assets)}]", "Alternate Views thumbnail", abs_url)
            assets.append(
                MediaAsset(role="alternate", kind="image", source_url=abs_url, provenance_key=key)
            )

        # Video: only if actually present in the page (probe separately when absent).
        source = self.soup.find("source", attrs={"src": re.compile(r"va\.mp4")})
        if isinstance(source, Tag) and source.get("src"):
            abs_url = urljoin(self.url, str(source["src"]))
            key = self._prov("media[video]", "video>source", abs_url)
            assets.append(
                MediaAsset(
                    role="video",
                    kind="video",
                    source_url=abs_url,
                    probed_ok=True,
                    provenance_key=key,
                )
            )
        return assets

    # -- breadcrumbs --

    def _breadcrumbs(self) -> list[list[str]]:
        label = self.soup.find(string=re.compile(r"Related Products"))
        container = label.find_parent(["td", "div"]) if label else None
        paths: list[list[str]] = []
        if container is None:
            self._warn("MissingField", "breadcrumbs", "no 'Related Products' block")
            return paths
        html = container.decode_contents()
        seen: set[tuple[str, ...]] = set()
        for chunk in re.split(r"<li\b", html)[1:]:
            # Only taxonomy anchors (prodList.asp); excludes Email a Friend / Product Inquiry.
            names = re.findall(r'<a[^>]*href="[^"]*prodList\.asp[^"]*"[^>]*>([^<]+)</a>', chunk)
            path = [_collapse(n) for n in names if _collapse(n) and _collapse(n) != ">"]
            if len(path) >= 2 and tuple(path) not in seen:
                seen.add(tuple(path))
                paths.append(path)
        if paths:
            self._prov("breadcrumbs", "Related Products block", str(paths)[:300])
        return paths

    # -- orchestration --

    def parse(self) -> Product:
        breadcrumbs = self._breadcrumbs()
        family, verified = self._family(breadcrumbs)
        specs = self._specs()
        title = self._marketing_title()
        variations, appraisals = self._option_groups()
        in_stock, free_ship = self._stock()

        return Product(
            sku=self._sku(),
            internal_product_id=self._internal_id(),
            family=family,
            family_verified=verified,
            source_url=self.url,
            marketing_title_raw=title,
            short_name=self._short_name(),
            long_description_raw=self._description(),
            specs=specs,
            attributes=self._attributes(specs, title),
            pricing=self._pricing(),
            in_stock=in_stock,
            free_shipping=free_ship,
            size_options=variations,
            appraisal_options=appraisals,
            media=self._media(),
            breadcrumbs=breadcrumbs,
            provenance=self.provenance,
            warnings=self.warnings,
            content_hash=self.fetch.content_hash,
            fetched_at=self.now,
        )


def parse_product(fetch: FetchResult) -> Product:
    return ProductParser(fetch).parse()
