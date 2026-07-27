-- Thin passthrough over raw.resting_heart_rate. Typing/renaming deliberately deferred
-- until there's a live warehouse to validate column names against -- see
-- dbt/README.md.
select * from {{ source('raw', 'resting_heart_rate') }}
