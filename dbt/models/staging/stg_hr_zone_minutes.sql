-- Thin passthrough over raw.hr_zone_minutes. Typing/renaming deliberately deferred
-- until there's a live warehouse to validate column names against -- see
-- dbt/README.md.
select * from {{ source('raw', 'hr_zone_minutes') }}
