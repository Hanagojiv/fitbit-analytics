-- Thin passthrough over raw.temperature_computed. Typing/renaming deliberately deferred
-- until there's a live warehouse to validate column names against -- see
-- dbt/README.md.
select * from {{ source('raw', 'temperature_computed') }}
