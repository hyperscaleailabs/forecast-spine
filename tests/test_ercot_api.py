"""The authenticated archive path, tested without credentials or a network.

The live calls cannot be exercised here, so the surfaces that *can* be pinned
are pinned hard: the rate limiter, the filename grammar the rest of the
pipeline parses, both archive payload shapes, credential handling, and -- the
one that matters most -- that a backfilled vintage is indistinguishable from a
vintage fetched off the public MIS listing.
"""

from __future__ import annotations

import datetime as dt
import io
import zipfile
from pathlib import Path

import pytest

from forecast_spine import ercot, ercot_api, fixtures, normalize

UTC = dt.UTC


# --- rate limiting ---------------------------------------------------------


def test_limiter_refuses_to_exceed_the_documented_ceiling():
    with pytest.raises(ValueError, match="exceeds ERCOT's documented"):
        ercot_api.RateLimiter(requests_per_minute=ercot_api.DOCUMENTED_REQUESTS_PER_MINUTE + 1)


def test_default_rate_stays_under_the_documented_ceiling():
    assert ercot_api.DEFAULT_REQUESTS_PER_MINUTE < ercot_api.DOCUMENTED_REQUESTS_PER_MINUTE


def test_limiter_allows_a_full_minute_of_requests_then_waits():
    slept: list[float] = []
    limiter = ercot_api.RateLimiter(requests_per_minute=3)

    for _ in range(3):
        assert limiter.acquire(now=100.0, sleep=slept.append) == 0.0
    assert slept == [], "the first N requests in a window must not block"

    waited = limiter.acquire(now=100.0, sleep=slept.append)
    assert waited > 0 and slept, "the N+1th request in the same window must wait"
    assert pytest.approx(waited, abs=0.1) == 60.0


def test_limiter_lets_requests_through_once_the_window_rolls():
    limiter = ercot_api.RateLimiter(requests_per_minute=2)
    limiter.acquire(now=0.0, sleep=lambda _: None)
    limiter.acquire(now=0.0, sleep=lambda _: None)
    # 61 seconds later the earlier pair has aged out of the window.
    assert limiter.acquire(now=61.0, sleep=lambda _: None) == 0.0


# --- filename grammar ------------------------------------------------------


@pytest.mark.parametrize(
    "published",
    [
        dt.datetime(2026, 3, 8, 11, 30, tzinfo=UTC),          # after spring-forward
        dt.datetime(2026, 9, 23, 10, 30, 0, 500_000, tzinfo=UTC),
        dt.datetime(2026, 11, 1, 5, 50, tzinfo=UTC),          # before the repeated hour
        dt.datetime(2026, 11, 1, 9, 50, tzinfo=UTC),          # after it
    ],
)
def test_reconstructed_filenames_round_trip_through_the_mis_parser(published):
    """A backfilled file must carry its publication time where the pipeline looks."""
    report = ercot.REPORTS["load_forecast"]
    name = ercot_api.mis_filename(report, published)
    assert ercot.parse_publication_ts(name) == published


def test_the_repeated_hour_is_not_recoverable_from_the_filename():
    """A documented limitation, pinned so it cannot regress silently.

    2026-11-01 06:50 and 07:50 UTC are both 01:50 local -- the hour that runs
    twice at fall-back. The MIS grammar has no DST flag, so both reconstruct
    to the same filename and the parser can only return one of them. The
    collision is handled in `backfill`, not papered over here.
    """
    report = ercot.REPORTS["load_forecast"]
    cdt = dt.datetime(2026, 11, 1, 6, 50, tzinfo=UTC)
    cst = dt.datetime(2026, 11, 1, 7, 50, tzinfo=UTC)
    assert ercot_api.mis_filename(report, cdt) == ercot_api.mis_filename(report, cst)
    assert ercot.parse_publication_ts(ercot_api.mis_filename(report, cst)) == cdt


# --- archive payload shapes ------------------------------------------------


def _cdr_zip(csv_text: str, member: str = "cdr.test.csv") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(member, csv_text)
    return buffer.getvalue()


