"""End-to-end orchestration: spreadsheet row → submission-ready listing.

The build pipeline is deliberately **separable from the network**. Stages 1–6 below produce a
complete, schema-checked listing payload on disk without any Amazon credential; only stages 7
and 8 talk to SP-API. That is what makes the system usable today, before the SP-API developer
registration completes, and it is also what makes it testable — the whole pipeline runs against
committed HTML fixtures with no network at all.

    1. read      spreadsheet row (validated intent)
    2. extract   fetch + parse the product page       → Product
    3. media     download, validate, host images      → public URLs
    4. copy      generate + validate listing copy     → ListingCopy
    5. price     gross up for marketplace fees        → PriceQuote
    6. map       Product + row + copy + price         → BuiltListing(s)
       schema    validate against Amazon's own JSON Schema, offline
    ----------------------------------------------------------------- network boundary
    7. validate  Amazon's VALIDATION_PREVIEW (creates nothing)
    8. submit    live write, gated behind --confirm

Every stage failure is captured on the listing as a :class:`ListingIssue` rather than raised, so
one bad SKU never aborts a catalog run. The exception is a credential error, which is raised: if
the credentials are wrong, every remaining SKU will fail the same way and failing fast is kinder.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone

import structlog

from anzorlist.channels.amazon.artifacts import clear_family, write_listings
from anzorlist.channels.amazon.definitions import (
    DefinitionsClient,
    SchemaUnavailable,
    validate_attributes,
)
from anzorlist.channels.amazon.mapper import AmazonMapper
from anzorlist.channels.ebay import artifacts as ebay_artifacts
from anzorlist.channels.ebay.mapper import EbayMapper
from anzorlist.channels.ebay.models import EbayListing
from anzorlist.channels.etsy import artifacts as etsy_artifacts
from anzorlist.channels.etsy.mapper import EtsyMapper
from anzorlist.channels.etsy.models import EtsyListing
from anzorlist.config import Settings
from anzorlist.extract.client import SiteClient
from anzorlist.extract.parser import parse_product
from anzorlist.generate.copy import CopyGenerator, ListingCopy, fallback_copy
from anzorlist.ingest.row import ListingRow
from anzorlist.marketplaces import Marketplace, resolve, resolve_all
from anzorlist.media.pipeline import MediaPipeline
from anzorlist.models.listing import BuiltListing, IssueSeverity, ListingIssue, ListingStatus
from anzorlist.models.product import Product
from anzorlist.pricing import PricingError, price_for
from anzorlist.store.db import Ledger

log = structlog.get_logger(__name__)


@dataclass
class BuildOptions:
    """What the operator asked for. Defaults are the safe, offline-capable path."""

    use_copy: bool = True  # call Claude; False uses spec-sheet fallback copy
    use_media: bool = True  # download and host images
    upload_media: bool = True  # push to R2 (False = validate only)
    schema_check: bool = True  # validate against Amazon's cached JSON Schema
    force_refetch: bool = False  # ignore the raw HTML cache
    fx_rates: dict[str, str] = field(default_factory=dict)  # marketplace code -> USD rate
    charm_pricing: bool = False
    channels: set[str] | None = None  # None: ANZOR_CHANNELS


@dataclass
class SkuBuild:
    """Everything produced for one spreadsheet row."""

    sku: str
    product: Product | None = None
    listings: list[BuiltListing] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    copy_escalated: bool = False
    copy_model: str = ""
    hosted_images: int = 0
    ebay: EbayListing | None = None
    etsy: EtsyListing | None = None

    @property
    def ok(self) -> bool:
        return (
            bool(self.listings)
            and not self.errors
            and all(listing.submittable for listing in self.listings)
        )


@dataclass
class BuildReport:
    run_id: str
    builds: list[SkuBuild] = field(default_factory=list)

    @property
    def listings(self) -> list[BuiltListing]:
        return [listing for b in self.builds for listing in b.listings]

    @property
    def submittable(self) -> list[BuiltListing]:
        return [listing for listing in self.listings if listing.submittable]

    def counts(self) -> dict[str, int]:
        return {
            "skus": len(self.builds),
            "skus_ok": sum(1 for b in self.builds if b.ok),
            "listings": len(self.listings),
            "listings_submittable": len(self.submittable),
            "listings_blocked": len(self.listings) - len(self.submittable),
        }


class BuildPipeline:
    """Runs the offline half of the pipeline. Holds no SP-API client."""

    def __init__(
        self,
        settings: Settings,
        *,
        ledger: Ledger | None = None,
        site: SiteClient | None = None,
        definitions: DefinitionsClient | None = None,
    ) -> None:
        self.settings = settings
        self.ledger = ledger
        self._own_site = site is None
        self.site = site or SiteClient(
            base_url=settings.base_url,
            user_agent=settings.user_agent,
            req_per_sec=settings.req_per_sec,
            data_dir=settings.raw_dir,
        )
        self.media = MediaPipeline(settings)
        self.mapper = AmazonMapper(settings)
        self.ebay_mapper = EbayMapper(settings)
        self.etsy_mapper = EtsyMapper(settings)
        self.definitions = definitions or DefinitionsClient(None, settings.schema_cache_dir)
        self._copy_gen: CopyGenerator | None = None

    # ------------------------------------------------------------------ per-row

    def build_row(self, row: ListingRow, options: BuildOptions) -> SkuBuild:
        channels = self._channels(options)
        build = self._build_row(row, options, channels)
        self._write_artifacts(row, build, channels)
        return build

    def _channels(self, options: BuildOptions) -> set[str]:
        return options.channels if options.channels is not None else self.settings.channel_set()

    def _build_row(self, row: ListingRow, options: BuildOptions, channels: set[str]) -> SkuBuild:
        build = SkuBuild(sku=row.sku)

        # --- 2. extract ---
        try:
            fetched = self.site.fetch_product(row.sku, force=options.force_refetch)
            product = parse_product(fetched)
            build.product = product
        except Exception as exc:  # noqa: BLE001 — one bad SKU must not stop the catalog
            build.errors.append(f"extraction failed: {exc}")
            log.error("pipeline.extract_failed", sku=row.sku, error=str(exc))
            return build

        if product.pricing.our_price is None and row.price_override_usd is None:
            build.errors.append(
                "the product page has no selling price and no 'Price Override (USD)' was set"
            )
            return build

        # --- 3. media ---
        image_urls: list[str] = []
        image_files: list[str] = []
        image_problem = "image processing was skipped (--no-media)"
        if options.use_media:
            media = self.media.process(product, upload=options.upload_media)
            image_urls = media.hosted_urls
            image_files = media.local_files
            build.hosted_images = len(image_urls)
            image_problem = media.diagnosis()
            if not image_urls:
                # Not fatal at build time - the listing is still worth producing and reviewing -
                # but every listing below carries a blocking NoMainImage issue saying why.
                log.warning("pipeline.no_hosted_images", sku=row.sku, reason=image_problem)

        # --- 4. copy ---
        copy_obj, escalated, model_used = self._generate_copy(product, options)
        build.copy_escalated = escalated
        build.copy_model = model_used

        # --- 5 & 6. price + map, per marketplace ---
        markets = self._markets_for(row) if "amazon" in channels else []
        for marketplace in markets:
            try:
                quote = price_for(
                    product.pricing.our_price.amount if product.pricing.our_price else None,
                    marketplace,
                    fee_fraction=self.settings.markup_amazon,
                    override=row.price_override_usd,
                    list_price=row.list_price_usd
                    or (product.pricing.list_price.amount if product.pricing.list_price else None),
                    floor=self.settings.price_floor,
                    fx_rate=self._fx_rate(marketplace, options),
                    charm=options.charm_pricing,
                )
            except PricingError as exc:
                build.errors.append(f"{marketplace.code}: {exc}")
                log.warning(
                    "pipeline.pricing_failed",
                    sku=row.sku,
                    marketplace=marketplace.code,
                    error=str(exc),
                )
                continue

            listings = self.mapper.build(
                product=product,
                row=row,
                marketplace=marketplace,
                copy=copy_obj,
                quote=quote,
                image_urls=image_urls,
            )
            for listing in listings:
                _explain_missing_image(listing, image_problem)
            if options.schema_check:
                for listing in listings:
                    self._schema_check(listing, marketplace)
            _block_empty_parents(listings)

            build.listings.extend(listings)

        if "ebay" in channels:
            build.ebay = self._build_ebay(product, row, copy_obj, image_urls, build)
        if "etsy" in channels:
            build.etsy = self._build_etsy(product, row, copy_obj, image_files, build)
        return build

    def _build_etsy(
        self,
        product: Product,
        row: ListingRow,
        copy_obj: ListingCopy,
        image_files: list[str],
        build: SkuBuild,
    ) -> EtsyListing | None:
        """One Etsy listing, priced with Etsy's fee gross-up. Images go as files, not URLs."""
        try:
            quote = price_for(
                product.pricing.our_price.amount if product.pricing.our_price else None,
                resolve("US"),
                fee_fraction=self.settings.markup_etsy,
                override=row.price_override_usd,
                floor=self.settings.price_floor,
            )
        except PricingError as exc:
            build.errors.append(f"Etsy: {exc}")
            return None
        return self.etsy_mapper.build(
            product=product, row=row, copy=copy_obj, quote=quote, image_files=image_files
        )

    def _build_ebay(
        self,
        product: Product,
        row: ListingRow,
        copy_obj: ListingCopy,
        image_urls: list[str],
        build: SkuBuild,
    ) -> EbayListing | None:
        """One eBay listing page, priced with eBay's own fee gross-up (US only for now)."""
        try:
            quote = price_for(
                product.pricing.our_price.amount if product.pricing.our_price else None,
                resolve("US"),
                fee_fraction=self.settings.markup_ebay,
                override=row.price_override_usd,
                floor=self.settings.price_floor,
            )
        except PricingError as exc:
            build.errors.append(f"eBay: {exc}")
            return None
        return self.ebay_mapper.build(
            product=product, row=row, copy=copy_obj, quote=quote, image_urls=image_urls
        )

    # ------------------------------------------------------------------ batch

    def build_all(self, rows: list[ListingRow], options: BuildOptions) -> BuildReport:
        run_id = _run_id()
        report = BuildReport(run_id=run_id)
        if self.ledger:
            self.ledger.start_run(run_id, "build", mode="offline")

        for i, row in enumerate(rows, start=1):
            log.info("pipeline.row", n=i, of=len(rows), sku=row.sku)
            build = self.build_row(row, options)
            report.builds.append(build)
            if self.ledger:
                for listing in build.listings:
                    self.ledger.record_listing(listing)

        if self.ledger:
            self.ledger.finish_run(run_id, report.counts())
        log.info("pipeline.build_done", run_id=run_id, **report.counts())
        return report

    # ------------------------------------------------------------------ stages

    def _generate_copy(
        self, product: Product, options: BuildOptions
    ) -> tuple[ListingCopy, bool, str]:
        """Generate copy, falling back to a spec-sheet rendering rather than failing the SKU.

        A fallback listing is honest and submittable; it simply reads like a spec sheet. That is
        a much better outcome than no listing, and the caller is told which path was taken so a
        human can decide whether to rewrite it.
        """
        if not options.use_copy or self.settings.anthropic_api_key is None:
            reason = "disabled" if not options.use_copy else "no ANTHROPIC_API_KEY"
            log.info("pipeline.copy_skipped", sku=product.sku, reason=reason)
            return fallback_copy(product, self.settings.brand_name), False, f"fallback ({reason})"

        if self._copy_gen is None:
            self._copy_gen = CopyGenerator(self.settings)
        result = self._copy_gen.generate(product)
        if result.ok and result.copy is not None:
            return result.copy, result.escalated, result.model_used

        log.warning(
            "pipeline.copy_failed_using_fallback",
            sku=product.sku,
            errors=[e.code for e in result.report.errors],
        )
        return (
            fallback_copy(product, self.settings.brand_name),
            result.escalated,
            f"fallback (validation failed after {result.attempts} attempts)",
        )

    def _schema_check(self, listing: BuiltListing, marketplace: Marketplace) -> None:
        """Validate the payload against Amazon's own schema, locally.

        This is the highest-value check in the pipeline: it catches the errors Amazon would
        return, in milliseconds, for the whole catalog, with no credentials and no rate limit.
        """
        try:
            schema = self.definitions.get_schema(listing.product_type, marketplace)
        except SchemaUnavailable as exc:
            listing.issues.append(
                ListingIssue(
                    code="SchemaNotCached",
                    message=str(exc).split("\n")[0]
                    + " — the payload was built but not verified against Amazon's rules.",
                    severity=IssueSeverity.WARNING,
                    source="local",
                )
            )
            return
        except Exception as exc:  # noqa: BLE001
            listing.issues.append(
                ListingIssue(
                    code="SchemaCheckFailed",
                    message=f"could not run the schema check: {exc}",
                    severity=IssueSeverity.WARNING,
                    source="local",
                )
            )
            return

        issues = validate_attributes(listing.attributes, schema)
        for issue in issues:
            listing.issues.append(
                ListingIssue(
                    code="SchemaViolation",
                    message=issue.message,
                    severity=(
                        IssueSeverity.ERROR if issue.severity == "ERROR" else IssueSeverity.INFO
                    ),
                    attribute_names=[issue.attribute] if issue.attribute else [],
                    source="schema",
                )
            )
        listing.status = (
            ListingStatus.SCHEMA_FAILED
            if any(i.blocking for i in listing.issues)
            else ListingStatus.SCHEMA_OK
        )

    def _write_artifacts(self, row: ListingRow, build: SkuBuild, channels: set[str]) -> None:
        """Replace this SKU's artifacts. The files on disk always equal the latest build.

        The SKU is cleared in every marketplace first - including one that failed this time and one
        the row no longer names - so a payload from an earlier build can never be submitted.
        """
        if "amazon" in channels:
            clear_family(self.settings.build_dir, row.sku)
            write_listings(self.settings.build_dir, build.listings)
        if "ebay" in channels:
            ebay_artifacts.clear(self.settings.data_dir, row.sku)
            if build.ebay is not None:
                ebay_artifacts.write(self.settings.data_dir, build.ebay)
        if "etsy" in channels:
            etsy_artifacts.clear(self.settings.data_dir, row.sku)
            if build.etsy is not None:
                etsy_artifacts.write(self.settings.data_dir, build.etsy)

    # ------------------------------------------------------------------ helpers

    def _markets_for(self, row: ListingRow) -> list[Marketplace]:
        codes = row.marketplaces or [
            c.strip() for c in self.settings.marketplaces.split(",") if c.strip()
        ]
        return resolve_all(codes)

    @staticmethod
    def _fx_rate(marketplace: Marketplace, options: BuildOptions):  # type: ignore[no-untyped-def]
        from decimal import Decimal

        if marketplace.currency == "USD":
            return None
        raw = options.fx_rates.get(marketplace.code) or options.fx_rates.get(marketplace.currency)
        return Decimal(str(raw)) if raw else None

    def close(self) -> None:
        if self._own_site:
            self.site.close()
        self.media.close()


def _block_empty_parents(listings: list[BuiltListing]) -> None:
    """A parent whose every child is blocked would be an empty detail page; block it too."""
    for parent in (x for x in listings if x.is_parent):
        children = [x for x in listings if x.parent_sku == parent.sku]
        if children and not any(c.submittable for c in children):
            reason = children[0].blocking_issues[0]
            parent.issues.append(
                ListingIssue(
                    code="FamilyBlocked",
                    message=f"every child of this family is blocked, so the parent is withheld "
                    f"too (first child's problem: {reason.code}: {reason.message})",
                    source="local",
                )
            )


def _explain_missing_image(listing: BuiltListing, reason: str) -> None:
    """Replace the mapper's generic NoMainImage message with the actual cause and fix."""
    if not reason:
        return
    for issue in listing.issues:
        if issue.code == "NoMainImage":
            issue.message = f"no main image: {reason}"


def _run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    salt = hashlib.sha256(stamp.encode()).hexdigest()[:6]
    return f"run-{stamp}-{salt}"
