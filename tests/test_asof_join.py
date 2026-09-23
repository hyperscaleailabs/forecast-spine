"""Boundary behaviour of the as-of selection.

These are the tests the whole exercise turns on. Each one states a rule the
`sql/asof_join.sql` WHERE clause must obey, in terms an operator would use.
"""

from __future__ import annotations

import datetime as dt

from conftest import add_actual, add_forecast, asof_rows, context, local

DAY = dt.date(2026, 9, 22)
HOUR = 12  # hour ending 12:00 -> local interval [11:00, 12:00)
WINDOW = {"window_start": local(DAY, 0), "window_end": local(DAY + dt.timedelta(days=1), 0)}


def _target_and_cutoff() -> tuple[dt.datetime, dt.datetime]:
    target = local(DAY, HOUR - 1)
    return target, target - dt.timedelta(hours=24)


def _seed_actuals(connection) -> None:
    add_actual(
        connection,
        operating_date=DAY,
        hour_ending=HOUR,
        publication=local(DAY + dt.timedelta(days=1), 5, 50),
        actual_mw=1000.0,
    )
    # The seasonal-naive input, published well before the cutoff.
    add_actual(
        connection,
        operating_date=DAY - dt.timedelta(days=7),
        hour_ending=HOUR,
        publication=local(DAY - dt.timedelta(days=6), 5, 50),
        actual_mw=990.0,
    )


def test_publication_exactly_at_cutoff_is_eligible(warehouse):
    _seed_actuals(warehouse)
    _, cutoff = _target_and_cutoff()
    add_forecast(
        warehouse, operating_date=DAY, hour_ending=HOUR, publication=cutoff, forecast_mw=1111.0
    )
    (row,) = asof_rows(warehouse, context(**WINDOW))
    assert row["ercot_forecast_mw"] == 1111.0, "publication at exactly T-24h must be usable"
    assert row["ercot_vintage_age_hours"] == 0.0


def test_publication_one_second_after_cutoff_is_not_eligible(warehouse):
    _seed_actuals(warehouse)
    _, cutoff = _target_and_cutoff()
    add_forecast(
        warehouse,
        operating_date=DAY,
        hour_ending=HOUR,
        publication=cutoff + dt.timedelta(seconds=1),
        forecast_mw=1111.0,
    )
    (row,) = asof_rows(warehouse, context(**WINDOW))
    assert row["ercot_forecast_mw"] is None, "one second of hindsight is still hindsight"


def test_latest_eligible_vintage_wins(warehouse):
    _seed_actuals(warehouse)
    _, cutoff = _target_and_cutoff()
    for hours_early, value in ((72, 900.0), (48, 950.0), (1, 990.0)):
        add_forecast(
            warehouse,
            operating_date=DAY,
            hour_ending=HOUR,
            publication=cutoff - dt.timedelta(hours=hours_early),
            forecast_mw=value,
        )
    (row,) = asof_rows(warehouse, context(**WINDOW))
    assert row["ercot_forecast_mw"] == 990.0
    assert row["ercot_vintage_age_hours"] == 1.0


def test_later_vintage_is_never_substituted(warehouse):
    """The regression this pipeline exists to prevent.

    A better forecast for the same hour exists today. It was published after
    the decision cutoff, so it must not appear in a historical evaluation --
    not even as a fallback for an hour that would otherwise be missing.
    """
    _seed_actuals(warehouse)
    _, cutoff = _target_and_cutoff()
    add_forecast(
        warehouse,
        operating_date=DAY,
        hour_ending=HOUR,
        publication=cutoff - dt.timedelta(hours=2),
        forecast_mw=900.0,
    )
    add_forecast(
        warehouse,
        operating_date=DAY,
        hour_ending=HOUR,
        publication=cutoff + dt.timedelta(hours=20),
        forecast_mw=1000.0,  # indistinguishable from the actual; far more accurate
    )
    (row,) = asof_rows(warehouse, context(**WINDOW))
    assert row["ercot_forecast_mw"] == 900.0