def _wrap(*payloads: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for index, payload in enumerate(payloads):
            archive.writestr(f"cdr.0001.{index}.inner_csv.zip", payload)
    return buffer.getvalue()


def test_a_bare_cdr_zip_is_passed_through_untouched():
    payload = _cdr_zip("a,b\n1,2\n")
    assert ercot_api.unpack_archive_payload(payload) == [("", payload)]


def test_a_zip_of_zips_is_unpacked_into_its_members():
    first, second = _cdr_zip("a\n1\n"), _cdr_zip("a\n2\n")
    unpacked = ercot_api.unpack_archive_payload(_wrap(first, second))
    assert [blob for _, blob in unpacked] == [first, second]


def test_a_payload_that_is_neither_shape_raises_instead_of_being_skipped():
    with pytest.raises(ercot_api.ErcotApiError, match="not a zip"):
        ercot_api.unpack_archive_payload(b"<html>login</html>")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("readme.txt", "nothing useful")
    with pytest.raises(ercot_api.ErcotApiError, match="neither a zip nor a CSV"):
        ercot_api.unpack_archive_payload(buffer.getvalue())


# --- credentials -----------------------------------------------------------


def test_missing_credentials_explain_what_to_do(monkeypatch, tmp_path):
    for name in ("ERCOT_SUBSCRIPTION_KEY", "ERCOT_USERNAME", "ERCOT_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ercot_api.CredentialsMissing) as raised:
        ercot_api.Credentials.from_env(tmp_path / "absent.env")
    message = str(raised.value)
    assert "ERCOT_USERNAME" in message and "ERCOT_PASSWORD" in message
    assert "subscription key alone returns 401" in message


def test_env_file_is_read_but_the_real_environment_wins(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "# comment\n"
        "ERCOT_SUBSCRIPTION_KEY=from-file\n"
        'ERCOT_USERNAME="quoted@example.com"\n'
        "ERCOT_PASSWORD=from-file-secret\n"
    )
    monkeypatch.delenv("ERCOT_SUBSCRIPTION_KEY", raising=False)
    monkeypatch.setenv("ERCOT_USERNAME", "from-environment@example.com")
    monkeypatch.setenv("ERCOT_PASSWORD", "env-secret")

    credentials = ercot_api.Credentials.from_env(env)
    assert credentials.subscription_key == "from-file"
    assert credentials.username == "from-environment@example.com"
    assert credentials.password == "env-secret"


def test_credentials_never_render_their_secrets():
    rendered = repr(ercot_api.Credentials("super-secret-key", "me@example.com", "hunter2"))
    assert "super-secret-key" not in rendered
    assert "hunter2" not in rendered
    assert "me@example.com" in rendered


def test_download_urls_are_redacted_in_error_messages():
    redacted = ercot_api._redact(
        "https://api.ercot.com/api/public-reports/archive/NP3-565-CD?download=abc123"
    )
    assert "abc123" not in redacted


# --- archive listing timestamps -------------------------------------------


def test_naive_archive_timestamps_are_read_as_central():
    entry = ercot_api._archive_entry(
        {"docId": 1, "postDatetime": "2026-03-08T05:30:00", "friendlyName": "x"}
    )
    # The transition is at 02:00 local, so 05:30 on 2026-03-08 is already CDT
    # (UTC-5) and lands at 10:30 UTC -- not 11:30, which is the CST reading.
    assert entry.post_datetime_utc == dt.datetime(2026, 3, 8, 10, 30, tzinfo=UTC)


def test_explicitly_utc_archive_timestamps_are_respected():
    entry = ercot_api._archive_entry(
        {"docId": 2, "postDatetime": "2026-03-08T11:30:00Z", "friendlyName": "x"}
    )
    assert entry.post_datetime_utc == dt.datetime(2026, 3, 8, 11, 30, tzinfo=UTC)


# --- the compatibility guarantee ------------------------------------------


class _FakeClient:
    """Serves real CDR zips the way the archive endpoint would."""

    def __init__(self, payloads: list[tuple[ercot_api.ArchiveEntry, bytes]]) -> None:
        self._payloads = payloads
        self.downloads = 0

    def list_archives(self, report, start, end):
        for entry, _ in self._payloads:
            yield entry

    def download_archive(self, report, entry):
        self.downloads += 1
        return next(payload for candidate, payload in self._payloads if candidate is entry)


def test_backfilled_vintages_are_indistinguishable_from_mis_vintages(tmp_path):
    """The point of the whole module: nothing downstream should notice.

    Files written by the archive path are scanned, parsed and normalized by
    exactly the code that handles the credential-free public path, with no
    quarantined rows and the publication timestamp intact.
    """
    source_root = fixtures.build("pass", tmp_path / "fixtures")
    originals = sorted((source_root / "load_forecast").glob("*_csv.zip"))[:2]

    payloads = []
    for index, path in enumerate(originals):
        published = dt.datetime(2026, 3, 6, 12 + index, 30, tzinfo=UTC)
        entry = ercot_api.ArchiveEntry(
            doc_id=f"doc-{index}", post_datetime_utc=published, friendly_name=path.name
        )
        payloads.append((entry, path.read_bytes()))

    client = _FakeClient(payloads)
    raw_root = tmp_path / "raw"
    written = ercot_api.backfill(
        "load_forecast", dt.date(2026, 3, 6), dt.date(2026, 3, 6), raw_root, client=client
    )

    assert len(written) == 2
    assert client.downloads == 2

    scanned = ercot.scan_local("load_forecast", raw_root)
    assert len(scanned) == 2
    assert [s.publication_ts_utc for s in scanned] == [e.post_datetime_utc for e, _ in payloads]

    for source in scanned:
        result = normalize.normalize(source)
        counts = result.disposition_counts()
        assert sum(counts.values()) == result.raw_row_count
        assert set(counts) == {normalize.ACCEPTED}
        assert result.observations


def test_backfill_does_not_overwrite_bytes_already_held(tmp_path):
    """Immutability survives the archive path too."""
    source_root = fixtures.build("pass", tmp_path / "fixtures")
    original = min((source_root / "load_forecast").glob("*_csv.zip"))
    entry = ercot_api.ArchiveEntry(
        doc_id="doc-0",
        post_datetime_utc=dt.datetime(2026, 3, 6, 12, 30, tzinfo=UTC),
        friendly_name=original.name,
    )
    raw_root = tmp_path / "raw"
    client = _FakeClient([(entry, original.read_bytes())])
    first = ercot_api.backfill(
        "load_forecast", dt.date(2026, 3, 6), dt.date(2026, 3, 6), raw_root, client=client
    )
    path = Path(first[0].local_path)
    mtime = path.stat().st_mtime_ns

    ercot_api.backfill(
        "load_forecast", dt.date(2026, 3, 6), dt.date(2026, 3, 6), raw_root,
        client=_FakeClient([(entry, original.read_bytes())]),
    )
    assert path.stat().st_mtime_ns == mtime, "an already-held vintage must not be rewritten"


def test_a_colliding_vintage_is_kept_rather_than_dropped(tmp_path):
    """Two different payloads, one ambiguous timestamp: both must survive.

    This is the fall-back repeated hour in miniature. Overwriting would lose a
    vintage silently; skipping would too. Keeping both lets the readiness gate
    see the conflict and refuse the release.
    """
    source_root = fixtures.build("pass", tmp_path / "fixtures")
    first, second = sorted((source_root / "load_forecast").glob("*_csv.zip"))[:2]
    assert first.read_bytes() != second.read_bytes()

    published = dt.datetime(2026, 11, 1, 6, 50, tzinfo=UTC)
    entries = [
        (ercot_api.ArchiveEntry(doc_id="cdt", post_datetime_utc=published, friendly_name=""),
         first.read_bytes()),
        (ercot_api.ArchiveEntry(doc_id="cst", post_datetime_utc=published, friendly_name=""),
         second.read_bytes()),
    ]
    raw_root = tmp_path / "raw"
    written = ercot_api.backfill(
        "load_forecast", dt.date(2026, 11, 1), dt.date(2026, 11, 1), raw_root,
        client=_FakeClient(entries),
    )

    assert len(written) == 2
    on_disk = sorted((raw_root / "load_forecast").glob("*_csv.zip"))
    assert len(on_disk) == 2, "the second payload must not have been dropped"
    assert {p.read_bytes() for p in on_disk} == {first.read_bytes(), second.read_bytes()}
    # Both parse to the same publication timestamp, which is what the gate sees.
    assert len({ercot.parse_publication_ts(p.name) for p in on_disk}) == 1
