-- Thin passthrough over raw.sedentary_minutes_daily. Typing/renaming deliberately deferred
-- until there's a live warehouse to validate column names against -- see
-- dbt/README.md.
select * from {{ source('raw', 'sedentary_minutes_daily') }}
