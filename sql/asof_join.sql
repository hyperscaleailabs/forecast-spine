-- As-of selection: the point-in-time core of this pipeline.
--
-- Invariant enforced here, and nowhere relaxed downstream:
--
--     Nothing used to produce a forecast for target hour T may have become
--     available after T - :cutoff_lag_hours.
--
-- Three things are selected as-of, each against a *different* clock:
--
--   ercot forecast   publication_ts <= target_ts - cutoff_lag   (decision time)
--   naive input      publication_ts <= target_ts - cutoff_lag   (decision time)
--   actual truth     publication_ts <= processing_ts            (evaluation time)
--
-- The cutoff is per target hour, not one global timestamp: the forecast for
-- 03:00 and the forecast for 22:00 have different deadlines. Collapsing them
-- to a single "as of yesterday" timestamp is the most common way this goes
-- quietly wrong.
--
-- Ties are never broken arbitrarily. Where two rows survive at the same
-- winning publication timestamp with different values, the row is emitted
-- with value_count > 1 and the readiness gate refuses the release.
--
-- Parameters: $cutoff_lag_hours, $window_start, $window_end, $processing_ts,
--             $seasonal_lag_days

WITH params AS (
    SELECT
        CAST($cutoff_lag_hours AS INTEGER)        AS cutoff_lag_hours,
        CAST($seasonal_lag_days AS INTEGER)       AS seasonal_lag_days,
        CAST($window_start AS TIMESTAMPTZ)        AS window_start,
        CAST($window_end AS TIMESTAMPTZ)          AS window_end,
        CAST($processing_ts AS TIMESTAMPTZ)       AS processing_ts
),

-- Every target hour we intend to judge, taken from the actuals we hold.
targets AS (
    SELECT DISTINCT
        a.weather_zone,
        a.target_ts_utc,
        a.operating_date,
        a.hour_ending,
        a.dst_flag
    FROM actual_vintage a, params p
    WHERE a.weather_zone <> 'SYSTEM_TOTAL'
      AND a.target_ts_utc >= p.window_start
      AND a.target_ts_utc <  p.window_end
),

----------------------------------------------------------------------------
-- 1. ERCOT's own forecast, as it stood at the decision cutoff
----------------------------------------------------------------------------
eligible_forecast AS (
    SELECT
        f.weather_zone,
        f.target_ts_utc,
        f.publication_ts_utc,
        f.forecast_mw,
        f.model
    FROM forecast_vintage f, params p
    WHERE f.weather_zone <> 'SYSTEM_TOTAL'
      AND f.is_ercot_model_in_use
      -- The whole exercise, in one predicate.
      AND f.publication_ts_utc <= f.target_ts_utc - (p.cutoff_lag_hours * INTERVAL 1 HOUR)
),
latest_forecast_publication AS (
    SELECT weather_zone, target_ts_utc, MAX(publication_ts_utc) AS publication_ts_utc
    FROM eligible_forecast
    GROUP BY weather_zone, target_ts_utc
),
asof_forecast AS (
    SELECT
        l.weather_zone,
        l.target_ts_utc,
        l.publication_ts_utc                 AS forecast_publication_ts_utc,
        MIN(e.forecast_mw)                   AS forecast_mw,
        COUNT(DISTINCT e.forecast_mw)        AS forecast_value_count,
        MIN(e.model)                         AS forecast_model,
        COUNT(DISTINCT e.model)              AS forecast_model_count
    FROM latest_forecast_publication l
    JOIN eligible_forecast e
      ON e.weather_zone = l.weather_zone
     AND e.target_ts_utc = l.target_ts_utc
     AND e.publication_ts_utc = l.publication_ts_utc
    GROUP BY l.weather_zone, l.target_ts_utc, l.publication_ts_utc
),

----------------------------------------------------------------------------
-- 2. Reported actuals, as evaluation truth (known only after the hour)
----------------------------------------------------------------------------
eligible_actual AS (
    SELECT a.weather_zone, a.target_ts_utc, a.publication_ts_utc, a.actual_mw
    FROM actual_vintage a, params p
    WHERE a.weather_zone <> 'SYSTEM_TOTAL'
      AND a.publication_ts_utc <= p.processing_ts
),
latest_actual_publication AS (
    SELECT weather_zone, target_ts_utc, MAX(publication_ts_utc) AS publication_ts_utc
    FROM eligible_actual
    GROUP BY weather_zone, target_ts_utc
),
asof_actual AS (
    SELECT
        l.weather_zone,
        l.target_ts_utc,
        l.publication_ts_utc          AS actual_publication_ts_utc,
        MIN(e.actual_mw)              AS actual_mw,
        COUNT(DISTINCT e.actual_mw)   AS actual_value_count
    FROM latest_actual_publication l
    JOIN eligible_actual e
      ON e.weather_zone = l.weather_zone
     AND e.target_ts_utc = l.target_ts_utc
     AND e.publication_ts_utc = l.publication_ts_utc
    GROUP BY l.weather_zone, l.target_ts_utc, l.publication_ts_utc
),

