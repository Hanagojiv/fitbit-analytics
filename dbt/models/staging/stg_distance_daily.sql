-- Thin passthrough over raw.distance_daily. Typing/renaming deliberately deferred
-- until there's a live warehouse to validate column names against -- see
-- dbt/README.md.
select * from {{ source('raw', 'distance_daily') }}
