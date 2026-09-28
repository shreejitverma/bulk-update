"""Unit tests for the Amazon transport, feed reconciliation, planning, and image handling."""

from __future__ import annotations

import io
from pathlib import Path

import httpx
import pytest
from PIL import Image

from anzorlist.channels.amazon import client as spapi_client
from anzorlist.channels.amazon.client import SpApiClient, SpApiError, SpApiThrottled
from anzorlist.channels.amazon.feeds import FeedManifest, FeedMessage, FeedResult, reconcile
from anzorlist.channels.amazon.submit import plan_submission
from anzorlist.config import Settings
from anzorlist.marketplaces import Region
from anzorlist.media.pipeline import MediaPipeline
from anzorlist.models.listing import BuiltListing, ListingIssue, ListingStatus
from anzorlist.store.db import SubmissionState


class _Tokens:
    def access_token(self, region: Region) -> str:
        return "Atza|t"

    def invalidate(self, region: Region) -> None:
        return None


def _client(handler, settings: Settings) -> SpApiClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return SpApiClient(settings, Region.NA, tokens=_Tokens(), http=http)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(spapi_client.time, "sleep", lambda s: None)


class TestRetryExhaustion:
    def test_persistent_5xx_is_reported_as_a_server_error(self, settings: Settings) -> None:
        client = _client(lambda r: httpx.Response(503, json={"errors": []}), settings)
        with pytest.raises(SpApiError) as err:
            client.request("GET", "/x", operation="getFeed")
        assert err.value.status == 503

    def test_persistent_429_is_reported_as_throttling(self, settings: Settings) -> None:
        client = _client(lambda r: httpx.Response(429), settings)
        with pytest.raises(SpApiThrottled):
            client.request("GET", "/x", operation="getFeed")

    def test_transport_failure_is_reported_as_such(self, settings: Settings) -> None:
        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        with pytest.raises(SpApiError) as err:
            _client(boom, settings).request("GET", "/x", operation="getFeed")
        assert err.value.errors[0]["code"] == "TransportError"


def _manifest(*skus: str) -> FeedManifest:
    return FeedManifest(
        feed_id="F1",
        marketplace_code="US",
        marketplace_id="ATVPDKIKX0DER",
        run_id="r",
        created_at="2026-09-28T00:00:00+00:00",
        messages=[FeedMessage(i, sku, f"h{i}") for i, sku in enumerate(skus, start=1)],
    )