----------------------------------------------------------------------------
-- 3. Seasonal-naive input: the same local hour, seven operating days back
--
-- Joined on (operating_date - 7, hour_ending, dst_flag) rather than
-- (target_ts - 168 hours). Seven calendar days at the same local hour is 167
-- or 169 hours across a DST boundary, so arithmetic on the UTC instant would
-- silently pick the wrong hour twice a year.
--
-- The source actual must itself have been publishable by the *forecast's*
-- cutoff. Using the value we know today would be hindsight.
----------------------------------------------------------------------------
eligible_naive AS (
    SELECT
        t.weather_zone,
        t.target_ts_utc,
        a.target_ts_utc      AS naive_source_ts_utc,
        a.publication_ts_utc AS naive_source_publication_ts_utc,
        a.actual_mw          AS naive_forecast_mw
    FROM targets t
    JOIN params p ON TRUE
    JOIN actual_vintage a
      ON a.weather_zone  = t.weather_zone
     AND a.operating_date = t.operating_date - (p.seasonal_lag_days * INTERVAL 1 DAY)
     AND a.hour_ending   = t.hour_ending
     AND a.dst_flag      = t.dst_flag
    WHERE a.publication_ts_utc <= t.target_ts_utc - (p.cutoff_lag_hours * INTERVAL 1 HOUR)
),
latest_naive_publication AS (
    SELECT weather_zone, target_ts_utc, MAX(naive_source_publication_ts_utc) AS pub
    FROM eligible_naive
    GROUP BY weather_zone, target_ts_utc
),
asof_naive AS (
    SELECT
        l.weather_zone,
        l.target_ts_utc,
        l.pub                                 AS naive_source_publication_ts_utc,
        MIN(e.naive_source_ts_utc)            AS naive_source_ts_utc,
        MIN(e.naive_forecast_mw)              AS naive_forecast_mw,
        COUNT(DISTINCT e.naive_forecast_mw)   AS naive_value_count
    FROM latest_naive_publication l
    JOIN eligible_naive e
      ON e.weather_zone = l.weather_zone
     AND e.target_ts_utc = l.target_ts_utc
     AND e.naive_source_publication_ts_utc = l.pub
    GROUP BY l.weather_zone, l.target_ts_utc, l.pub
)

----------------------------------------------------------------------------
-- 4. The evaluation dataset
--
-- LEFT JOINs on purpose: a target hour with no eligible forecast must appear
-- as an explicit NULL that the readiness gate can count, never vanish from
-- the denominator and never be back-filled from a later vintage.
----------------------------------------------------------------------------
SELECT
    t.weather_zone,
    t.target_ts_utc,
    t.operating_date,
    t.hour_ending,
    t.dst_flag,
    t.target_ts_utc - (p.cutoff_lag_hours * INTERVAL 1 HOUR) AS cutoff_ts_utc,

    f.forecast_publication_ts_utc AS ercot_publication_ts_utc,
    f.forecast_mw                 AS ercot_forecast_mw,
    f.forecast_model              AS ercot_model,
    COALESCE(f.forecast_value_count, 0) AS ercot_value_count,
    COALESCE(f.forecast_model_count, 0) AS ercot_model_count,
    -- How stale the newest publishable forecast was at its own deadline.
    -- NP3-565 publishes hourly, so a healthy value is under ~1 hour; a large
    -- value means acquisition missed vintages, not that ERCOT was late.
    date_diff(
        'minute',
        f.forecast_publication_ts_utc,
        t.target_ts_utc - (p.cutoff_lag_hours * INTERVAL 1 HOUR)
    ) / 60.0 AS ercot_vintage_age_hours,

    a.actual_publication_ts_utc,
    a.actual_mw,
    COALESCE(a.actual_value_count, 0) AS actual_value_count,

    n.naive_source_ts_utc,
    n.naive_source_publication_ts_utc,
    n.naive_forecast_mw,
    COALESCE(n.naive_value_count, 0) AS naive_value_count,

    f.forecast_mw - a.actual_mw AS ercot_error_mw,
    n.naive_forecast_mw - a.actual_mw AS naive_error_mw
FROM targets t
JOIN params p ON TRUE
LEFT JOIN asof_forecast f
       ON f.weather_zone = t.weather_zone AND f.target_ts_utc = t.target_ts_utc
LEFT JOIN asof_actual a
       ON a.weather_zone = t.weather_zone AND a.target_ts_utc = t.target_ts_utc
LEFT JOIN asof_naive n
       ON n.weather_zone = t.weather_zone AND n.target_ts_utc = t.target_ts_utc
ORDER BY t.target_ts_utc, t.weather_zone
