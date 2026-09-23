"""Authenticated ERCOT Public API client, for historical vintages.

Why this exists
---------------
The public MIS listing (`ercot.py`) needs no credentials but retains only
about 7 days of NP3-565 vintages, which is why the evaluation window in
`MEMO.md` does not cross a DST transition. The Public API's archive endpoint
serves older vintages, and needs credentials.

Two credentials, not one
------------------------
An API Explorer subscription key on its own is **not** sufficient. Every
endpoint answers

    401 {"message": "Unauthorized. Access token is missing or invalid."}

until an `Authorization: Bearer <id_token>` is also supplied. That token comes
from ERCOT's Azure B2C resource-owner password flow and therefore needs the
ERCOT *account* username and password as well. Both facts were established by
probing, not assumed: the B2C endpoint and client id below are confirmed
correct because a deliberately invalid login reaches credential validation and
returns `AADB2C90225`, rather than a 404 or a malformed-request error.

Credentials are read from the environment (or a gitignored `.env`) and are
never logged, never echoed and never written to the warehouse.

One limitation, handled rather than hidden
------------------------------------------
The MIS filename grammar carries a local timestamp and no DST flag, so a
publication inside the repeated hour at fall-back is genuinely ambiguous: two
instants an hour apart reconstruct to the same name. The information is not
recoverable from the grammar. `backfill` therefore never overwrites on a name
collision -- it keeps both payloads under distinct sequence numbers, so the
pipeline sees two vintages at one publication timestamp, the as-of query
reports `value_count > 1`, and the readiness gate blocks the release. Ambiguous
input stopping the line is the correct outcome; a silently dropped vintage is
not.

Output compatibility
--------------------
Archived vintages are written into `data/raw/<report_key>/` under the same MIS
filename grammar the public path produces, so normalization, the as-of SQL and
the gates consume them unchanged. The acquisition source is an implementation
detail; nothing downstream knows which one ran.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import io
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import deque
from collections.abc import Iterator
from pathlib import Path

from .ercot import CENTRAL, REPORTS, Report, SourceFile, Vintage, _source_file

AUTH_URL = (
    "https://ercotb2c.b2clogin.com/ercotb2c.onmicrosoft.com/"
    "B2C_1_PUBAPI-ROPC-FLOW/oauth2/v2.0/token"
)
CLIENT_ID = "fec253ea-0d06-4272-a5e6-b478baeecd70"
API_BASE = "https://api.ercot.com/api/public-reports"

# ERCOT documents 30 requests per minute for the Public API. We run below it
# on purpose: the archive backfill is a batch job with no deadline, and being
# throttled costs more time than the margin does.
DOCUMENTED_REQUESTS_PER_MINUTE = 30
DEFAULT_REQUESTS_PER_MINUTE = 24

# Refresh a little before the token actually expires so a long backfill never
# races the boundary mid-request.
TOKEN_REFRESH_MARGIN_SECONDS = 120


class CredentialsMissing(RuntimeError):
    """Raised with instructions rather than a stack trace."""


class ErcotApiError(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class Credentials:
    subscription_key: str
    username: str
    password: str

    def __repr__(self) -> str:  # pragma: no cover - trivial, but load-bearing
        """Never let a credential reach a log, a traceback or a notebook cell."""
        return f"Credentials(username={self.username!r}, subscription_key=***, password=***)"

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> Credentials:
        values = dict(os.environ)
        if env_file and env_file.is_file():
            values = {**read_env_file(env_file), **values}  # real env wins

        missing = [
            name
            for name in ("ERCOT_SUBSCRIPTION_KEY", "ERCOT_USERNAME", "ERCOT_PASSWORD")
            if not values.get(name)
        ]
        if missing:
            raise CredentialsMissing(
                "The ERCOT Public API needs an account login as well as a subscription key.\n"
                f"Missing: {', '.join(missing)}\n\n"
                "Add them to a gitignored .env in the repo root:\n"
                "    ERCOT_SUBSCRIPTION_KEY=<key from apiexplorer.ercot.com>\n"
                "    ERCOT_USERNAME=<your ercot.com account email>\n"
                "    ERCOT_PASSWORD=<that account's password>\n\n"
                "The subscription key alone returns 401 on every endpoint; the username and\n"
                "password are what mint the bearer token. Nothing is committed: .env is\n"
                "gitignored and credentials are never written to the warehouse."
            )
        return cls(
            subscription_key=values["ERCOT_SUBSCRIPTION_KEY"],
            username=values["ERCOT_USERNAME"],
            password=values["ERCOT_PASSWORD"],
        )


def read_env_file(path: Path) -> dict[str, str]:
    """Minimal `KEY=value` reader; no shell semantics, no interpolation."""
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


class RateLimiter:
    """Sliding-window limiter over a rolling 60 seconds.

    A plain `sleep` between requests is not the same thing: it controls the
    gap, not the count, so a retry or a burst of small pages can still cross
    the published ceiling. This counts what was actually sent.
    """

    def __init__(self, requests_per_minute: int = DEFAULT_REQUESTS_PER_MINUTE) -> None:
        if requests_per_minute > DOCUMENTED_REQUESTS_PER_MINUTE:
            raise ValueError(
                f"{requests_per_minute}/min exceeds ERCOT's documented "
                f"{DOCUMENTED_REQUESTS_PER_MINUTE}/min"
            )
        self.requests_per_minute = requests_per_minute
        self._sent: deque[float] = deque()

    def acquire(
        self, *, now: float | None = None, sleep=time.sleep, clock=time.monotonic
    ) -> float:
        """Block until another request is allowed. Returns seconds waited.

        `clock` is injected so the throughput of the live path can be tested.
        Re-reading it after sleeping matters: adding the pause to a freshly
        read clock instead double-counts it, which pushes recorded timestamps
        into the future, stops the window draining, and compounds into a
        throttle several times tighter than the configured rate.
        """
        current = clock() if now is None else now
        waited = 0.0
        while True:
            while self._sent and current - self._sent[0] >= 60.0:
                self._sent.popleft()
            if len(self._sent) < self.requests_per_minute:
                self._sent.append(current)
                return waited
            pause = 60.0 - (current - self._sent[0]) + 0.01
            sleep(pause)
            waited += pause
            current = clock() if now is None else current + pause


@dataclasses.dataclass
class ArchiveEntry:
    """One archived publication, as the archive listing describes it."""

    doc_id: str
    post_datetime_utc: dt.datetime
    friendly_name: str


class ErcotApiClient:
    def __init__(
        self,
        credentials: Credentials,
        *,
        requests_per_minute: int = DEFAULT_REQUESTS_PER_MINUTE,
        timeout: int = 60,
    ) -> None:
        self._credentials = credentials
        self._limiter = RateLimiter(requests_per_minute)
        self._timeout = timeout
        self._token: str | None = None
        self._token_expires_at = 0.0

    # -- authentication ----------------------------------------------------
    def _bearer(self, *, force: bool = False) -> str:
        if not force and self._token and time.monotonic() < self._token_expires_at:
            return self._token

        payload = urllib.parse.urlencode(
            {
                "username": self._credentials.username,
                "password": self._credentials.password,
                "grant_type": "password",
                "scope": f"openid {CLIENT_ID} offline_access",
                "client_id": CLIENT_ID,
                "response_type": "id_token",
            }
        ).encode()
        request = urllib.request.Request(
            AUTH_URL, data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self._limiter.acquire()
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                body = json.loads(response.read())
        except urllib.error.HTTPError as error:
            raise _auth_error(error) from None

        token = body.get("id_token") or body.get("access_token")
        if not token:
            raise ErcotApiError("B2C returned no id_token; the auth flow may have changed")
        self._token = token
        self._token_expires_at = (
            time.monotonic() + float(body.get("expires_in", 3600)) - TOKEN_REFRESH_MARGIN_SECONDS
        )
        return token

    # -- requests ----------------------------------------------------------
    def _get(self, url: str, *, retry_auth: bool = True) -> bytes:
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self._bearer()}",
                "Ocp-Apim-Subscription-Key": self._credentials.subscription_key,
                "Accept": "application/json",
            },
        )
        self._limiter.acquire()
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if error.code == 401 and retry_auth:
                # The token expired mid-run; mint a new one and try once more.
                self._bearer(force=True)
                return self._get(url, retry_auth=False)
            if error.code == 429:
                pause = float(error.headers.get("Retry-After", 60))
                time.sleep(pause)
                return self._get(url, retry_auth=retry_auth)
            raise ErcotApiError(
                f"{error.code} from {_redact(url)}: "
                f"{error.read(300).decode('utf-8', 'replace')}"
            ) from None

    # -- archive -----------------------------------------------------------
    def list_archives(
        self, report: Report, start: dt.date, end: dt.date, *, page_size: int = 1000
    ) -> Iterator[ArchiveEntry]:
        """Every archived publication of `report` posted within [start, end]."""
        page = 1
        while True:
            query = urllib.parse.urlencode(
                {
                    "postDatetimeFrom": f"{start.isoformat()}T00:00:00",
                    "postDatetimeTo": f"{end.isoformat()}T23:59:59",
                    "size": page_size,
                    "page": page,
                }
            )
            body = json.loads(self._get(f"{API_BASE}/archive/{report.product_id}?{query}"))
            archives = body.get("archives", [])
            for record in archives:
                yield _archive_entry(record)
            meta = body.get("_meta", {})
            if page >= int(meta.get("totalPages", page)) or not archives:
                return
            page += 1

    def download_archive(self, report: Report, entry: ArchiveEntry) -> bytes:
        return self._get(f"{API_BASE}/archive/{report.product_id}?download={entry.doc_id}")


def _auth_error(error: urllib.error.HTTPError) -> ErcotApiError:
    try:
        body = json.loads(error.read())
        description = body.get("error_description", "")
    except Exception:  # noqa: BLE001 -- an unparseable error body must not mask the 401
        description = ""
    if "AADB2C90225" in description:
        return ErcotApiError(
            "ERCOT rejected the username or password (AADB2C90225). These are your "
            "ercot.com account credentials, which are separate from the API Explorer "
            "subscription key."
        )
    return ErcotApiError(f"B2C authentication failed ({error.code}): {description[:200]}")


def _archive_entry(record: dict) -> ArchiveEntry:
    raw = record.get("postDatetime") or record.get("postDatetimeUTC") or ""
    stamp = dt.datetime.fromisoformat(raw) if raw else None
    if stamp is None:
        raise ErcotApiError(f"archive record has no postDatetime: {record}")
    if stamp.tzinfo is None:
        # ERCOT reports operating times in Central; see MEMO.md section 1.
        stamp = stamp.replace(tzinfo=CENTRAL)
    return ArchiveEntry(
        doc_id=str(record.get("docId") or record.get("docID") or ""),
        post_datetime_utc=stamp.astimezone(dt.UTC),
        friendly_name=str(record.get("friendlyName") or ""),
    )


def _redact(url: str) -> str:
    return re.sub(r"(subscription-key|download)=[^&]+", r"\1=***", url)


def unpack_archive_payload(payload: bytes) -> list[tuple[str, bytes]]:
    """Return the CDR zips inside an archive download.

    The archive endpoint sometimes serves the CDR zip directly and sometimes a
    zip containing one or more of them. Both shapes are handled rather than
    guessed at, and anything else raises instead of being silently skipped.
    """
    if not payload.startswith(b"PK"):
        raise ErcotApiError("archive download is not a zip archive")
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = archive.namelist()
        inner = [n for n in names if n.lower().endswith(".zip")]
        if inner:
            return [(n, archive.read(n)) for n in inner]
        if any(n.lower().endswith(".csv") for n in names):
            return [("", payload)]
    raise ErcotApiError(f"archive download contains neither a zip nor a CSV: {names}")


def mis_filename(report: Report, published_utc: dt.datetime, sequence: int = 0) -> str:
    """Reconstruct the MIS filename grammar the rest of the pipeline parses."""
    local = published_utc.astimezone(CENTRAL)
    stamp = local.strftime("%Y%m%d.%H%M%S") + f"{local.microsecond // 1000:03d}"
    report_type_id = f"{report.report_type_id:08d}"
    return f"cdr.{report_type_id}.{sequence:016d}.{stamp}.{report.file_marker}_csv.zip"


def _free_path(
    directory: Path,
    report: Report,
    entry: ArchiveEntry,
    name: str,
    blob: bytes,
    index: int,
) -> tuple[str, Path]:
    """A path holding exactly these bytes, without clobbering different ones.

    Re-downloading a vintage we already hold is a no-op. A *different* payload
    landing on the same name -- which the repeated hour at fall-back can cause
    -- takes the next free sequence number instead of overwriting. Both
    survive, and the readiness gate refuses to release the ambiguity.
    """
    path = directory / name
    for sequence in range(index, index + 1000):
        if not path.exists() or path.read_bytes() == blob:
            return name, path
        name = mis_filename(report, entry.post_datetime_utc, sequence + 1)
        path = directory / name
    raise ErcotApiError(f"could not find a free filename for {entry.doc_id}")


def backfill(
    report_key: str,
    start: dt.date,
    end: dt.date,
    raw_root: Path,
    *,
    credentials: Credentials | None = None,
    client: ErcotApiClient | None = None,
    on_progress=None,
) -> list[SourceFile]:
    """Download archived vintages into the layout the pipeline already reads."""
    report = REPORTS[report_key]
    client = client or ErcotApiClient(credentials or Credentials.from_env(Path(".env")))
    directory = raw_root / report_key
    directory.mkdir(parents=True, exist_ok=True)

    written: list[SourceFile] = []
    for entry in client.list_archives(report, start, end):
        payload = client.download_archive(report, entry)
        for index, (inner_name, blob) in enumerate(unpack_archive_payload(payload)):
            name = inner_name.rsplit("/", 1)[-1] if inner_name else ""
            if not name.startswith("cdr.") or not name.endswith("_csv.zip"):
                name = mis_filename(report, entry.post_datetime_utc, index)
            name, path = _free_path(directory, report, entry, name, blob, index)
            if not path.exists():
                path.write_bytes(blob)
            vintage = Vintage(
                report_key=report_key,
                filename=name,
                doc_id=entry.doc_id,
                publication_ts_utc=entry.post_datetime_utc,
            )
            written.append(_source_file(vintage, path, blob))
        if on_progress:
            on_progress(entry, len(written))
    return written
