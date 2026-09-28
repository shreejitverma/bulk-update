"""``anzorlist`` — the operator interface.

Command grouping follows the safety boundary, not the code structure:

* ``workbook``  — everything about the spreadsheet. No network.
* ``build``     — extract, price, write copy, host images, build and schema-check payloads.
                  Touches the Anzor site, Anthropic, and R2 — never Amazon.
* ``amazon``    — the only group that talks to SP-API. Within it, ``preflight``, ``validate``,
                  and ``status`` are read-only or dry-run; ``submit`` and ``delete`` are the only
                  commands that can change anything on the account, and both demand ``--confirm``.

The grouping is the point: an operator can run everything except two commands and be certain
nothing was created.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import structlog
import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from anzorlist.config import MissingCredential, Settings
from anzorlist.config import settings as get_settings
from anzorlist.ingest import read_workbook, write_template
from anzorlist.ingest.row import ListingRow
from anzorlist.marketplaces import BY_ID, group_by_region, resolve, resolve_all
from anzorlist.models.listing import BuiltListing, SubmissionOutcome
from anzorlist.pipeline import BuildOptions, BuildPipeline, BuildReport
from anzorlist.store.db import Ledger, new_run_id

if TYPE_CHECKING:
    from collections.abc import Callable

    from anzorlist.channels.amazon.client import ClientPool
    from anzorlist.channels.ebay.client import EbayClient
    from anzorlist.channels.etsy.client import EtsyClient

app = typer.Typer(
    name="anzorlist",
    help="Extract Anzor Jewelry products and create Amazon listings — safely.",
    no_args_is_help=True,
    add_completion=False,
)
workbook_app = typer.Typer(
    help="Create and validate the Product Listing workbook.", no_args_is_help=True
)
amazon_app = typer.Typer(help="Amazon SP-API operations.", no_args_is_help=True)
app.add_typer(workbook_app, name="workbook")
app.add_typer(amazon_app, name="amazon")
ebay_app = typer.Typer(help="eBay Sell API operations.", no_args_is_help=True)
app.add_typer(ebay_app, name="ebay")
etsy_app = typer.Typer(help="Etsy Open API operations.", no_args_is_help=True)
app.add_typer(etsy_app, name="etsy")

console = Console()
err_console = Console(stderr=True)

# Exit codes: 0 done, 1 something failed or was blocked, 2 refused (safety gate or bad setup),
# 3 accepted by Amazon for processing but the result is not known yet.
EXIT_PENDING = 3


def _configure_logging(verbose: bool) -> None:
    import logging

    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.DEBUG if verbose else logging.INFO
        ),
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="%H:%M:%S"),
            structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()),
        ],
        logger_factory=structlog.PrintLoggerFactory(sys.stderr),
    )


@app.callback()
def main(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")] = False,
) -> None:
    _configure_logging(verbose)


# =====================================================================  doctor


@app.command()
def doctor() -> None:
    """Check configuration and credentials, and say exactly what is missing."""
    s = get_settings()
    table = Table(title="anzorlist configuration", show_lines=False)
    table.add_column("Check", style="cyan", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Detail", overflow="fold")

    def add(name: str, ok: bool | None, detail: str) -> None:
        mark = "[green]OK[/]" if ok else ("[yellow]—[/]" if ok is None else "[red]MISSING[/]")
        table.add_row(name, mark, detail)

    add(
        "workbook",
        s.workbook_path.exists(),
        (
            str(s.workbook_path)
            if s.workbook_path.exists()
            else f"{s.workbook_path} not found — run `anzorlist workbook init`"
        ),
    )
    add("brand", bool(s.brand_name), f"{s.brand_name} (manufacturer: {s.manufacturer})")
    add(
        "brand registry",
        s.is_brand_registered or None,
        (
            "enrolled"
            if s.is_brand_registered
            else "not enrolled — GTIN exemption needs approval in Seller Central"
        ),
    )

    add(
        "Anthropic API key",
        s.anthropic_api_key is not None,
        (
            f"copy model {s.copy_model}, escalates to {s.copy_model_escalation}"
            if s.anthropic_api_key
            else "copy generation will use the spec-sheet fallback"
        ),
    )

    r2_ok = all(
        [
            s.r2_account_id,
            s.r2_access_key_id,
            s.r2_secret_access_key,
            s.r2_bucket,
            s.r2_public_base_url,
        ]
    )
    add(
        "image hosting (R2)",
        r2_ok,
        (
            f"bucket {s.r2_bucket} → {s.r2_public_base_url}"
            if r2_ok
            else "not configured — listings cannot carry images without it"
        ),
    )

    add(
        "SP-API app",
        s.has_spapi_credentials(),
        (
            "LWA client id and secret present"
            if s.has_spapi_credentials()
            else "register a developer profile and create an SP-API app (see docs/RUNBOOK.md)"
        ),
    )

    markets = resolve_all(s.marketplaces)
    for region, group in group_by_region(markets).items():
        codes = ", ".join(m.code for m in group)
        try:
            s.refresh_token(region)
            s.seller_id(region)
            add(f"auth: {region.value.upper()}", True, f"authorized for {codes}")
        except MissingCredential as exc:
            add(f"auth: {region.value.upper()}", False, f"{codes} — {exc.var} is not set")

    channels = s.channel_set()
    if "ebay" in channels:
        _doctor_ebay(s, add)
    if "etsy" in channels:
        _doctor_etsy(s, add)

    add(
        "live writes",
        None,
        (
            "[green]ENABLED[/] — submit can create listings"
            if s.allow_live
            else "disabled (ANZOR_ALLOW_LIVE=false); submit will refuse"
        ),
    )

    console.print(table)
    schemas = sorted(s.schema_cache_dir.glob("*.json")) if s.schema_cache_dir.exists() else []
    console.print(
        f"\nCached Amazon product-type schemas: [bold]{len(schemas)}[/]"
        + ("" if schemas else "  (run `anzorlist amazon sync-schemas` once credentials exist)")
    )


def _unset(values: dict[str, object]) -> list[str]:
    return [var for var, value in values.items() if not value]


def _doctor_ebay(s: Settings, add: Callable[[str, bool | None, str], None]) -> None:
    env = s.ebay_env.upper()
    add(
        "eBay environment",
        env in ("SANDBOX", "PRODUCTION"),
        (
            f"{env}, marketplace {s.ebay_marketplace_id}"
            if env in ("SANDBOX", "PRODUCTION")
            else f"EBAY_ENV is {s.ebay_env!r} — set it to SANDBOX or PRODUCTION"
        ),
    )
    missing = _unset(
        {
            "EBAY_CLIENT_ID": s.ebay_client_id,
            "EBAY_CLIENT_SECRET": s.ebay_client_secret,
            "EBAY_REFRESH_TOKEN": s.ebay_refresh_token,
        }
    )
    add(
        "eBay credentials",
        not missing,
        (
            "keyset and user token present"
            if not missing
            else f"{', '.join(missing)} not set — create a keyset and a user token "
            "(docs/RUNBOOK.md, section eBay)"
        ),
    )
    missing = _unset(
        {
            "EBAY_FULFILLMENT_POLICY_ID": s.ebay_fulfillment_policy_id,
            "EBAY_PAYMENT_POLICY_ID": s.ebay_payment_policy_id,
            "EBAY_RETURN_POLICY_ID": s.ebay_return_policy_id,
            "EBAY_MERCHANT_LOCATION_KEY": s.ebay_merchant_location_key,
        }
    )
    add(
        "eBay policies",
        not missing,
        (
            "business policies and inventory location set"
            if not missing
            else f"{', '.join(missing)} not set — run `anzorlist ebay setup` to list the ids"
        ),
    )


def _doctor_etsy(s: Settings, add: Callable[[str, bool | None, str], None]) -> None:
    add(
        "Etsy app",
        s.etsy_api_key is not None,
        (
            "keystring present"
            if s.etsy_api_key
            else "ETSY_API_KEY not set — create an app at etsy.com/developers "
            "(docs/RUNBOOK.md, section Etsy)"
        ),
    )
    add(
        "Etsy shared secret",
        True if s.etsy_shared_secret else None,
        (
            "present; sent with the keystring"
            if s.etsy_shared_secret
            else "ETSY_SHARED_SECRET not set — requests send the keystring alone; "
            "copy the app's shared secret"
        ),
    )
    # The client prefers the rotated token it saved over the one in .env.
    token_file = s.data_dir / "etsy" / "token.json"
    add(
        "Etsy authorization",
        token_file.exists() or s.etsy_refresh_token is not None,
        (
            f"rotated refresh token in {token_file}"
            if token_file.exists()
            else (
                "ETSY_REFRESH_TOKEN present"
                if s.etsy_refresh_token
                else "ETSY_REFRESH_TOKEN not set — authorize the shop with OAuth 2 "
                "(docs/RUNBOOK.md, section Etsy)"
            )
        ),
    )
    missing = _unset(
        {
            "ETSY_SHOP_ID": s.etsy_shop_id,
            "ETSY_SHIPPING_PROFILE_ID": s.etsy_shipping_profile_id,
            "ETSY_RETURN_POLICY_ID": s.etsy_return_policy_id,
        }
    )
    add(
        "Etsy shop",
        not missing,
        (
            f"shop {s.etsy_shop_id}, shipping profile {s.etsy_shipping_profile_id}, "
            f"return policy {s.etsy_return_policy_id}"
            if not missing
            else f"{', '.join(missing)} not set — copy the ids from Shop Manager "
            "(docs/RUNBOOK.md, section Etsy)"
        ),
    )
    add(
        "Etsy listing claims",
        None,
        f"ETSY_WHO_MADE={s.etsy_who_made}, ETSY_WHEN_MADE={s.etsy_when_made} — "
        "confirm both describe the catalog",
    )


# =====================================================================  workbook


@workbook_app.command("init")
def workbook_init(
    path: Annotated[Path | None, typer.Option(help="Where to write the workbook.")] = None,
    no_examples: Annotated[bool, typer.Option("--no-examples")] = False,
) -> None:
    """Generate (or regenerate) the fillable workbook. Existing rows are preserved."""
    s = get_settings()
    target = path or s.workbook_path
    existed = target.exists()
    out = write_template(target, with_examples=not no_examples, preserve_existing=True)
    console.print(
        Panel.fit(
            f"[bold green]{'Regenerated' if existed else 'Created'}[/] {out}\n\n"
            "Open the [bold]Products[/] tab and fill in the [bold]SKU[/] column.\n"
            "Everything else is optional — hover any header for what it does.\n\n"
            "Then run: [cyan]anzorlist workbook validate[/]",
            title="workbook ready",
        )
    )


@workbook_app.command("validate")
def workbook_validate(
    path: Annotated[Path | None, typer.Option(help="Workbook to validate.")] = None,
) -> None:
    """Check the workbook without touching the network. Reports every error at once."""
    s = get_settings()
    result = read_workbook(path or s.workbook_path)

    if result.errors:
        table = Table(title=f"{len(result.errors)} problem(s) to fix", show_lines=False)
        table.add_column("Cell", style="red", no_wrap=True)
        table.add_column("Column", style="cyan", no_wrap=True)
        table.add_column("Value", overflow="fold")
        table.add_column("Problem", overflow="fold")
        for e in result.errors:
            table.add_row(e.cell or f"row {e.row}", e.header, str(e.value)[:40], e.message)
        console.print(table)

    included = result.included()
    console.print(
        f"\n[bold]{len(result.rows)}[/] row(s) read · "
        f"[green]{len(included)}[/] included · "
        f"[yellow]{len(result.skipped)}[/] staged (Include = N) · "
        f"[red]{len(result.errors)}[/] error(s)"
    )
    if result.unknown_headers:
        console.print(f"[yellow]Ignored unknown columns:[/] {', '.join(result.unknown_headers)}")
    if result.errors:
        raise typer.Exit(1)
    if included:
        markets = sorted({m for r in included for m in (r.marketplaces or [s.marketplaces])})
        console.print(f"Marketplaces referenced: [cyan]{', '.join(markets)}[/]")
        console.print("\nNext: [cyan]anzorlist build[/]")


# =====================================================================  build


@app.command()
def build(
    skus: Annotated[
        list[str] | None,
        typer.Argument(help="Specific SKUs; default is every included row."),
    ] = None,
    no_copy: Annotated[
        bool, typer.Option("--no-copy", help="Skip Claude; use spec-sheet copy.")
    ] = False,
    no_media: Annotated[
        bool, typer.Option("--no-media", help="Skip image download and hosting.")
    ] = False,
    no_upload: Annotated[
        bool, typer.Option("--no-upload", help="Check images but do not host them.")
    ] = False,
    refetch: Annotated[
        bool, typer.Option("--refetch", help="Ignore the cached page HTML.")
    ] = False,
    fx: Annotated[list[str] | None, typer.Option("--fx", help="FX rate, e.g. --fx UK=0.79")] = None,
) -> None:
    """Build submission-ready listing payloads. Talks to the Anzor site, never to Amazon."""
    s = get_settings()
    result = read_workbook(s.workbook_path)
    if result.errors:
        err_console.print(
            f"[red]The workbook has {len(result.errors)} error(s).[/] "
            "Run `anzorlist workbook validate` first."
        )
        raise typer.Exit(1)

    rows = result.included()
    if skus:
        wanted = {x.upper() for x in skus}
        rows = [r for r in rows if r.sku in wanted]
        missing = wanted - {r.sku for r in rows}
        if missing:
            err_console.print(
                f"[yellow]Not in the workbook (or Include = N):[/] {', '.join(sorted(missing))}"
            )
    if not rows:
        err_console.print("[red]Nothing to build.[/] Add SKUs to the Products tab.")
        raise typer.Exit(1)

    options = BuildOptions(
        use_copy=not no_copy,
        use_media=not no_media,
        upload_media=not no_upload,
        force_refetch=refetch,
        fx_rates=dict(pair.split("=", 1) for pair in (fx or [])),
    )

    with Ledger(s.state_db) as ledger:
        pipeline = BuildPipeline(s, ledger=ledger)
        try:
            report = pipeline.build_all(rows, options)
        finally:
            pipeline.close()

    _print_build_report(report, s)
    ebay_blocked = any(b.ebay is not None and not b.ebay.submittable for b in report.builds)
    etsy_blocked = any(b.etsy is not None and not b.etsy.submittable for b in report.builds)
    if len(report.submittable) < len(report.listings) or ebay_blocked or etsy_blocked:
        raise typer.Exit(1)


def _print_build_report(report: BuildReport, s: Settings) -> None:
    table = Table(title=f"Build {report.run_id}", show_lines=False)
    table.add_column("SKU", style="cyan", no_wrap=True)
    table.add_column("Listings", justify="right")
    table.add_column("Images", justify="right")
    table.add_column("Copy", overflow="fold")
    table.add_column("Status", overflow="fold")
    table.add_column("eBay", overflow="fold")
    table.add_column("Etsy", overflow="fold")

    for b in report.builds:
        if b.errors:
            status = "[red]" + "; ".join(b.errors) + "[/]"
        else:
            blocking = [i for listing in b.listings for i in listing.blocking_issues]
            warnings = [i for listing in b.listings for i in listing.issues if not i.blocking]
            if blocking:
                # Lead with a root cause, not the parent's derived "family is blocked" note.
                cause = next((i for i in blocking if i.code != "FamilyBlocked"), blocking[0])
                status = f"[red]{len(blocking)} blocking[/]: {cause.message}"
            elif warnings:
                status = f"[yellow]{len(warnings)} warning(s)[/]"
            else:
                status = "[green]ready[/]"
        copy_note = b.copy_model + (" [yellow](escalated)[/]" if b.copy_escalated else "")
        if b.ebay is None:
            ebay_note = "-"
        elif b.ebay.submittable:
            ebay_note = "[green]ready[/]"
        else:
            ebay_note = f"[red]blocked[/]: {b.ebay.blocking_issues[0].code}"
        if b.etsy is None:
            etsy_note = "-"
        elif b.etsy.submittable:
            etsy_note = "[green]ready[/]"
        else:
            etsy_note = f"[red]blocked[/]: {b.etsy.blocking_issues[0].code}"
        table.add_row(
            b.sku,
            str(len(b.listings)),
            str(b.hosted_images),
            copy_note,
            status,
            ebay_note,
            etsy_note,
        )

    console.print(table)
    c = report.counts()
    console.print(
        f"\n[bold]{c['listings']}[/] listing(s) built from [bold]{c['skus']}[/] SKU(s) · "
        f"[green]{c['listings_submittable']}[/] ready · [red]{c['listings_blocked']}[/] blocked"
    )
    console.print(f"Payloads written to [cyan]{s.build_dir}/[/]")
    if c["listings_submittable"]:
        console.print(
            "\nNext: [cyan]anzorlist amazon validate[/] "
            "(Amazon checks the payload and creates nothing)"
        )


# =====================================================================  amazon


def _make_pool(s: Settings) -> ClientPool:
    """Construct the SP-API client pool. A seam: the end-to-end tests swap in a fake Amazon."""
    from anzorlist.channels.amazon.client import ClientPool

    return ClientPool(s)


def _pool_and_settings() -> tuple[ClientPool, Settings]:
    s = get_settings()
    if not s.has_spapi_credentials():
        err_console.print(
            Panel.fit(
                "[red]No SP-API credentials.[/]\n\n"
                "This command needs a registered SP-API application. Until then, everything up to "
                "and including [cyan]anzorlist build[/] works offline.\n\n"
                "Setup steps: [cyan]docs/RUNBOOK.md[/]",
                title="credentials required",
            )
        )
        raise typer.Exit(2)
    return _make_pool(s), s


def _select_built(
    s: Settings, skus: list[str] | None
) -> tuple[list[BuiltListing], list[BuiltListing]]:
    """(selected, everything built): the listings an ``amazon`` command acts on, and the full
    build that parent/child checks consult.

    With explicit SKUs, those (a website SKU selects its whole variation family). Without, the
    rows currently included in the workbook, in the marketplaces those rows name - so a SKU set
    to Include = N is never sent just because an old build of it is still on disk.
    """
    from anzorlist.channels.amazon.artifacts import ArtifactError, load_listings
    from anzorlist.channels.amazon.submit import select_listings

    try:
        loaded = load_listings(s.build_dir)
    except ArtifactError as exc:
        err_console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc
    if loaded.legacy_files:
        err_console.print(
            f"[yellow]Ignoring {len(loaded.legacy_files)} payload(s) in the old flat layout "
            f"(e.g. {loaded.legacy_files[0]}). They carry no local check results; run "
            f"`anzorlist build` to regenerate them.[/]"
        )

    rows: list[ListingRow] | None = None
    if not skus:
        if not s.workbook_path.exists():
            err_console.print(
                f"[red]Workbook not found: {s.workbook_path}.[/] Without it there is no list of "
                "included rows to act on; name the SKUs explicitly."
            )
            raise typer.Exit(2)
        result = read_workbook(s.workbook_path)
        if result.errors:
            err_console.print(
                f"[red]The workbook has {len(result.errors)} error(s).[/] "
                "Run `anzorlist workbook validate`, or name the SKUs explicitly."
            )
            raise typer.Exit(1)
        rows = result.included()

    selected = select_listings(
        loaded.listings, skus=skus, rows=rows, default_marketplaces=s.marketplaces
    )
    if skus:
        found = {x.source_sku.upper() for x in selected} | {x.sku.upper() for x in selected}
        missing = sorted({k.upper() for k in skus} - found)
        if missing:
            err_console.print(f"[yellow]No built payloads for:[/] {', '.join(missing)}")
    return selected, loaded.listings


def _print_local_blockers(listings: list[BuiltListing], title: str) -> None:
    if not listings:
        return
    table = Table(title=title, show_lines=False)
    table.add_column("SKU", style="cyan", no_wrap=True)
    table.add_column("Mkt", no_wrap=True)
    table.add_column("Blocking problem", overflow="fold")
    for x in listings[:25]:
        first = x.blocking_issues[0] if x.blocking_issues else None
        table.add_row(x.sku, x.marketplace_code, f"{first.code}: {first.message}" if first else "")
    console.print(table)
    if len(listings) > 25:
        console.print(f"[dim]... and {len(listings) - 25} more[/]")


@amazon_app.command("sync-schemas")
def amazon_sync_schemas(
    marketplace: Annotated[str, typer.Option(help="Marketplace code.")] = "US",
    product_types: Annotated[list[str] | None, typer.Option("--type", help="Repeatable.")] = None,
) -> None:
    """Download and cache Amazon's JSON Schema for each jewelry product type.

    Run this once per marketplace. Afterwards `anzorlist build` validates every payload against
    Amazon's real rules offline, with no further API calls.
    """
    from anzorlist.channels.amazon.definitions import DefinitionsClient
    from anzorlist.ingest.schema import JEWELRY_PRODUCT_TYPES

    pool, s = _pool_and_settings()
    market = resolve(marketplace)
    types = product_types or list(JEWELRY_PRODUCT_TYPES)
    try:
        client = DefinitionsClient(pool.for_region(market.region), s.schema_cache_dir)
        ok, failed = 0, []
        for pt in types:
            try:
                schema = client.get_schema(pt, market, refresh=True)
                console.print(
                    f"  [green]✓[/] {pt:<16} {len(schema.required_attributes)} required, "
                    f"{len(schema.known_attributes)} total attributes"
                )
                ok += 1
            except Exception as exc:  # noqa: BLE001
                failed.append(pt)
                console.print(f"  [yellow]✗[/] {pt:<16} {str(exc)[:80]}")
        console.print(f"\n[bold]{ok}[/] cached to [cyan]{s.schema_cache_dir}[/]")
        if failed:
            console.print(
                f"[yellow]Not available in {market.code}:[/] {', '.join(failed)} "
                "— remove these from the workbook's dropdown if you never use them."
            )
    finally:
        pool.close()


@amazon_app.command("preflight")
def amazon_preflight(
    marketplaces: Annotated[str | None, typer.Option("--marketplaces", "-m")] = None,
    asin: Annotated[
        str | None, typer.Option(help="A comparable ASIN, to test category gating.")
    ] = None,
) -> None:
    """Check account registration and category gating before building a whole catalog."""
    from anzorlist.channels.amazon.preflight import PreflightClient

    pool, s = _pool_and_settings()
    markets = resolve_all(marketplaces or s.marketplaces)
    try:
        blocked = False
        for market in markets:
            client = PreflightClient(pool.for_region(market.region))
            report = client.run(market, sample_asin=asin)
            console.print(Panel(report.render(), border_style="red" if report.blocked else "green"))
            blocked = blocked or report.blocked
        if blocked:
            raise typer.Exit(1)
    finally:
        pool.close()


@amazon_app.command("validate")
def amazon_validate(
    skus: Annotated[
        list[str] | None,
        typer.Argument(help="Website SKUs (whole family) or listing SKUs; default: workbook."),
    ] = None,
) -> None:
    """Run built payloads through Amazon's own validator. Creates nothing.

    This is the real dry run: Amazon applies the identical rules it would apply to a live
    submission, and returns the identical issues, without creating a listing.
    """
    from anzorlist.channels.amazon.submit import AmazonSubmitter

    pool, s = _pool_and_settings()
    listings, _ = _select_built(s, skus)
    if not listings:
        pool.close()
        err_console.print("[red]No built payloads selected.[/] Run `anzorlist build` first.")
        raise typer.Exit(1)

    blocked = [x for x in listings if not x.submittable]
    with Ledger(s.state_db) as ledger:
        run_id = new_run_id("validate")
        ledger.start_run(run_id, "amazon validate", mode="VALIDATION_PREVIEW")
        try:
            outcomes = AmazonSubmitter(pool, s, ledger, run_id).validate(listings)
        finally:
            pool.close()
        ledger.finish_run(
            run_id,
            {"validated": len(outcomes), "passed": sum(1 for o in outcomes if o.accepted)},
        )

    _print_outcomes(outcomes, "Amazon validation (nothing was created)")
    _print_local_blockers(blocked, "Blocked by local checks (submit will skip these)")
    if blocked or any(not o.accepted for o in outcomes):
        raise typer.Exit(1)
    console.print("\n[green]All payloads pass Amazon's validation.[/]")
    console.print("To create them for real: [cyan]anzorlist amazon submit --confirm[/]")


@amazon_app.command("submit")
def amazon_submit(
    skus: Annotated[
        list[str] | None,
        typer.Argument(help="Website SKUs (whole family) or listing SKUs; default: workbook."),
    ] = None,
    confirm: Annotated[
        bool, typer.Option("--confirm", help="Required. Creates real listings.")
    ] = False,
    feed: Annotated[
        bool,
        typer.Option(
            "--feed",
            help="Bulk mode: JSON_LISTINGS_FEED instead of one call per listing. "
            "Use for hundreds of listings or more.",
        ),
    ] = False,
    skip_validate: Annotated[
        bool,
        typer.Option("--skip-validate", help="Per-item mode: skip the preview before each write."),
    ] = False,
    preview_sample: Annotated[
        int,
        typer.Option(min=1, help="Feed mode: listings per marketplace previewed before sending."),
    ] = 5,
    wait: Annotated[
        bool, typer.Option("--wait/--no-wait", help="Feed mode: wait for Amazon's report.")
    ] = True,
) -> None:
    """Create listings on Amazon. The only command that writes to the account."""
    from anzorlist.channels.amazon.submit import AmazonSubmitter, plan_submission

    s = get_settings()
    if not confirm:
        err_console.print(
            Panel.fit(
                "[red]--confirm is required.[/]\n\n"
                "This command creates real listings on your Amazon account.\n"
                "Run [cyan]anzorlist amazon validate[/] first - it exercises the identical payload "
                "through Amazon's validator and creates nothing.",
                title="refusing to submit",
            )
        )
        raise typer.Exit(2)
    if not s.allow_live:
        err_console.print(
            Panel.fit(
                "[red]ANZOR_ALLOW_LIVE is not enabled.[/]\n\n"
                "Both gates must be open: set [cyan]ANZOR_ALLOW_LIVE=true[/] in .env and pass "
                "--confirm. They are separate on purpose - neither a stray flag nor a stale config "
                "value can cause a write on its own.",
                title="refusing to submit",
            )
        )
        raise typer.Exit(2)

    pool, _ = _pool_and_settings()
    try:
        listings, everything = _select_built(s, skus)
        if not listings:
            err_console.print("[red]No built payloads selected.[/] Run `anzorlist build` first.")
            raise typer.Exit(1)

        with Ledger(s.state_db) as ledger:
            plan = plan_submission(
                listings, ledger.submission_state, is_live=ledger.is_live, family=everything
            )
            _print_local_blockers(plan.blocked, "Not sent: blocked by local checks")
            if plan.orphaned:
                console.print(
                    f"[yellow]{len(plan.orphaned)} listing(s) not sent because the rest of "
                    f"their variation family is blocked, or their parent is not on Amazon yet "
                    f"(submit the parent too):[/] "
                    + ", ".join(f"{x.sku} ({x.marketplace_code})" for x in plan.orphaned)
                )
            if plan.unchanged:
                console.print(
                    f"[dim]{len(plan.unchanged)} listing(s) unchanged since the last accepted "
                    f"submission - skipping.[/]"
                )
            if plan.in_flight:
                console.print(
                    f"[yellow]{len(plan.in_flight)} listing(s) are in a feed that has not been "
                    f"reconciled - skipping. Run `anzorlist amazon feed-status` to list them.[/]"
                )
            if not plan.send:
                if plan.blocked or plan.orphaned:
                    raise typer.Exit(1)
                if plan.in_flight:
                    raise typer.Exit(EXIT_PENDING)
                console.print("[green]Everything is already up to date.[/]")
                return

            mode = "feed" if feed else "per-item"
            console.print(
                f"\n[bold yellow]About to create {len(plan.send)} listing(s) on Amazon ({mode}):[/]"
            )
            for x in plan.send[:10]:
                kind = "parent" if x.is_parent else "child" if x.parent_sku else "standalone"
                price = f" @ {x.offer.currency} {x.offer.price}" if x.offer else ""
                console.print(f"  {x.sku:<24} {x.marketplace_code}  {kind:<10}{price}")
            if len(plan.send) > 10:
                console.print(f"  ... and {len(plan.send) - 10} more")
            if not typer.confirm("\nProceed?", default=False):
                console.print("Aborted. Nothing was sent.")
                raise typer.Exit(0)

            run_id = new_run_id("submit")
            ledger.start_run(run_id, "amazon submit", mode="FEED" if feed else "SUBMIT")
            submitter = AmazonSubmitter(pool, s, ledger, run_id)
            try:
                if feed:
                    submitter.submit_feed(plan.send, preview_sample=preview_sample, wait=wait)
                else:
                    submitter.submit_items(plan.send, preview_first=not skip_validate)
            finally:
                # Whatever happened, record and show what was written before it happened.
                outcomes = submitter.outcomes
                pending = submitter.feed_run.pending
                ledger.finish_run(
                    run_id,
                    {
                        "sent": len(plan.send),
                        "accepted": sum(1 for o in outcomes if o.accepted),
                        "failed": sum(1 for o in outcomes if not o.accepted),
                        "in_flight": sum(len(m.messages) for m in pending),
                        "blocked": len(plan.blocked) + len(plan.orphaned),
                    },
                )
                if outcomes:
                    _print_outcomes(outcomes, f"Submission {run_id}")
                for manifest in pending:
                    console.print(
                        f"[yellow]Feed {manifest.feed_id} ({len(manifest.messages)} listing(s)) "
                        f"has no result yet.[/] Check it with "
                        f"[cyan]anzorlist amazon feed-status {manifest.feed_id}[/]"
                    )
    finally:
        pool.close()

    if any(o.accepted for o in outcomes):
        console.print(
            "\n[bold]Accepted listings are created but not yet buyable.[/] Amazon processes new "
            "fine-jewelry listings asynchronously; check [cyan]anzorlist amazon status[/] in a "
            "few minutes, then confirm the detail pages in Seller Central before enabling "
            "inventory."
        )
    if plan.blocked or plan.orphaned or any(not o.accepted for o in outcomes):
        raise typer.Exit(1)
    if pending:
        raise typer.Exit(EXIT_PENDING)


@amazon_app.command("feed-status")
def amazon_feed_status(
    feed_id: Annotated[
        str | None,
        typer.Argument(help="A feed id from `amazon submit --feed`; omit to list open feeds."),
    ] = None,
    wait: Annotated[bool, typer.Option("--wait", help="Poll until the feed finishes.")] = False,
) -> None:
    """Check a bulk feed and record its per-listing results in the ledger."""
    from anzorlist.channels.amazon.feeds import (
        FeedError,
        FeedManifest,
        FeedPending,
        FeedsClient,
        reconcile,
    )
    from anzorlist.channels.amazon.submit import AmazonSubmitter

    if feed_id is None:
        s = get_settings()
        open_feeds = FeedManifest.unreconciled(s.data_dir / "feeds")
        if not open_feeds:
            console.print("No unreconciled feeds.")
            return
        table = Table(title=f"{len(open_feeds)} feed(s) without a recorded result")
        for col in ("Feed", "Mkt", "Listings", "Created"):
            table.add_column(col)
        for m in open_feeds:
            table.add_row(m.feed_id, m.marketplace_code, str(len(m.messages)), m.created_at[:19])
        console.print(table)
        console.print("Check one with [cyan]anzorlist amazon feed-status <feed-id>[/]")
        return

    pool, s = _pool_and_settings()
    try:
        try:
            manifest = FeedManifest.load(s.data_dir / "feeds", feed_id)
        except FeedError as exc:
            err_console.print(f"[red]{exc}[/]")
            raise typer.Exit(1) from exc
        market = resolve(manifest.marketplace_code)
        feeds = FeedsClient(pool.for_region(market.region), s, raw_http=pool.raw_http)
        try:
            result = feeds.wait(feed_id) if wait else feeds.check(feed_id)
        except FeedPending as exc:
            console.print(f"[yellow]{exc}[/] - nothing to record yet.")
            raise typer.Exit(EXIT_PENDING) from exc
        outcomes = reconcile(result, manifest)
        if manifest.reconciled:
            console.print("[dim]Already recorded in the ledger; showing the report again.[/]")
        else:
            with Ledger(s.state_db) as ledger:
                AmazonSubmitter(pool, s, ledger, manifest.run_id).record_feed_outcomes(
                    manifest, outcomes
                )
    finally:
        pool.close()

    _print_outcomes(outcomes, f"Feed {feed_id} ({result.processing_status})")
    if any(not o.accepted for o in outcomes):
        raise typer.Exit(1)


@amazon_app.command("status")
def amazon_status(
    sku: Annotated[str | None, typer.Argument(help="One SKU; omit for a summary.")] = None,
) -> None:
    """Show what the ledger believes about listing state."""
    s = get_settings()
    with Ledger(s.state_db) as ledger:
        if sku:
            history = ledger.history(sku)
            if not history:
                console.print(f"No submission history for [cyan]{sku}[/].")
                return
            table = Table(title=f"History for {sku}")
            for col in ("When", "Marketplace", "Mode", "Status", "Submission", "Issues"):
                table.add_column(col, overflow="fold")
            for h in history:
                issues = json.loads(h["issues_json"])
                table.add_row(
                    h["submitted_at"][:19],
                    h["marketplace_id"][:14],
                    h["mode"],
                    h["status"],
                    (h["submission_id"] or "")[:20],
                    str(len(issues)),
                )
            console.print(table)
            return

        summary = ledger.status_summary()
        table = Table(title="Listing status")
        table.add_column("Status", style="cyan")
        table.add_column("Count", justify="right")
        for k, v in sorted(summary.items(), key=lambda kv: -kv[1]):
            table.add_row(k, str(v))
        console.print(table)
        live = [e for e in ledger.live_skus() if e.marketplace_id in BY_ID]
        console.print(f"\n[bold]{len(live)}[/] SKU(s) believed to exist on Amazon.")
        for run in ledger.recent_runs(5):
            console.print(
                f"  [dim]{run['started_at'][:19]}  {run['command']:<18} "
                f"{run['mode']:<20} {run.get('counts_json') or ''}[/]"
            )


@amazon_app.command("delete")
def amazon_delete(
    skus: Annotated[list[str], typer.Argument(help="SKUs to delete.")],
    marketplace: Annotated[str, typer.Option("--marketplace", "-m")] = "US",
    confirm: Annotated[bool, typer.Option("--confirm")] = False,
) -> None:
    """Delete listings. Irreversible — the ASIN's history and reviews do not come back."""
    from anzorlist.channels.amazon.listings import ListingsClient

    s = get_settings()
    if not (confirm and s.allow_live):
        err_console.print("[red]Refusing:[/] deletion needs both --confirm and ANZOR_ALLOW_LIVE.")
        raise typer.Exit(2)

    market = resolve(marketplace)
    console.print(f"[bold red]About to DELETE {len(skus)} listing(s) from {market.code}.[/]")
    console.print(
        "[dim]This removes your offer. Sales history and reviews on the ASIN are "
        "not recoverable by re-listing.[/]"
    )
    if not typer.confirm("Type-check: proceed with deletion?", default=False):
        console.print("Aborted.")
        raise typer.Exit(0)

    from anzorlist.channels.amazon.client import SpApiError

    pool, _ = _pool_and_settings()
    deleted, failed = 0, 0
    with Ledger(s.state_db) as ledger:
        run_id = new_run_id("delete")
        ledger.start_run(run_id, "amazon delete", mode="DELETE")
        try:
            client = ListingsClient(pool.for_region(market.region), s)
            for sku in skus:
                try:
                    outcome = client.delete(sku, market, confirm=True)
                except SpApiError as exc:
                    failed += 1
                    console.print(f"  [red]not deleted[/] [cyan]{sku}[/]: {exc}")
                    continue
                ledger.record_submission(outcome, run_id)
                if outcome.accepted:
                    deleted += 1
                    console.print(f"  deleted [cyan]{sku}[/]")
                else:
                    failed += 1
                    console.print(f"  [red]not confirmed[/] [cyan]{sku}[/]: {outcome.summary()}")
        finally:
            pool.close()
            ledger.finish_run(run_id, {"deleted": deleted, "failed": failed})
    if failed:
        raise typer.Exit(1)


