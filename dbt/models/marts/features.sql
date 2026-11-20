with consumption as (
    select hour_utc, consumption_mwh
    from {{ ref('stg_consumption_hourly') }}
),
hour_bounds as (
    select min(hour_utc) as min_hour, max(hour_utc) as max_hour
    from consumption
),
hour_spine as (
    select unnest(generate_series(min_hour, max_hour, interval '1 hour')) as hour_utc
    from hour_bounds
),
filled_consumption as (
    select
        spine.hour_utc,
        coalesce(
            current_hour.consumption_mwh,
            last_value(current_hour.consumption_mwh ignore nulls) over (
                order by spine.hour_utc
                rows between unbounded preceding and 1 preceding
            )
        ) as consumption_mwh,
        current_hour.consumption_mwh is null as consumption_imputed
    from hour_spine as spine
    left join consumption as current_hour using (hour_utc)
),
weather_weights as (
    select place, population
    from {{ ref('population_weights') }}
),
weather_lagged as (
    select
        weather.hour_utc + interval '24 hours' as hour_utc,
        sum(weather.temperature_c * weights.population)
            / nullif(sum(weights.population), 0) as population_weighted_temperature_c
    from {{ ref('stg_fmi_weather_hourly') }} as weather
    inner join weather_weights as weights using (place)
    where weather.temperature_c is not null
    group by 1
),
calendar as (
    select
        filled.*,
        timezone('Europe/Helsinki', filled.hour_utc) as local_time,
        weather.population_weighted_temperature_c
    from filled_consumption as filled
    left join weather_lagged as weather using (hour_utc)
),
features as (
    select
        calendar.hour_utc,
        calendar.consumption_mwh,
        calendar.consumption_imputed,
        lag(consumption_mwh, 24) over ordered_hours as consumption_lag_24h,
        lag(consumption_mwh, 48) over ordered_hours as consumption_lag_48h,
        lag(consumption_mwh, 168) over ordered_hours as consumption_lag_168h,
        avg(consumption_mwh) over (
            ordered_hours rows between 24 preceding and 1 preceding
        ) as consumption_mean_24h,
        avg(consumption_mwh) over (
            ordered_hours rows between 168 preceding and 1 preceding
        ) as consumption_mean_168h,
        extract(hour from local_time)::integer as hour_of_day,
        extract(isodow from local_time)::integer as weekday,
        extract(month from local_time)::integer as month,
        holiday.holiday_date is not null as is_finnish_holiday,
        date_diff(
            'hour',
            timezone('UTC', calendar.hour_utc),
            timezone('Europe/Helsinki', calendar.hour_utc)
        ) = 3 as is_dst,
        calendar.population_weighted_temperature_c,
        greatest(0, 18 - calendar.population_weighted_temperature_c)
            as heating_degree_hours
    from calendar
    left join {{ ref('finnish_holidays') }} as holiday
        on cast(local_time as date) = holiday.holiday_date
    window ordered_hours as (order by calendar.hour_utc)
)
select * from features
