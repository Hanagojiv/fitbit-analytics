-- One row per calendar day, every daily-grain source left-joined onto a
-- continuous date spine. Mirrors transform.build_daily_facts in the local
-- Python pipeline; see dbt/macros/daily_facts.sql for the join generation
-- and CLAUDE.md for why column collisions across sources are expected, not
-- a bug, and handled automatically rather than guessed around.

with bounds as (
    {% set model_names = daily_facts_models() %}
    select min(lo) as lo, max(hi) as hi
    from (
        {% for model_name in model_names %}
        select min(date) as lo, max(date) as hi from {{ ref(model_name) }}
        {% if not loop.last %}union all{% endif %}
        {% endfor %}
    ) b
),

spine as (
    select generate_series(bounds.lo, bounds.hi, interval '1 day')::date as date
    from bounds
)

{{ build_daily_facts() }}
