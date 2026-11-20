with normalized as (
    select
        lower(place) as place,
        timezone('UTC', observed_at) as observed_at_utc,
        try_cast(temperature_c as double) as temperature_c,
        row_number() over (
            partition by lower(place), observed_at
            order by temperature_c desc nulls last
        ) as duplicate_rank
    from {{ source('raw', 'fmi_weather') }}
    where lower(parameter) = 't2m'
)
select
    place,
    timezone('UTC', observed_at_utc) as hour_utc,
    temperature_c
from normalized
where duplicate_rank = 1
