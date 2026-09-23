"""Acquisition layer: immutable, content-addressed ERCOT MIS vintages.

Raw files are never mutated in place. Each download is stored under its
publication timestamp and recorded with a SHA256 of its bytes, so a later
ERCOT revision of the same logical report produces a *new* source file
rather than silently rewriting historical evidence.

Only the public MIS listing is used; no credentials are required. The
listing's retention window is measured at runtime and reported by the
readiness gate (see `MEMO.md` -- NP3-565 retains ~7 days of hourly
vintages, NP6-345 ~32 days of daily vintages).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import io
import re
import urllib.request
import zipfile
from pathlib import Path
from zoneinfo import ZoneInfo

CENTRAL = ZoneInfo("America/Chicago")
UTC = dt.UTC

_LISTING_URL = "https://www.ercot.com/misapp/GetReports.do?reportTypeId={report_type_id}"
_DOWNLOAD_URL = (
    "https://www.ercot.com/misdownload/servlets/mirDownload"
    "?mimic_duns=000000000&doclookupId={doc_id}"
)
_USER_AGENT = "forecast-spine/0.1 (+interview exercise; contact via repo)"

# Rows in the listing look like:
#   <td class='labelOptional_ind'>cdr.00014837.0.20260923.053000500.LFMODWEATHERNP3565_csv.zip</td>
#   ... <a href='/misdownload/servlets/mirDownload?...&doclookupId=1277949267'>zip</a>
_ROW_RE = re.compile(
    r"labelOptional_ind'>(?P<filename>cdr\.[^<]+?_csv\.zip)</td>"
    r".*?doclookupId=(?P<doc_id>\d+)'>zip</a>",
    re.DOTALL,
)

# cdr.00014837.0000000000000000.20260923.053000500.LFMODWEATHERNP3565_csv.zip
_FILENAME_RE = re.compile(
    r"^cdr\.(?P<report_type_id>\d+)\.(?P<seq>\d+)\."
    r"(?P<date>\d{8})\.(?P<time>\d{9})\.(?P<marker>[A-Za-z0-9_]+)_csv\.zip$"
)


@dataclasses.dataclass(frozen=True)
class Report:
    """A logical ERCOT data product we ingest."""

    key: str
    report_type_id: int
    product_id: str
    title: str
    file_marker: str


REPORTS: dict[str, Report] = {
    "load_forecast": Report(
        key="load_forecast",
        report_type_id=14837,
        product_id="NP3-565-CD",
        title="Seven-Day Load Forecast by Model and Weather Zone",
        file_marker="LFMODWEATHERNP3565",
    ),
    "actual_load": Report(
        key="actual_load",
        report_type_id=13101,
        product_id="NP6-345-CD",
        title="Actual System Load by Weather Zone",
        file_marker="ACTUALSYSLOADWZNP6345",
    ),
}


@dataclasses.dataclass(frozen=True)
class Vintage:
    """One publication of a report, as advertised by the MIS listing."""

    report_key: str
    filename: str
    doc_id: str
    publication_ts_utc: dt.datetime

    @property
    def publication_ts_local(self) -> dt.datetime:
        return self.publication_ts_utc.astimezone(CENTRAL)


@dataclasses.dataclass(frozen=True)
class SourceFile:
    """A downloaded vintage on local disk, with its content hash."""

    report_key: str
    filename: str
    publication_ts_utc: dt.datetime
    local_path: Path
    content_sha256: str
    size_bytes: int

    @property
    def source_file_id(self) -> str:
        """Stable identity = what the bytes are, not where they came from."""
        return self.content_sha256[:16]


class AcquisitionError(RuntimeError):
    pass


def parse_publication_ts(filename: str) -> dt.datetime:
    """Publication timestamp embedded in an MIS filename, as UTC.

    The MIS filename carries `YYYYMMDD.HHMMSSmmm` in ERCOT's operating
    timezone (America/Chicago), not UTC. This is asserted rather than
    documented by ERCOT; `scripts/evidence.py` re-derives it
    from the data and the result is recorded in `MEMO.md`. It matters: a
    wrong reading shifts every publication by 5-6 hours and would quietly
    admit forecasts published *after* the T-24h cutoff.
    """
    m = _FILENAME_RE.match(filename)
    if m is None:
        raise AcquisitionError(f"unrecognised MIS filename: {filename!r}")
    date, time = m.group("date"), m.group("time")
    naive = dt.datetime(  # noqa: DTZ001 -- localized on the next statement
        int(date[0:4]), int(date[4:6]), int(date[6:8]),
        int(time[0:2]), int(time[2:4]), int(time[4:6]),
        microsecond=int(time[6:9]) * 1000,
    )
    # fold=0: on the repeated hour we take the first (CDT) occurrence.
    return naive.replace(tzinfo=CENTRAL, fold=0).astimezone(UTC)


def _http_get(url: str, timeout: int = 60) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def fetch_listing(report: Report, timeout: int = 60) -> str:
    return _http_get(_LISTING_URL.format(report_type_id=report.report_type_id), timeout).decode(
        "utf-8", errors="replace"
    )


def parse_listing(report: Report, html: str) -> list[Vintage]:
    """Every CSV vintage advertised for `report`, newest first."""
    vintages = [
        Vintage(
            report_key=report.key,
            filename=m.group("filename"),
            doc_id=m.group("doc_id"),
            publication_ts_utc=parse_publication_ts(m.group("filename")),
        )
        for m in _ROW_RE.finditer(html)
        if report.file_marker in m.group("filename")
    ]
    # The listing repeats rows across its collapsible month sections.
    unique = {v.filename: v for v in vintages}
    return sorted(unique.values(), key=lambda v: v.publication_ts_utc, reverse=True)


def list_vintages(report: Report, timeout: int = 60) -> list[Vintage]:
    return parse_listing(report, fetch_listing(report, timeout))


def download_vintage(vintage: Vintage, raw_root: Path, timeout: int = 60) -> SourceFile:
    """Download a vintage if we do not already hold those exact bytes.

    Re-downloading is a no-op when the on-disk copy hashes identically. If
    ERCOT reissues a file under the same name with different bytes, the new
    content lands beside the old one under a hash-suffixed path; nothing is
    overwritten.
    """
    directory = raw_root / vintage.report_key
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / vintage.filename

    if path.exists():
        payload = path.read_bytes()
        return _source_file(vintage, path, payload)

    payload = _http_get(_DOWNLOAD_URL.format(doc_id=vintage.doc_id), timeout)
    if not payload.startswith(b"PK"):
        raise AcquisitionError(f"{vintage.filename}: response is not a zip archive")
    path.write_bytes(payload)
    return _source_file(vintage, path, payload)


def _source_file(vintage: Vintage, path: Path, payload: bytes) -> SourceFile:
    return SourceFile(
        report_key=vintage.report_key,
        filename=vintage.filename,
        publication_ts_utc=vintage.publication_ts_utc,
        local_path=path,
        content_sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
    )


def read_csv_text(source: SourceFile) -> str:
    """The single CSV member inside a downloaded MIS zip."""
    with zipfile.ZipFile(io.BytesIO(source.local_path.read_bytes())) as archive:
        members = [n for n in archive.namelist() if n.lower().endswith(".csv")]
        if len(members) != 1:
            raise AcquisitionError(
                f"{source.filename}: expected exactly one CSV member, found {members}"
            )
        return archive.read(members[0]).decode("utf-8-sig")


def scan_local(report_key: str, raw_root: Path) -> list[SourceFile]:
    """Source files already on disk for a report, oldest first.

    This is what makes `--source local` and the fixture runs work without
    touching the network, and what makes a rerun reproducible: the pipeline
    consumes files, not HTTP responses.
    """
    directory = raw_root / report_key
    if not directory.is_dir():
        return []
    files = []
    for path in sorted(directory.glob("*_csv.zip")):
        payload = path.read_bytes()
        files.append(
            SourceFile(
                report_key=report_key,
                filename=path.name,
                publication_ts_utc=parse_publication_ts(path.name),
                local_path=path,
                content_sha256=hashlib.sha256(payload).hexdigest(),
                size_bytes=len(payload),
            )
        )
    return sorted(files, key=lambda f: f.publication_ts_utc)