# =====================================================================  ebay


def _make_ebay_client(s: Settings) -> EbayClient:
    """Construct the eBay client. A seam: the end-to-end tests swap in a fake eBay."""
    from anzorlist.channels.ebay.client import EbayClient

    return EbayClient(s)


def _ebay_client_and_settings() -> tuple[EbayClient, Settings]:
    s = get_settings()
    try:
        s.ebay_credentials()
    except MissingCredential as exc:
        err_console.print(Panel.fit(f"[red]{exc}[/]", title="eBay credentials required"))
        raise typer.Exit(2) from exc
    return _make_ebay_client(s), s


@ebay_app.command("setup")
def ebay_setup() -> None:
    """List your eBay business policies and inventory locations, to fill in .env."""
    client, s = _ebay_client_and_settings()
    try:
        for kind, key in (
            ("fulfillment_policy", "fulfillmentPolicies"),
            ("payment_policy", "paymentPolicies"),
            ("return_policy", "returnPolicies"),
        ):
            _, body = client.request(
                "GET", f"/sell/account/v1/{kind}", params={"marketplace_id": client.marketplace_id}
            )
            table = Table(title=f"EBAY_{kind.upper()}_ID candidates")
            table.add_column("Id")
            table.add_column("Name")
            for p in (body or {}).get(key, []) or []:
                table.add_row(str(p.get(f"{kind.split('_')[0]}PolicyId", "")), str(p.get("name")))
            console.print(table)
        _, body = client.request("GET", "/sell/inventory/v1/location")
        table = Table(title="EBAY_MERCHANT_LOCATION_KEY candidates")
        table.add_column("Key")
        table.add_column("Name")
        for loc in (body or {}).get("locations", []) or []:
            table.add_row(str(loc.get("merchantLocationKey")), str(loc.get("name", "")))
        console.print(table)
    finally:
        client.close()


