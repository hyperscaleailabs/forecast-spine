"""The gates, exercised through the whole pipeline.

Each failing scenario is a materially different failure -- a missing
publication, an ambiguous one, and an upstream schema change -- because a
gate that only catches one shape of problem is a gate that will be surprised.
"""

from __future__ import annotations

import json

from conftest import reason_codes, run_scenario

from forecast_spine import gates


def test_clean_day_passes_both_gates(tmp_path):
    _, _, readiness, model = run_scenario("pass", tmp_path)
    assert readiness.passed, readiness.to_json()
    assert model.passed, model.to_json()
    assert readiness.reasons == () and model.reasons == ()


def test_spring_forward_day_passes_with_twenty_three_hours(tmp_path):
    _, _, readiness, model = run_scenario("dst_spring_forward", tmp_path)
    assert readiness.passed, readiness.to_json()
    assert model.passed
    # 23 hours x 8 weather zones; a hard-coded 24 would report missing data.
    assert readiness.metrics["evaluation_rows"] == 23 * 8
    assert readiness.metrics["expected_evaluation_rows"] == 23 * 8


def test_missing_publication_blocks_release_and_is_not_back_filled(tmp_path):
    connection, _, readiness, model = run_scenario("missing_forecast", tmp_path)
    assert not readiness.passed
    assert "MISSING_ASOF_FORECAST" in reason_codes(readiness)
    assert model is None, "the model gate must not run on a dataset that failed readiness"

    reason = next(r for r in readiness.reasons if r.code == "MISSING_ASOF_FORECAST")
    assert reason.count == 8  # one hour x eight zones
    assert reason.sample_keys

    # The value does exist in a later vintage; it simply was not knowable.
    missing_hours = connection.execute(
        "SELECT DISTINCT hour_ending FROM evaluation_dataset WHERE ercot_forecast_mw IS NULL"
    ).fetchall()
    assert missing_hours == [(13,)]
    later = connection.execute(
        "SELECT count(*) FROM forecast_vintage WHERE hour_ending = 13 AND is_ercot_model_in_use"
    ).fetchone()[0]
    assert later > 0, "the data was available later, and was correctly not used"


def test_conflicting_duplicate_blocks_release_without_choosing_a_winner(tmp_path):
    connection, _, readiness, model = run_scenario("conflicting_duplicate", tmp_path)
    assert not readiness.passed
    assert "QUARANTINED_SAME_KEY_DIFFERENT_VALUES" in reason_codes(readiness)
    assert model is None

    conflicted = connection.execute(
        """SELECT count(*) FROM source_row
           WHERE reason_code = 'SAME_KEY_DIFFERENT_VALUES'"""
    ).fetchone()[0]
    assert conflicted == 2
    # Neither value reached the vintage table.
    emitted = connection.execute(
        """SELECT count(*) FROM forecast_vintage f
           WHERE EXISTS (SELECT 1 FROM source_row r
                         WHERE r.source_file_id = f.source_file_id
                           AND r.source_row_number = f.source_row_number
                           AND r.reason_code = 'SAME_KEY_DIFFERENT_VALUES')"""
    ).fetchone()[0]
    assert emitted == 0


def test_schema_drift_blocks_release_rather_than_parsing_positionally(tmp_path):
    _, _, readiness, model = run_scenario("schema_drift", tmp_path)
    assert not readiness.passed
    assert {"SCHEMA_DRIFT", "QUARANTINED_SCHEMA_DRIFT"} <= reason_codes(readiness)
    assert model is None


def test_gate_verdicts_are_machine_readable(tmp_path):
    _, _, readiness, model = run_scenario("pass", tmp_path)
    for result in (readiness, model):
        payload = json.loads(result.to_json())
        assert payload["status"] in {"PASS", "FAIL"}
        assert payload["gate"] and payload["run_id"] and payload["processing_date"]
        assert isinstance(payload["reasons"], list)
        assert isinstance(payload["metrics"], dict)


def test_readiness_gate_does_not_trust_the_query_it_gates(tmp_path):
    """Inject hindsight straight into the dataset; the gate must still catch it.

    The cutoff predicate lives in `asof_join.sql`. If someone edits that file
    and relaxes it, every downstream number stays plausible. This check is
    the independent second opinion.
    """
    connection, run_context, readiness, _ = run_scenario("pass", tmp_path)
    assert readiness.passed

    connection.execute(
        """UPDATE evaluation_dataset
           SET ercot_publication_ts_utc = cutoff_ts_utc + INTERVAL 1 MINUTE
           WHERE weather_zone = 'COAST'"""
    )
    tampered = gates.data_readiness(connection, run_context)
    assert not tampered.passed
    assert "CUTOFF_VIOLATION" in reason_codes(tampered)


def test_model_gate_fails_when_the_threshold_is_tightened(tmp_path):
    """The gate is a decision, not a formality: tighten it and it says no."""
    connection, run_context, _, model = run_scenario("pass", tmp_path)
    assert model.passed
    observed = model.metrics["wape_pct"]

    strict = gates.Thresholds(max_wape_pct=observed / 2)
    refused = gates.seasonal_naive_gate(connection, run_context, strict)
    assert not refused.passed
    assert "WAPE_ABOVE_THRESHOLD" in reason_codes(refused)


def test_a_good_average_does_not_excuse_a_bad_peak_hour(tmp_path):
    """The failure mode the peak guardrail exists for.

    One zone-day peak hour is corrupted while every other hour is untouched.
    Overall WAPE barely moves and still passes; the peak-hour guardrail is
    what refuses the release.
    """
    connection, run_context, _, baseline = run_scenario("pass", tmp_path)
    assert baseline.passed

    connection.execute(
        """UPDATE evaluation_dataset
           SET naive_forecast_mw = naive_forecast_mw * 0.5,
               naive_error_mw = naive_forecast_mw * 0.5 - actual_mw
           WHERE (weather_zone, target_ts_utc) IN (
               SELECT weather_zone, target_ts_utc FROM (
                   SELECT weather_zone, target_ts_utc,
                          row_number() OVER (PARTITION BY weather_zone
                                             ORDER BY actual_mw DESC) AS peak_rank
                   FROM evaluation_dataset
               ) WHERE peak_rank = 1 AND weather_zone = 'NORTH_CENTRAL'
           )"""
    )
    degraded = gates.seasonal_naive_gate(connection, run_context)
    assert degraded.metrics["wape_pct"] <= gates.Thresholds().max_wape_pct, (
        "overall WAPE still passes, which is the point"
    )
    assert not degraded.passed
    assert "PEAK_HOUR_ERROR_ABOVE_THRESHOLD" in reason_codes(degraded)
