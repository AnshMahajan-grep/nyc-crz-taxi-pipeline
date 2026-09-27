# Workflow & data model (Class 7)

## 1. The operational workflow of a taxi trip

```mermaid
flowchart LR
  A([Street hail / request<br/><i>not recorded</i>]) --> B[Pickup<br/>tpep_pickup_datetime<br/>PULocationID]
  B --> C[In-trip<br/>distance, duration, route<br/><i>route not recorded</i>]
  C --> D[Dropoff<br/>tpep_dropoff_datetime<br/>DOLocationID]
  D --> E[Payment<br/>payment_type, fare, tip,<br/>tolls, cbd_congestion_fee]
  E -. dispute / void .-> F[Reversal record<br/>negative mirror row]
  P{{Intervention:<br/>CRZ toll from 2025-01-05}} -. affects traffic in .-> C
  X{{External factors:<br/>weather, holidays}} -. affect .-> C
```

| Element | In our model |
|---|---|
| **Entities** | Trip (fact_trip), Zone (dim_zone), Day (dim_date). Vendor and payment type are attributes on the trip |
| **Events / states** | pickup → dropoff → payment → (optional) reversal. A record's data-quality state is `is_valid` + `dq_flags` / `warn_flags` |
| **Interventions** | CRZ congestion toll (`dim_date.is_toll_live`, zone `is_crz`) |
| **External factors (confounders)** | Weather (`is_wet_day`, snowfall), holidays (`is_holiday`), weekend |
| **Outcomes** | In-trip speed, duration tail (p90), fare per engaged hour, trip volume |
| **Comparison group** | Trips wholly inside Manhattan north of the CRZ (`segment = manhattan_non_crz`) |

## 2. Relational (star) model - SQLite `warehouse/tlc.sqlite`

```mermaid
erDiagram
  dim_zone ||--o{ fact_trip : "pickup zone (pu_zone_id)"
  dim_zone ||--o{ fact_trip : "dropoff zone (do_zone_id)"
  dim_date ||--o{ fact_trip : "pickup day (date_key)"
  fact_trip }o--|| metrics_monthly : "aggregated into"

  dim_zone {
    int zone_id PK
    text borough
    text zone
    text service_zone
    int is_crz "assumed list, validated vs toll flag"
    int is_unknown "264/265"
  }
  dim_date {
    int date_key PK "yyyymmdd"
    text date
    text year_month
    int day_of_week
    int is_weekend
    int is_holiday "Nager API, national only"
    real precip_mm "Open-Meteo"
    real snowfall_cm
    real temp_mean_c
    int is_wet_day ">= 1 mm"
    int is_toll_live ">= 2025-01-05"
  }
  fact_trip {
    int trip_id PK "yyyymm*1e8 + source row"
    text year_month "partition key"
    int vendor_id
    text pickup_ts
    int date_key FK
    int pickup_hour
    int pu_zone_id FK
    int do_zone_id FK
    text segment "crz_internal|crz_inbound|crz_outbound|manhattan_non_crz|other"
    int payment_type
    real distance_mi
    real duration_s
    real speed_mph
    real fare_amount
    real tip_amount
    real cbd_fee "NULL before 2025"
    int dq_flags "bitmask of exclude rules"
    int warn_flags "bitmask of warn rules"
    int is_valid
  }
  metrics_monthly {
    text year_month PK
    text metric PK
    real value
    int n_trips
    text computed_at
  }
```

**Grain decisions**
- `fact_trip` keeps **one row per source record, including invalid ones**. Nothing is deleted, and every exclusion is visible as a flag and traceable to its source row through `trip_id`.
- `segment` is derived from the pickup and dropoff zones: `crz_internal` (both ends in the CRZ), `crz_inbound`, `crz_outbound`, `manhattan_non_crz` (the control group), and `other`.
- The KPI population is `segment = 'crz_internal'`, valid, non-holiday weekday, pickup 05:00–20:59 (the toll peak window).

## 3. Metrics linked to the KPI

| Metric | Formula (SQL) | Role |
|---|---|---|
| valid_trip_rate | `SUM(is_valid) / COUNT(*)` for the month | Trust gate |
| **crz_median_speed_mph** | median(`speed_mph`) over the KPI population | **Project KPI** |
| control_median_speed_mph | same, `segment = 'manhattan_non_crz'` | Counterfactual |
| crz_p90_duration_min | p90(`duration_s`)/60 over the KPI population | Reliability |
| crz_fare_per_engaged_hour | `SUM(fare_amount) / SUM(duration_s/3600)`, reversal twins excluded | Driver economics |
| crz_touching_trips_per_weekday | valid trips in the 3 CRZ segments / working weekdays | Demand |
| crz_median_speed_dry_days | KPI with `is_wet_day = 0` | Sensitivity (weather) |
| crz_median_speed_excl_pt0 | KPI excluding `payment_type 0` | Sensitivity (data-mix shift) |