@ebay_app.command("submit")
def ebay_submit(
    skus: Annotated[
        list[str] | None, typer.Argument(help="Website SKUs; default: workbook.")
    ] = None,
    confirm: Annotated[
        bool, typer.Option("--confirm", help="Required. Publishes listings.")
    ] = False,
) -> None:
    """Stage and publish eBay listings, in bulk. Writes to the eBay account."""
    from anzorlist.channels.amazon.artifacts import ArtifactError
    from anzorlist.channels.ebay import artifacts as ebay_artifacts
    from anzorlist.channels.ebay.publish import EbayPublisher

    s = get_settings()
    if not (confirm and s.allow_live):
        err_console.print(
            Panel.fit(
                "[red]Publishing needs both --confirm and ANZOR_ALLOW_LIVE=true.[/]",
                title="refusing to publish",
            )
        )
        raise typer.Exit(2)
    try:
        s.ebay_listing_policies()
    except MissingCredential as exc:
        err_console.print(f"[red]{exc}[/]")
        raise typer.Exit(2) from exc

    try:
        built = ebay_artifacts.load_all(s.data_dir)
    except ArtifactError as exc:
        err_console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc
    wanted = {k.upper() for k in skus} if skus else _included_skus(s)
    selected = [x for x in built if x.source_sku.upper() in wanted]
    if not selected:
        err_console.print("[red]No built eBay listings selected.[/] Run `anzorlist build` first.")
        raise typer.Exit(1)

    blocked = [x for x in selected if not x.submittable]
    for x in blocked:
        first = x.blocking_issues[0]
        console.print(f"[red]blocked[/] {x.source_sku}: {first.code}: {first.message}")

    client = _make_ebay_client(s)
    outcomes: list[SubmissionOutcome] = []
    try:
        with Ledger(s.state_db) as ledger:
            ready = [x for x in selected if x.submittable]
            send = [x for x in ready if ledger.needs_submission(x)]
            if len(ready) > len(send):
                console.print(f"[dim]{len(ready) - len(send)} unchanged since last publish.[/]")
            if not send:
                if blocked:
                    raise typer.Exit(1)
                console.print("[green]Everything is already up to date.[/]")
                return
            console.print(f"\n[bold yellow]About to publish {len(send)} eBay listing(s):[/]")
            for x in send[:10]:
                kind = f"{len(x.items)} sizes" if x.is_group else "single"
                console.print(
                    f"  {x.source_sku:<12} {kind:<10} from {x.currency} {x.items[0].price}"
                )
            if not typer.confirm("\nProceed?", default=False):
                console.print("Aborted. Nothing was sent.")
                raise typer.Exit(0)
            run_id = new_run_id("ebay-submit")
            ledger.start_run(run_id, "ebay submit", mode="SUBMIT")
            publisher = EbayPublisher(client, s)
            try:
                for listing in send:
                    outcome = publisher.submit(listing, confirm=True)
                    ledger.record_submission(outcome, run_id)
                    outcomes.append(outcome)
            finally:
                ledger.finish_run(
                    run_id,
                    {
                        "published": sum(1 for o in outcomes if o.accepted),
                        "failed": sum(1 for o in outcomes if not o.accepted),
                        "blocked": len(blocked),
                    },
                )
                if outcomes:
                    _print_outcomes(outcomes, f"eBay {run_id}")
    finally:
        client.close()
    if blocked or any(not o.accepted for o in outcomes):
        raise typer.Exit(1)


