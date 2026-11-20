select hour_utc, consumption_mwh
from {{ ref('features') }}
where consumption_mwh < 3000 or consumption_mwh > 16000
