"""Row accountability and the zone-mapping trap.

The invariant under test is the one the readiness gate enforces:

    raw_rows == accepted + duplicate_identical + quarantined

with every quarantined row keeping enough context to be acted on.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import run_scenario

from forecast_spine import ercot, fixtures, normalize

RAW = Path(__file__).resolve().parents[1] / "data" / "raw"


def _normalize_scenario_files(tmp_path, scenario="pass"):
    raw = fixtures.build(scenario, tmp_path)
    return [
        normalize.normalize(source)
        for key in ("load_forecast", "actual_load")
        for source in ercot.scan_local(key, raw)
    ]


def test_every_source_row_gets_exactly_one_disposition(tmp_path):
    for result in _normalize_scenario_files(tmp_path):
        counts = result.disposition_counts()
        assert sum(counts.values()) == result.raw_row_count
        assert result.raw_row_count > 0


def test_accepted_rows_expand_to_one_observation_per_zone_column(tmp_path):
    for result in _normalize_scenario_files(tmp_path):
        accepted = result.disposition_counts()[normalize.ACCEPTED]
        expected_columns = (
            len(normalize.FORECAST_ZONE_COLUMNS)
            if result.report_key == "load_forecast"
            else len(normalize.ACTUAL_ZONE_COLUMNS)
        )
        assert len(result.observations) == accepted * expected_columns


@pytest.mark.skipif(not RAW.is_dir(), reason="no downloaded vintages; run `forecast-spine acquire`")
def test_accounting_holds_for_every_downloaded_ercot_file():
    checked = 0
    for key in ("load_forecast", "actual_load"):
        for source in ercot.scan_local(key, RAW):
            result = normalize.normalize(source)  # raises if accounting breaks
            assert sum(result.disposition_counts().values()) == result.raw_row_count
            checked += 1
    assert checked > 0


def test_quarantined_rows_keep_their_payload_and_a_reason(tmp_path):
    raw = fixtures.build("schema_drift", tmp_path)
    results = [normalize.normalize(s) for s in ercot.scan_local("load_forecast", raw)]
    quarantined = [
        entry
        for result in results
        for entry in result.ledger
        if entry.disposition in normalize.QUARANTINE_DISPOSITIONS
    ]
    assert quarantined
    for entry in quarantined:
        assert entry.raw_line.strip(), "the raw payload must survive quarantine"
        assert entry.reason_code and entry.reason
        assert entry.source_row_number >= 2  # 1-based, header is row 1
        assert entry.source_filename


def test_schema_drift_quarantines_the_whole_file_and_emits_nothing(tmp_path):
    raw = fixtures.build("schema_drift", tmp_path)
    drifted = [
        result
        for result in (normalize.normalize(s) for s in ercot.scan_local("load_forecast", raw))
        if any(e.reason_code == "SCHEMA_DRIFT" for e in result.ledger)
    ]
    assert len(drifted) == 1
    result = drifted[0]
    assert result.observations == []
    assert result.disposition_counts()[normalize.QUARANTINED_SCHEMA_ERROR] == result.raw_row_count


def test_conflicting_duplicate_quarantines_both_sides_and_picks_no_winner(tmp_path):
    raw = fixtures.build("conflicting_duplicate", tmp_path)
    conflicted = [
        result
        for result in (normalize.normalize(s) for s in ercot.scan_local("load_forecast", raw))
        if any(e.reason_code == "SAME_KEY_DIFFERENT_VALUES" for e in result.ledger)
    ]
    assert len(conflicted) == 1
    result = conflicted[0]
    flagged = [e for e in result.ledger if e.reason_code == "SAME_KEY_DIFFERENT_VALUES"]
    assert len(flagged) == 2, "both rows are quarantined; neither is chosen"
    conflicted_rows = {e.source_row_number for e in flagged}
    emitted = {o.source_row_number for o in result.observations}
    assert conflicted_rows.isdisjoint(emitted)


def test_zone_columns_are_mapped_by_name_because_the_reports_order_them_differently(tmp_path):
    """The silent-swap trap, stated as an executable fact.

    NP3-565 lists SouthCentral before Southern; NP6-345 lists SOUTHERN before
    SOUTH_C. A parser that trusted column position would swap two multi-GW
    zones and produce a dataset that looks perfectly healthy.
    """
    # Column position, taken from the real headers the parser validates.
    forecast_columns = normalize.FORECAST_HEADER.split(",")
    actual_columns = normalize.ACTUAL_HEADER.split(",")
    forecast_at = lambda i: normalize.FORECAST_ZONE_COLUMNS[forecast_columns[i]]
    actual_at = lambda i: normalize.ACTUAL_ZONE_COLUMNS[actual_columns[i]]

    assert forecast_at(7) == "SOUTH_CENTRAL" and actual_at(7) == "SOUTHERN"
    assert forecast_at(8) == "SOUTHERN" and actual_at(8) == "SOUTH_CENTRAL"
    # Every other zone column does line up, which is what makes the swap so
    # easy to miss: a positional parser gets six of eight zones right.
    aligned = [i for i in range(2, 10) if forecast_at(i) == actual_at(i)]
    assert len(aligned) == 6

    raw = fixtures.build("pass", tmp_path)
    forecast = normalize.normalize(ercot.scan_local("load_forecast", raw)[-1])
    actual = normalize.normalize(ercot.scan_local("actual_load", raw)[-1])

    actual_by_key = {
        (o.weather_zone, o.target_ts_utc): o.value_mw for o in actual.observations
    }
    compared = 0
    for observation in forecast.observations:
        if not observation.is_ercot_model_in_use:
            continue
        key = (observation.weather_zone, observation.target_ts_utc)
        if key not in actual_by_key:
            continue
        # The fixtures set the in-use forecast to actual * 1.008 per zone. A
        # positional mapping would break this for SOUTHERN and SOUTH_CENTRAL,
        # whose magnitudes differ by roughly 2x.
        assert observation.value_mw == pytest.approx(actual_by_key[key] * 1.008, rel=1e-3)
        compared += 1
    assert compared > 0


def test_readiness_gate_reports_the_ledger_it_checked(tmp_path):
    _, _, readiness, _ = run_scenario("pass", tmp_path)
    metrics = readiness.metrics
    assert metrics["raw_source_rows"] == metrics["ledger_rows"]
    assert set(metrics["dispositions"]) == {normalize.ACCEPTED}
    assert readiness.passed