class TestReconcile:
    def test_a_fatal_feed_fails_every_message(self) -> None:
        outcomes = reconcile(FeedResult("F1", "FATAL"), _manifest("A", "B"))
        assert [o.status for o in outcomes] == [ListingStatus.REJECTED] * 2

    def test_warnings_do_not_reject(self) -> None:
        report = {"issues": [{"messageId": 2, "code": "W", "severity": "WARNING", "message": ""}]}
        outcomes = reconcile(FeedResult("F1", "DONE", report=report), _manifest("A", "B"))
        assert [o.status for o in outcomes] == [ListingStatus.SUBMITTED] * 2
        assert outcomes[1].issues[0].code == "W"

    def test_unknown_severity_is_blocking(self) -> None:
        report = {"issues": [{"messageId": 1, "code": "X", "severity": "MYSTERY"}]}
        [outcome] = reconcile(FeedResult("F1", "DONE", report=report), _manifest("A"))
        assert outcome.status is ListingStatus.REJECTED

    def test_summary_that_contradicts_the_issues_marks_the_rest_unknown(self) -> None:
        # The report says one message was invalid but attributes no error to any of them.
        report = {"issues": [], "summary": {"messagesProcessed": 2, "messagesInvalid": 1}}
        result = FeedResult("F1", "DONE", summary=report["summary"], report=report)
        outcomes = reconcile(result, _manifest("A", "B"))
        assert [o.status for o in outcomes] == [ListingStatus.ERROR] * 2
        assert outcomes[0].issues[0].code == "FeedReportMismatch"

    def test_unprocessed_messages_are_not_assumed_accepted(self) -> None:
        report = {"issues": [], "summary": {"messagesProcessed": 1, "messagesInvalid": 0}}
        result = FeedResult("F1", "DONE", summary=report["summary"], report=report)
        assert all(o.status is ListingStatus.ERROR for o in reconcile(result, _manifest("A", "B")))

    def test_an_unattributed_warning_does_not_skip_the_summary_check(self) -> None:
        report = {
            "issues": [{"code": "W", "severity": "WARNING", "message": ""}],
            "summary": {"messagesProcessed": 1, "messagesInvalid": 0},
        }
        result = FeedResult("F1", "DONE", summary=report["summary"], report=report)
        outcomes = reconcile(result, _manifest("A", "B"))
        assert [o.status for o in outcomes] == [ListingStatus.ERROR] * 2
        assert outcomes[0].issues[-1].code == "FeedReportMismatch"

    def test_an_unattributed_error_rejects_every_message(self) -> None:
        report = {
            "issues": [{"code": "E", "severity": "ERROR", "message": ""}],
            "summary": {"messagesProcessed": 0, "messagesInvalid": 2},
        }
        result = FeedResult("F1", "DONE", summary=report["summary"], report=report)
        outcomes = reconcile(result, _manifest("A", "B"))
        assert [o.status for o in outcomes] == [ListingStatus.REJECTED] * 2

    def test_a_consistent_summary_is_trusted(self) -> None:
        report = {
            "issues": [{"messageId": 1, "code": "E", "severity": "ERROR", "message": ""}],
            "summary": {"messagesProcessed": 2, "messagesInvalid": 1},
        }
        result = FeedResult("F1", "DONE", summary=report["summary"], report=report)
        statuses = [o.status for o in reconcile(result, _manifest("A", "B"))]
        assert statuses == [ListingStatus.REJECTED, ListingStatus.SUBMITTED]


def _listing(
    sku: str, *, parent: str | None = None, is_parent: bool = False, blocked: bool = False
) -> BuiltListing:
    return BuiltListing(
        sku=sku,
        parent_sku=parent,
        source_sku="R1",
        marketplace_id="ATVPDKIKX0DER",
        marketplace_code="US",
        product_type="RING",
        attributes={},
        is_parent=is_parent,
        issues=[ListingIssue(code="NoMainImage", message="x")] if blocked else [],
    )


class TestPlan:
    def test_child_selected_alone_still_sees_its_blocked_parent(self) -> None:
        parent = _listing("R1-PARENT", is_parent=True, blocked=True)
        child = _listing("R1-7", parent="R1-PARENT")
        plan = plan_submission([child], lambda x: "new", family=[parent, child])
        assert plan.send == [] and plan.orphaned == [child]

    def test_parent_is_withheld_when_every_child_is_blocked(self) -> None:
        family = [
            _listing("R1-PARENT", is_parent=True),
            _listing("R1-7", parent="R1-PARENT", blocked=True),
        ]
        plan = plan_submission(family, lambda x: "new")
        assert plan.send == []
        assert [x.sku for x in plan.orphaned] == ["R1-PARENT"]

    def test_child_selected_alone_is_held_until_its_parent_is_on_amazon(self) -> None:
        parent = _listing("R1-PARENT", is_parent=True)
        child = _listing("R1-7", parent="R1-PARENT")
        plan = plan_submission([child], lambda x: "new", family=[parent, child])
        assert plan.send == [] and plan.orphaned == [child]

        def parent_accepted(x: BuiltListing) -> SubmissionState:
            return "accepted" if x.is_parent else "new"

        plan = plan_submission([child], parent_accepted, family=[parent, child])
        assert plan.send == [child] and plan.orphaned == []

    def test_child_goes_when_its_parent_is_sent_in_the_same_run(self) -> None:
        family = [_listing("R1-7", parent="R1-PARENT"), _listing("R1-PARENT", is_parent=True)]
        plan = plan_submission(family, lambda x: "new")
        assert [x.sku for x in plan.send] == ["R1-PARENT", "R1-7"]

    def test_children_of_a_blocked_parent_are_orphaned(self) -> None:
        family = [
            _listing("R1-PARENT", is_parent=True, blocked=True),
            _listing("R1-7", parent="R1-PARENT"),
        ]
        plan = plan_submission(family, lambda x: "new")
        assert [x.sku for x in plan.orphaned] == ["R1-7"]

    @pytest.mark.parametrize(
        ("state", "bucket"),
        [
            ("new", "send"),
            ("changed", "send"),
            ("failed", "send"),
            ("accepted", "unchanged"),
            ("in_flight", "in_flight"),
        ],
    )
    def test_ledger_state_decides_the_bucket(self, state: str, bucket: str) -> None:
        plan = plan_submission([_listing("S1")], lambda x: state)  # type: ignore[arg-type,return-value]
        assert [x.sku for x in getattr(plan, bucket)] == ["S1"]


