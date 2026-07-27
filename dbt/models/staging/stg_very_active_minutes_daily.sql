-- Thin passthrough over raw.very_active_minutes_daily. Typing/renaming deliberately deferred
-- until there's a live warehouse to validate column names against -- see
-- dbt/README.md.
select * from {{ source('raw', 'very_active_minutes_daily') }}
