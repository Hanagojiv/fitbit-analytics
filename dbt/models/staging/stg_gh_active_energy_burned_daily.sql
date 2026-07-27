-- Thin passthrough over raw.gh_active_energy_burned_daily. Typing/renaming deliberately deferred
-- until there's a live warehouse to validate column names against -- see
-- dbt/README.md.
select * from {{ source('raw', 'gh_active_energy_burned_daily') }}