def _jpeg(size: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (size, size), (255, 255, 255)).save(buf, format="JPEG")
    return buf.getvalue()


class TestLocalImages:
    def test_operator_photos_replace_the_site_images(
        self, settings: Settings, ring, tmp_path: Path
    ) -> None:
        folder = settings.images_dir / ring.sku
        folder.mkdir(parents=True)
        (folder / "b.jpg").write_bytes(_jpeg(1100))
        (folder / "a.jpg").write_bytes(_jpeg(1200))

        def no_network(request: httpx.Request) -> httpx.Response:
            raise AssertionError("site images must not be fetched when overrides exist")

        media = MediaPipeline(
            settings, http=httpx.Client(transport=httpx.MockTransport(no_network))
        )
        result = media.process(ring, upload=False)
        assert result.source == "local"
        assert [(i.role, i.width) for i in result.images] == [("main", 1200), ("alternate", 1100)]
        assert "--no-upload" in result.diagnosis()

    def test_small_site_image_diagnosis_names_the_override_folder(
        self, settings: Settings, ring
    ) -> None:
        small = _jpeg(400)
        media = MediaPipeline(
            settings,
            http=httpx.Client(
                transport=httpx.MockTransport(lambda r: httpx.Response(200, content=small))
            ),
        )
        result = media.process(ring, upload=False)
        assert result.hosted_urls == []
        text = result.diagnosis()
        assert "400px" in text and str(settings.images_dir / ring.sku) in text


class TestResponseStatus:
    """Amazon answers a clean preview with VALID and a clean write with ACCEPTED."""

    @pytest.mark.parametrize(
        ("mode", "status", "expected"),
        [
            ("VALIDATION_PREVIEW", "VALID", ListingStatus.VALIDATED),
            ("VALIDATION_PREVIEW", "ACCEPTED", ListingStatus.VALIDATED),
            ("SUBMIT", "ACCEPTED", ListingStatus.SUBMITTED),
            ("SUBMIT", "INVALID", ListingStatus.REJECTED),
            ("SUBMIT", "VALID", ListingStatus.ERROR),  # a write that was only validated
            ("SUBMIT", "", ListingStatus.ERROR),  # empty body: unknown, never "pending"
        ],
    )
    def test_status_mapping(self, mode, status, expected, us) -> None:
        from anzorlist.channels.amazon.listings import _outcome_from_response

        outcome = _outcome_from_response({"status": status}, _listing("S1"), us, mode, None, 200)
        assert outcome.status is expected


def test_feeds_are_chunked_by_message_count(monkeypatch: pytest.MonkeyPatch) -> None:
    from anzorlist.channels.amazon import feeds

    monkeypatch.setattr(feeds, "MAX_MESSAGES_PER_FEED", 2)
    chunks = feeds.chunk_listings([_listing(f"S{i}") for i in range(5)])
    assert [len(c) for c in chunks] == [2, 2, 1]
    assert [x.sku for c in chunks for x in c] == [f"S{i}" for i in range(5)]