def _included_skus(s: Settings) -> set[str]:
    """Website SKUs the workbook currently includes. Fails closed without a workbook."""
    if not s.workbook_path.exists():
        err_console.print(
            f"[red]Workbook {s.workbook_path} not found.[/] Name the SKUs explicitly."
        )
        raise typer.Exit(2)
    result = read_workbook(s.workbook_path)
    if result.errors:
        err_console.print("[red]The workbook has errors.[/] Run `anzorlist workbook validate`.")
        raise typer.Exit(1)
    return {r.sku.upper() for r in result.included()}


# =====================================================================  etsy


def _make_etsy_client(s: Settings) -> EtsyClient:
    """Construct the Etsy client. A seam: the end-to-end tests swap in a fake Etsy."""
    from anzorlist.channels.etsy.client import EtsyClient

    return EtsyClient(s)


@etsy_app.command("submit")
def etsy_submit(
    skus: Annotated[
        list[str] | None, typer.Argument(help="Website SKUs; default: workbook.")
    ] = None,
    confirm: Annotated[
        bool, typer.Option("--confirm", help="Required. Publishes listings.")
    ] = False,
) -> None:
    """Create or update Etsy listings and activate them. Writes to the Etsy shop."""
    from anzorlist.channels.amazon.artifacts import ArtifactError
    from anzorlist.channels.etsy import artifacts as etsy_artifacts
    from anzorlist.channels.etsy.publish import EtsyPublisher, shop_fields

    s = get_settings()
    if not (confirm and s.allow_live):
        err_console.print(
            Panel.fit(
                "[red]Publishing needs both --confirm and ANZOR_ALLOW_LIVE=true.[/]",
                title="refusing to publish",
            )
        )
        raise typer.Exit(2)
    try:
        shop = shop_fields(s)
    except MissingCredential as exc:
        err_console.print(f"[red]{exc}[/]")
        raise typer.Exit(2) from exc
    try:
        built = etsy_artifacts.load_all(s.data_dir, shop)
    except ArtifactError as exc:
        err_console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc
    wanted = {k.upper() for k in skus} if skus else _included_skus(s)
    selected = [x for x in built if x.source_sku.upper() in wanted]
    if not selected:
        err_console.print("[red]No built Etsy listings selected.[/] Run `anzorlist build` first.")
        raise typer.Exit(1)

    blocked = [x for x in selected if not x.submittable]
    for x in blocked:
        first = x.blocking_issues[0]
        console.print(f"[red]blocked[/] {x.source_sku}: {first.code}: {first.message}")

    client = _make_etsy_client(s)
    outcomes: list[SubmissionOutcome] = []
    try:
        with Ledger(s.state_db) as ledger:
            ready = [x for x in selected if x.submittable]
            send = [x for x in ready if ledger.needs_submission(x)]
            if len(ready) > len(send):
                console.print(f"[dim]{len(ready) - len(send)} unchanged since last publish.[/]")
            if not send:
                if blocked:
                    raise typer.Exit(1)
                console.print("[green]Everything is already up to date.[/]")
                return
            console.print(f"\n[bold yellow]About to publish {len(send)} Etsy listing(s):[/]")
            for x in send[:10]:
                kind = f"{len(x.variations)} sizes" if x.variations else "single"
                console.print(f"  {x.source_sku:<12} {kind:<10} from {x.currency} {x.price}")
            if not typer.confirm("\nProceed? (Etsy charges a listing fee per activation)"):
                console.print("Aborted. Nothing was sent.")
                raise typer.Exit(0)
            run_id = new_run_id("etsy-submit")
            ledger.start_run(run_id, "etsy submit", mode="SUBMIT")
            publisher = EtsyPublisher(client, s)
            try:
                for listing in send:
                    outcome = publisher.submit(listing, confirm=True)
                    ledger.record_submission(outcome, run_id)
                    outcomes.append(outcome)
            finally:
                ledger.finish_run(
                    run_id,
                    {
                        "published": sum(1 for o in outcomes if o.accepted),
                        "failed": sum(1 for o in outcomes if not o.accepted),
                        "blocked": len(blocked),
                    },
                )
                if outcomes:
                    _print_outcomes(outcomes, f"Etsy {run_id}")
    finally:
        client.close()
    if blocked or any(not o.accepted for o in outcomes):
        raise typer.Exit(1)


def _print_outcomes(outcomes: list[SubmissionOutcome], title: str) -> None:
    # A narrow table would otherwise wrap the title and split the run id mid-token.
    table = Table(title=title, show_lines=False, min_width=len(title))
    table.add_column("SKU", style="cyan", no_wrap=True)
    table.add_column("Mkt", no_wrap=True)
    table.add_column("Result", no_wrap=True)
    table.add_column("Issues", overflow="fold")

    for o in outcomes:
        mark = "[green]pass[/]" if o.accepted else "[red]FAIL[/]"
        blocking = [i for i in o.issues if i.blocking]
        warnings = [i for i in o.issues if not i.blocking]
        detail = ""
        if blocking:
            detail = "\n".join(f"[red]{i.code}[/]: {i.message}" for i in blocking[:3])
        elif warnings:
            detail = "\n".join(f"[yellow]{i.code}[/]: {i.message}" for i in warnings[:2])
        table.add_row(o.sku, o.marketplace_code, mark, detail or "—")
    console.print(table)


if __name__ == "__main__":
    app()
