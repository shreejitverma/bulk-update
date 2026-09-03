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
from typing import Annotated

import structlog
import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from anzorlist.config import MissingCredential, Settings
from anzorlist.config import settings as get_settings
from anzorlist.ingest import read_workbook, write_template
from anzorlist.marketplaces import group_by_region, resolve, resolve_all
from anzorlist.models.listing import BuiltListing
from anzorlist.pipeline import BuildOptions, BuildPipeline, BuildReport
from anzorlist.store.db import Ledger

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

console = Console()
err_console = Console(stderr=True)


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

    add("workbook", s.workbook_path.exists(),
        str(s.workbook_path) if s.workbook_path.exists()
        else f"{s.workbook_path} not found — run `anzorlist workbook init`")
    add("brand", bool(s.brand_name), f"{s.brand_name} (manufacturer: {s.manufacturer})")
    add("brand registry", s.is_brand_registered or None,
        "enrolled" if s.is_brand_registered
        else "not enrolled — GTIN exemption needs approval in Seller Central")

    add("Anthropic API key", s.anthropic_api_key is not None,
        f"copy model {s.copy_model}, escalates to {s.copy_model_escalation}"
        if s.anthropic_api_key else "copy generation will use the spec-sheet fallback")

    r2_ok = all([s.r2_account_id, s.r2_access_key_id, s.r2_secret_access_key,
                 s.r2_bucket, s.r2_public_base_url])
    add("image hosting (R2)", r2_ok,
        f"bucket {s.r2_bucket} → {s.r2_public_base_url}" if r2_ok
        else "not configured — listings cannot carry images without it")

    add("SP-API app", s.has_spapi_credentials(),
        "LWA client id and secret present" if s.has_spapi_credentials()
        else "register a developer profile and create an SP-API app (see docs/RUNBOOK.md)")

    markets = resolve_all(s.marketplaces)
    for region, group in group_by_region(markets).items():
        codes = ", ".join(m.code for m in group)
        try:
            s.refresh_token(region)
            s.seller_id(region)
            add(f"auth: {region.value.upper()}", True, f"authorized for {codes}")
        except MissingCredential as exc:
            add(f"auth: {region.value.upper()}", False, f"{codes} — {exc.var} is not set")

    add("live writes", None,
        "[green]ENABLED[/] — submit can create listings" if s.allow_live
        else "disabled (ANZOR_ALLOW_LIVE=false); submit will refuse")

    console.print(table)
    schemas = sorted(s.schema_cache_dir.glob("*.json")) if s.schema_cache_dir.exists() else []
    console.print(
        f"\nCached Amazon product-type schemas: [bold]{len(schemas)}[/]"
        + ("" if schemas else "  (run `anzorlist amazon sync-schemas` once credentials exist)")
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
    console.print(Panel.fit(
        f"[bold green]{'Regenerated' if existed else 'Created'}[/] {out}\n\n"
        "Open the [bold]Products[/] tab and fill in the [bold]SKU[/] column.\n"
        "Everything else is optional — hover any header for what it does.\n\n"
        "Then run: [cyan]anzorlist workbook validate[/]",
        title="workbook ready",
    ))


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
        err_console.print(f"[red]The workbook has {len(result.errors)} error(s).[/] "
                          "Run `anzorlist workbook validate` first.")
        raise typer.Exit(1)

    rows = result.included()
    if skus:
        wanted = {x.upper() for x in skus}
        rows = [r for r in rows if r.sku in wanted]
        missing = wanted - {r.sku for r in rows}
        if missing:
            err_console.print(f"[yellow]Not in the workbook (or Include = N):[/] "
                              f"{', '.join(sorted(missing))}")
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
    if len(report.submittable) < len(report.listings):
        raise typer.Exit(1)


def _print_build_report(report: BuildReport, s: Settings) -> None:
    table = Table(title=f"Build {report.run_id}", show_lines=False)
    table.add_column("SKU", style="cyan", no_wrap=True)
    table.add_column("Listings", justify="right")
    table.add_column("Images", justify="right")
    table.add_column("Copy", overflow="fold")
    table.add_column("Status", overflow="fold")

    for b in report.builds:
        if b.errors:
            status = "[red]" + "; ".join(b.errors)[:70] + "[/]"
        else:
            blocking = [i for listing in b.listings for i in listing.blocking_issues]
            warnings = [i for listing in b.listings for i in listing.issues if not i.blocking]
            if blocking:
                status = f"[red]{len(blocking)} blocking[/]: {blocking[0].message[:55]}"
            elif warnings:
                status = f"[yellow]{len(warnings)} warning(s)[/]"
            else:
                status = "[green]ready[/]"
        copy_note = b.copy_model + (" [yellow](escalated)[/]" if b.copy_escalated else "")
        table.add_row(b.sku, str(len(b.listings)), str(b.hosted_images), copy_note, status)

    console.print(table)
    c = report.counts()
    console.print(
        f"\n[bold]{c['listings']}[/] listing(s) built from [bold]{c['skus']}[/] SKU(s) · "
        f"[green]{c['listings_submittable']}[/] ready · [red]{c['listings_blocked']}[/] blocked"
    )
    console.print(f"Payloads written to [cyan]{s.build_dir}/[/]")
    if c["listings_submittable"]:
        console.print("\nNext: [cyan]anzorlist amazon validate[/] "
                      "(Amazon checks the payload and creates nothing)")


# =====================================================================  amazon


def _pool_and_settings():  # type: ignore[no-untyped-def]
    from anzorlist.channels.amazon.client import ClientPool

    s = get_settings()
    if not s.has_spapi_credentials():
        err_console.print(Panel.fit(
            "[red]No SP-API credentials.[/]\n\n"
            "This command needs a registered SP-API application. Until then, everything up to "
            "and including [cyan]anzorlist build[/] works offline.\n\n"
            "Setup steps: [cyan]docs/RUNBOOK.md[/]",
            title="credentials required",
        ))
        raise typer.Exit(2)
    return ClientPool(s), s


def _load_built(s: Settings, skus: list[str] | None) -> list[BuiltListing]:
    """Read payloads back off disk. The file is the artifact of record."""
    listings: list[BuiltListing] = []
    if not s.build_dir.exists():
        return listings
    wanted = {x.upper() for x in skus} if skus else None
    for path in sorted(s.build_dir.rglob("*.json")):
        data = json.loads(path.read_text())
        if wanted and data["sku"].upper() not in wanted:
            continue
        listings.append(BuiltListing(
            sku=data["sku"],
            parent_sku=data.get("parentSku"),
            source_sku=data.get("sku"),
            marketplace_id=resolve(data["marketplace"]).marketplace_id,
            marketplace_code=data["marketplace"],
            product_type=data["productType"],
            requirements=data.get("requirements", "LISTING"),
            attributes=data["body"]["attributes"],
            is_parent=data.get("isParent", False),
            payload_hash=data.get("payloadHash", ""),
            source_url=data.get("sourceUrl", ""),
        ))
    # Parents must be created before their children reference them.
    listings.sort(key=lambda x: (not x.is_parent,))
    return listings


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
                console.print(f"  [green]✓[/] {pt:<16} {len(schema.required_attributes)} required, "
                              f"{len(schema.known_attributes)} total attributes")
                ok += 1
            except Exception as exc:  # noqa: BLE001
                failed.append(pt)
                console.print(f"  [yellow]✗[/] {pt:<16} {str(exc)[:80]}")
        console.print(f"\n[bold]{ok}[/] cached to [cyan]{s.schema_cache_dir}[/]")
        if failed:
            console.print(f"[yellow]Not available in {market.code}:[/] {', '.join(failed)} "
                          "— remove these from the workbook's dropdown if you never use them.")
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
            console.print(Panel(report.render(),
                                border_style="red" if report.blocked else "green"))
            blocked = blocked or report.blocked
        if blocked:
            raise typer.Exit(1)
    finally:
        pool.close()


@amazon_app.command("validate")
def amazon_validate(
    skus: Annotated[list[str] | None, typer.Argument()] = None,
) -> None:
    """Run every built payload through Amazon's own validator. Creates nothing.

    This is the real dry run: Amazon applies the identical rules it would apply to a live
    submission, and returns the identical issues, without creating a listing.
    """
    from anzorlist.channels.amazon.listings import ListingsClient

    pool, s = _pool_and_settings()
    listings = _load_built(s, skus)
    if not listings:
        err_console.print("[red]No built payloads found.[/] Run `anzorlist build` first.")
        raise typer.Exit(1)

    with Ledger(s.state_db) as ledger:
        run_id = f"validate-{listings[0].payload_hash[:8]}"
        ledger.start_run(run_id, "amazon validate", mode="VALIDATION_PREVIEW")
        outcomes = []
        try:
            for listing in listings:
                market = resolve(listing.marketplace_code)
                client = ListingsClient(pool.for_region(market.region), s)
                outcome = client.put(listing, market, mode="VALIDATION_PREVIEW")
                ledger.record_submission(outcome, run_id)
                outcomes.append(outcome)
        finally:
            pool.close()
        ledger.finish_run(run_id, {"validated": len(outcomes)})

    _print_outcomes(outcomes, "Amazon validation (nothing was created)")
    if any(not o.accepted for o in outcomes):
        raise typer.Exit(1)
    console.print("\n[green]All payloads pass Amazon's validation.[/]")
    console.print("To create them for real: [cyan]anzorlist amazon submit --confirm[/]")


@amazon_app.command("submit")
def amazon_submit(
    skus: Annotated[list[str] | None, typer.Argument()] = None,
    confirm: Annotated[
        bool, typer.Option("--confirm", help="Required. Creates real listings.")
    ] = False,
    skip_validate: Annotated[bool, typer.Option("--skip-validate")] = False,
) -> None:
    """Create listings on Amazon. The only command that writes to the account."""
    from anzorlist.channels.amazon.listings import ListingsClient

    s = get_settings()
    if not confirm:
        err_console.print(Panel.fit(
            "[red]--confirm is required.[/]\n\n"
            "This command creates real listings on your Amazon account.\n"
            "Run [cyan]anzorlist amazon validate[/] first — it exercises the identical payload "
            "through Amazon's validator and creates nothing.",
            title="refusing to submit",
        ))
        raise typer.Exit(2)
    if not s.allow_live:
        err_console.print(Panel.fit(
            "[red]ANZOR_ALLOW_LIVE is not enabled.[/]\n\n"
            "Both gates must be open: set [cyan]ANZOR_ALLOW_LIVE=true[/] in .env and pass "
            "--confirm. They are separate on purpose — neither a stray flag nor a stale config "
            "value can cause a write on its own.",
            title="refusing to submit",
        ))
        raise typer.Exit(2)

    pool, _ = _pool_and_settings()
    listings = _load_built(s, skus)
    if not listings:
        err_console.print("[red]No built payloads found.[/] Run `anzorlist build` first.")
        raise typer.Exit(1)

    with Ledger(s.state_db) as ledger:
        pending = [x for x in listings if ledger.needs_submission(x)]
        unchanged = len(listings) - len(pending)
        if unchanged:
            console.print(f"[dim]{unchanged} listing(s) unchanged since the last accepted "
                          f"submission — skipping.[/]")
        if not pending:
            console.print("[green]Everything is already up to date.[/]")
            return

        console.print(f"\n[bold yellow]About to create {len(pending)} listing(s) on Amazon:[/]")
        for x in pending[:10]:
            kind = "parent" if x.is_parent else "child" if x.parent_sku else "standalone"
            price = f" @ {x.offer.currency} {x.offer.price}" if x.offer else ""
            console.print(f"  {x.sku:<24} {x.marketplace_code}  {kind:<10}{price}")
        if len(pending) > 10:
            console.print(f"  ... and {len(pending) - 10} more")
        if not typer.confirm("\nProceed?", default=False):
            console.print("Aborted. Nothing was sent.")
            raise typer.Exit(0)

        run_id = f"submit-{listings[0].payload_hash[:8]}"
        ledger.start_run(run_id, "amazon submit", mode="SUBMIT")
        outcomes = []
        try:
            for listing in pending:
                market = resolve(listing.marketplace_code)
                client = ListingsClient(pool.for_region(market.region), s)
                if not skip_validate:
                    preview = client.put(listing, market, mode="VALIDATION_PREVIEW")
                    ledger.record_submission(preview, run_id)
                    if not preview.accepted:
                        console.print(f"[red]✗ {listing.sku}[/] failed validation — not submitted")
                        outcomes.append(preview)
                        continue
                outcome = client.put(listing, market, mode="SUBMIT", confirm=True)
                ledger.record_submission(outcome, run_id)
                outcomes.append(outcome)
        finally:
            pool.close()
        ledger.finish_run(run_id, {"submitted": sum(1 for o in outcomes if o.accepted)})

    _print_outcomes(outcomes, f"Submission {run_id}")
    console.print(
        "\n[bold]Listings are created but not yet buyable.[/] Amazon processes new fine-jewelry "
        "listings asynchronously; check [cyan]anzorlist amazon status[/] in a few minutes, then "
        "confirm the detail pages in Seller Central before enabling inventory."
    )


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
                table.add_row(h["submitted_at"][:19], h["marketplace_id"][:14], h["mode"],
                              h["status"], (h["submission_id"] or "")[:20], str(len(issues)))
            console.print(table)
            return

        summary = ledger.status_summary()
        table = Table(title="Listing status")
        table.add_column("Status", style="cyan")
        table.add_column("Count", justify="right")
        for k, v in sorted(summary.items(), key=lambda kv: -kv[1]):
            table.add_row(k, str(v))
        console.print(table)
        live = ledger.live_skus()
        console.print(f"\n[bold]{len(live)}[/] SKU(s) believed to exist on Amazon.")
        for run in ledger.recent_runs(5):
            console.print(f"  [dim]{run['started_at'][:19]}  {run['command']:<18} "
                          f"{run['mode']:<20} {run.get('counts_json') or ''}[/]")


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
    console.print("[dim]This removes your offer. Sales history and reviews on the ASIN are "
                  "not recoverable by re-listing.[/]")
    if not typer.confirm("Type-check: proceed with deletion?", default=False):
        console.print("Aborted.")
        raise typer.Exit(0)

    pool, _ = _pool_and_settings()
    with Ledger(s.state_db) as ledger:
        run_id = f"delete-{market.code}"
        ledger.start_run(run_id, "amazon delete", mode="SUBMIT")
        try:
            client = ListingsClient(pool.for_region(market.region), s)
            for sku in skus:
                outcome = client.delete(sku, market, confirm=True)
                ledger.record_submission(outcome, run_id)
                console.print(f"  deleted [cyan]{sku}[/]")
        finally:
            pool.close()
        ledger.finish_run(run_id, {"deleted": len(skus)})


def _print_outcomes(outcomes: list, title: str) -> None:  # type: ignore[type-arg]
    table = Table(title=title, show_lines=False)
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
