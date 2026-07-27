{% macro daily_facts_models() %}
{#- Static list of staging models to join into fct_daily_facts, generated
   from what's actually in models/staging/ -- kept static rather than
   discovered from `graph` at compile time because get_columns_in_relation()
   needs `execute` to be true anyway (see below), and a plain list is far
   easier to reason about than runtime graph introspection. Regenerate by
   listing models/staging/*.sql if models are added or removed. #}
  {%- set models = [
    "stg_azm",
    "stg_calories_daily",
    "stg_distance_daily",
    "stg_gh_active_energy_burned_daily",
    "stg_gh_active_zone_minutes_daily",
    "stg_gh_activity_level_daily",
    "stg_gh_body_temperature_daily",
    "stg_gh_calories_daily",
    "stg_gh_calories_in_heart_rate_zone_daily",
    "stg_gh_cardio_acute_chronic_workload_ratio",
    "stg_gh_cardio_load_daily",
    "stg_gh_cardio_load_observed_interval",
    "stg_gh_daily_heart_rate_variability",
    "stg_gh_daily_oxygen_saturation",
    "stg_gh_daily_readiness",
    "stg_gh_daily_respiratory_rate",
    "stg_gh_daily_resting_heart_rate",
    "stg_gh_daily_sleep_temperature_derivations",
    "stg_gh_distance_daily",
    "stg_gh_heart_rate_daily",
    "stg_gh_heart_rate_variability",
    "stg_gh_height",
    "stg_gh_oxygen_saturation_daily",
    "stg_gh_speed_daily",
    "stg_gh_steps_daily",
    "stg_gh_time_in_heart_rate_zone_daily",
    "stg_gh_weight",
    "stg_heart_rate_daily",
    "stg_hr_zone_minutes",
    "stg_hrv_daily",
    "stg_lightly_active_minutes_daily",
    "stg_moderately_active_minutes_daily",
    "stg_respiratory_rate",
    "stg_resting_heart_rate",
    "stg_sedentary_minutes_daily",
    "stg_sleep_daily",
    "stg_sleep_score",
    "stg_spo2_daily",
    "stg_steps_daily",
    "stg_stress_score",
    "stg_temperature_computed",
    "stg_very_active_minutes_daily",
    "stg_vo2max",
    "stg_wear_coverage"
  ] -%}
  {{ return(models) }}
{% endmacro %}


{% macro build_daily_facts() %}
{#- Mirrors transform.build_daily_facts in Python: a continuous date spine,
   left-joined against every daily table, with automatic column-collision
   suffixing since two sources (old Takeout format vs. new "Google Health"
   format, or a future third source) can legitimately produce the same
   column name. See CLAUDE.md "Column collisions across silver tables". #}
  {%- set model_names = daily_facts_models() -%}

  {%- set select_parts = ["spine.date"] -%}
  {%- set join_parts = [] -%}
  {%- set seen_columns = ["date"] -%}

  {%- for model_name in model_names -%}
    {%- set relation = ref(model_name) -%}
    {%- set alias = "t" ~ loop.index -%}
    {%- if execute -%}
      {%- set columns = adapter.get_columns_in_relation(relation) -%}
    {%- else -%}
      {%- set columns = [] -%}
    {%- endif -%}
    {%- for col in columns -%}
      {%- if col.column|lower != 'date' -%}
        {%- if col.column in seen_columns -%}
          {%- set out_name = col.column ~ "__" ~ model_name -%}
        {%- else -%}
          {%- set out_name = col.column -%}
        {%- endif -%}
        {%- do seen_columns.append(out_name) -%}
        {%- do select_parts.append(alias ~ "." ~ adapter.quote(col.column) ~ " as " ~ adapter.quote(out_name)) -%}
      {%- endif -%}
    {%- endfor -%}
    {%- do join_parts.append("left join " ~ relation ~ " as " ~ alias ~ " on " ~ alias ~ ".\"date\" = spine.date") -%}
  {%- endfor -%}

select
  {{ select_parts | join(",\n  ") }}
from spine
{{ join_parts | join("\n") }}
{% endmacro %}
