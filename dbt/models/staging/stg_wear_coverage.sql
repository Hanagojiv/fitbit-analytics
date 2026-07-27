-- Thin passthrough over raw.wear_coverage. Typing/renaming deliberately deferred
-- until there's a live warehouse to validate column names against -- see
-- dbt/README.md.
select * from {{ source('raw', 'wear_coverage') }}
