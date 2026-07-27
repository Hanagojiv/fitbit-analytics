-- Thin passthrough over raw.gh_calories_in_heart_rate_zone_daily. Typing/renaming deliberately deferred
-- until there's a live warehouse to validate column names against -- see
-- dbt/README.md.
select * from {{ source('raw', 'gh_calories_in_heart_rate_zone_daily') }}