def test_missing_forecast_yields_a_null_row_not_a_dropped_row(warehouse):
    _seed_actuals(warehouse)
    rows = asof_rows(warehouse, context(**WINDOW))
    assert len(rows) == 1, "a target hour with no eligible forecast must stay in the denominator"
    assert rows[0]["ercot_forecast_mw"] is None
    assert rows[0]["actual_mw"] == 1000.0


def test_only_the_in_use_model_is_selected(warehouse):
    _seed_actuals(warehouse)
    _, cutoff = _target_and_cutoff()
    add_forecast(
        warehouse, operating_date=DAY, hour_ending=HOUR, publication=cutoff,
        forecast_mw=1234.0, model="A3", in_use=False,
    )
    add_forecast(
        warehouse, operating_date=DAY, hour_ending=HOUR, publication=cutoff,
        forecast_mw=1111.0, model="E", in_use=True,
    )
    (row,) = asof_rows(warehouse, context(**WINDOW))
    assert row["ercot_model"] == "E"
    assert row["ercot_forecast_mw"] == 1111.0
    assert row["ercot_value_count"] == 1


def test_conflicting_values_at_the_winning_publication_are_surfaced(warehouse):
    """No winner is picked; the row is flagged for the readiness gate."""
    _seed_actuals(warehouse)
    _, cutoff = _target_and_cutoff()
    for value in (1111.0, 2222.0):
        add_forecast(
            warehouse, operating_date=DAY, hour_ending=HOUR, publication=cutoff, forecast_mw=value
        )
    (row,) = asof_rows(warehouse, context(**WINDOW))
    assert row["ercot_value_count"] == 2


def test_seasonal_naive_input_must_also_respect_the_cutoff(warehouse):
    """The subtle one: the naive model's *input* is a point-in-time value.

    The actual for T-7d exists in the warehouse, but was only published after
    T-24h. Using it would be hindsight even though the model itself is
    trivial.
    """
    _, cutoff = _target_and_cutoff()
    add_actual(
        warehouse, operating_date=DAY, hour_ending=HOUR,
        publication=local(DAY + dt.timedelta(days=1), 5, 50), actual_mw=1000.0,
    )
    add_actual(
        warehouse, operating_date=DAY - dt.timedelta(days=7), hour_ending=HOUR,
        publication=cutoff + dt.timedelta(minutes=1), actual_mw=990.0,
    )
    (row,) = asof_rows(warehouse, context(**WINDOW))
    assert row["naive_forecast_mw"] is None


def test_cutoff_is_per_target_hour_not_a_single_run_timestamp(warehouse):
    """Hour 2 and hour 23 of the same day have deadlines 21 hours apart."""
    for hour_ending in (2, 23):
        add_actual(
            warehouse, operating_date=DAY, hour_ending=hour_ending,
            publication=local(DAY + dt.timedelta(days=1), 5, 50), actual_mw=1000.0,
        )
        add_actual(
            warehouse, operating_date=DAY - dt.timedelta(days=7), hour_ending=hour_ending,
            publication=local(DAY - dt.timedelta(days=6), 5, 50), actual_mw=990.0,
        )
    # Hour 2's deadline is DAY-1 01:00; hour 23's is DAY-1 22:00. One
    # publication sits between them: in time for hour 23, far too late for
    # hour 2. A single "as of yesterday" cutoff would admit both.
    publication = local(DAY - dt.timedelta(days=1), 12)
    for hour_ending in (2, 23):
        add_forecast(
            warehouse, operating_date=DAY, hour_ending=hour_ending,
            publication=publication, forecast_mw=1111.0,
        )
    rows = {row["hour_ending"]: row for row in asof_rows(warehouse, context(**WINDOW))}
    assert rows[2]["ercot_forecast_mw"] is None
    assert rows[23]["ercot_forecast_mw"] == 1111.0
