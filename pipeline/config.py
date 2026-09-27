"""Central configuration: sources, paths, business definitions and quality gates.

Everything a reviewer might want to challenge (thresholds, zone lists, toll hours)
lives here so it is explicit rather than buried in code.
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
PROCESSED_DIR = ROOT / "data" / "processed"
# Override with TLC_WAREHOUSE=<path> to keep the ~1 GB SQLite file out of synced folders (OneDrive etc.).
WAREHOUSE_PATH = Path(os.environ.get("TLC_WAREHOUSE", ROOT / "warehouse" / "tlc.sqlite"))
OUTPUT_DIR = ROOT / "outputs"
LOG_DIR = ROOT / "logs"

# Default analysis: same calendar month, one year apart (controls for seasonality).
# Congestion pricing (CRZ toll) started 2025-01-05.
DEFAULT_MONTHS = ["2024-01", "2025-01"]
CRZ_START_DATE = "2025-01-05"

# ---------------------------------------------------------------- sources
TRIP_URL = "https://d37ci6vzurychx.cloudfront.net/trip-data/yellow_tripdata_{month}.parquet"
ZONE_URL = "https://d37ci6vzurychx.cloudfront.net/misc/taxi_zone_lookup.csv"
WEATHER_URL = "https://archive-api.open-meteo.com/v1/archive"
WEATHER_PARAMS = {
    "latitude": 40.7831,  # Central Park, Manhattan
    "longitude": -73.9712,
    "daily": "precipitation_sum,snowfall_sum,temperature_2m_mean",
    "timezone": "America/New_York",
}
HOLIDAY_URL = "https://date.nager.at/api/v3/PublicHolidays/{year}/US"

# Columns we actually need from the trip file (TLC data dictionary names).
TRIP_COLUMNS = [
    "VendorID",
    "tpep_pickup_datetime",
    "tpep_dropoff_datetime",
    "passenger_count",
    "trip_distance",
    "RatecodeID",
    "PULocationID",
    "DOLocationID",
    "payment_type",
    "fare_amount",
    "tip_amount",
    "total_amount",
]
# Only present from 2025-01 onward; used to cross-check our CRZ zone definition.
OPTIONAL_TRIP_COLUMNS = ["cbd_congestion_fee"]

# ---------------------------------------------------------------- business definitions
# Congestion Relief Zone = Manhattan at or below 60th St. TLC zones do not carry a CRZ flag,
# so this list is an ASSUMPTION built from zone geography, then checked empirically
# against cbd_congestion_fee in 2025 data (see validate.check_crz_definition).
# Zones that straddle 60th St (Lincoln Sq E/W 142/143, Lenox Hill E/W 140/141, Central Park 43)
# are excluded because most of their area lies north of the boundary.
CRZ_ZONE_IDS = {
    4, 12, 13, 45, 48, 50, 68, 79, 87, 88, 90, 100, 107, 113, 114, 125, 137, 144,
    148, 158, 161, 162, 163, 164, 170, 186, 209, 211, 224, 229, 230, 231, 232, 233,
    234, 246, 249, 261,
}
# 264 = "Unknown", 265 = "Outside of NYC" / N/A in the zone lookup.
UNKNOWN_ZONE_IDS = {264, 265}

# CRZ peak toll hours: weekdays 05:00-21:00, weekends 09:00-21:00 (local time).
PEAK_WEEKDAY_HOURS = range(5, 21)
PEAK_WEEKEND_HOURS = range(9, 21)

# ---------------------------------------------------------------- validation thresholds
MIN_DURATION_S = 60           # < 1 minute: meter tapped on/off, not a real trip
MAX_DURATION_S = 3 * 3600     # > 3 hours: meter left running
MAX_DISTANCE_MI = 100         # NYC-origin yellow trips beyond this are implausible
MAX_SPEED_MPH = 80            # physically implausible in NYC traffic
DAYS_WITH_PRECIP_MM = 1.0     # "wet day" threshold for the weather sensitivity check

# ---------------------------------------------------------------- quality gates (fail the run)
MIN_VALID_RATE = 0.85         # if < 85% of trips pass validation, do not publish metrics
MAX_OUT_OF_MONTH_RATE = 0.01  # >1% of trips outside the file month => wrong/corrupt file
MIN_TRIPS_PER_DAY = 10_000    # a day below this in a yellow-cab month suggests missing data
CRZ_FEE_AGREEMENT_MIN = 0.90  # our zone list must agree with the toll flag >=90% of the time
