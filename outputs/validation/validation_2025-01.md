# Validation report - 2025-01

Rows: **3,475,226** | valid: **3,216,893** (92.57%) | published: **True**

## Quality gates (any failure blocks publication)

| Gate | Observed | Threshold | Result |
|---|---|---|---|
| row_count_matches_source_footer | 3475226 | 3475226 | PASS |
| out_of_period_rate | 1e-05 | <= 0.01 | PASS |
| every_day_present | 31 | 31 | PASS |
| min_trips_per_day | 79624 | >= 10000 | PASS |
| valid_trip_rate | 0.9257 | >= 0.85 | PASS |
| crz_zone_list_matches_toll_flag | 0.9317 | >= 0.9 | PASS |

## Exclude rules (row kept in warehouse, flagged, left out of metrics)

| Rule | Meaning | Rows | % |
|---|---|---|---|
| OUT_OF_PERIOD | Pickup timestamp not in the file's month (clock errors, e.g. year 2002) | 22 | 0.001 |
| NON_POSITIVE_DURATION | Dropoff at or before pickup - timestamps cannot describe a trip | 2,051 | 0.059 |
| TOO_SHORT | Duration < 60s - meter tapped on/off, not a ride | 38,060 | 1.095 |
| TOO_LONG | Duration > 3h - meter left running | 1,377 | 0.04 |
| ZERO_DISTANCE | trip_distance <= 0 - no movement recorded | 90,893 | 2.615 |
| IMPLAUSIBLE_DISTANCE | trip_distance > 100 mi - odometer/GPS error | 162 | 0.005 |
| IMPLAUSIBLE_SPEED | Implied speed > 80 mph | 982 | 0.028 |
| NEGATIVE_FARE | fare_amount < 0 - accounting reversal/adjustment record, not a trip | 144,118 | 4.147 |
| UNKNOWN_LOCATION | Pickup or dropoff zone is 264/265 (Unknown / Outside NYC) | 28,357 | 0.816 |
| DUPLICATE | Exact duplicate of an earlier record | 0 | 0.0 |

Rules overlap, so a row can break several; the percentages do not sum to the exclusion rate.

## Warn rules (row kept in metrics; caveat recorded)

| Rule | Meaning | Rows | % |
|---|---|---|---|
| PAYMENT_TYPE_0 | payment_type 0 with null passenger_count/RatecodeID (unrecorded or flex-fare trip) | 540,149 | 15.543 |
| UNKNOWN_RATECODE | RatecodeID = 99 (unknown rate) | 41,963 | 1.207 |
| ZERO_PASSENGERS | passenger_count = 0 | 24,656 | 0.709 |
| HAS_REVERSAL | Trip has a mirror-image negative record - revenue was reversed; excluded from revenue metrics | 52,231 | 1.503 |

## CRZ zone-list cross-check against `cbd_congestion_fee`

- Intra-zone trips checked: 117,938
- Agreement between our zone list and the toll flag: **93.17%**
- Lowest toll rate for a zone in our list: 0.94
- Highest toll rate for a zone outside our list: 0.422
- Zones where list and toll flag disagree (>=200 trips): none
