-- Thin passthrough over raw.gh_cardio_load_daily. Typing/renaming deliberately deferred
-- until there's a live warehouse to validate column names against -- see
-- dbt/README.md.
select * from {{ source('raw', 'gh_cardio_load_daily') }}
