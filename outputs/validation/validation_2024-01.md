# Validation report - 2024-01

Rows: **2,964,624** | valid: **2,831,623** (95.51%) | published: **True**

## Quality gates (any failure blocks publication)

| Gate | Observed | Threshold | Result |
|---|---|---|---|
| row_count_matches_source_footer | 2964624 | 2964624 | PASS |
| out_of_period_rate | 1e-05 | <= 0.01 | PASS |
| every_day_present | 31 | 31 | PASS |
| min_trips_per_day | 67543 | >= 10000 | PASS |
| valid_trip_rate | 0.9551 | >= 0.85 | PASS |

## Exclude rules (row kept in warehouse, flagged, left out of metrics)

| Rule | Meaning | Rows | % |
|---|---|---|---|
| OUT_OF_PERIOD | Pickup timestamp not in the file's month (clock errors, e.g. year 2002) | 18 | 0.001 |
| NON_POSITIVE_DURATION | Dropoff at or before pickup - timestamps cannot describe a trip | 870 | 0.029 |
| TOO_SHORT | Duration < 60s - meter tapped on/off, not a ride | 34,251 | 1.155 |
| TOO_LONG | Duration > 3h - meter left running | 1,983 | 0.067 |
| ZERO_DISTANCE | trip_distance <= 0 - no movement recorded | 60,371 | 2.036 |
| IMPLAUSIBLE_DISTANCE | trip_distance > 100 mi - odometer/GPS error | 59 | 0.002 |
| IMPLAUSIBLE_SPEED | Implied speed > 80 mph | 1,107 | 0.037 |
| NEGATIVE_FARE | fare_amount < 0 - accounting reversal/adjustment record, not a trip | 37,448 | 1.263 |
| UNKNOWN_LOCATION | Pickup or dropoff zone is 264/265 (Unknown / Outside NYC) | 31,527 | 1.063 |
| DUPLICATE | Exact duplicate of an earlier record | 0 | 0.0 |

Rules overlap, so a row can break several; the percentages do not sum to the exclusion rate.

## Warn rules (row kept in metrics; caveat recorded)

| Rule | Meaning | Rows | % |
|---|---|---|---|
| PAYMENT_TYPE_0 | payment_type 0 with null passenger_count/RatecodeID (unrecorded or flex-fare trip) | 140,162 | 4.728 |
| UNKNOWN_RATECODE | RatecodeID = 99 (unknown rate) | 28,663 | 0.967 |
| ZERO_PASSENGERS | passenger_count = 0 | 31,465 | 1.061 |
| HAS_REVERSAL | Trip has a mirror-image negative record - revenue was reversed; excluded from revenue metrics | 30,980 | 1.045 |
