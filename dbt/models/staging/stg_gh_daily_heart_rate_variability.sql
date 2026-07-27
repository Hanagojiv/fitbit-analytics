-- Thin passthrough over raw.gh_daily_heart_rate_variability. Typing/renaming deliberately deferred
-- until there's a live warehouse to validate column names against -- see
-- dbt/README.md.
select * from {{ source('raw', 'gh_daily_heart_rate_variability') }}
