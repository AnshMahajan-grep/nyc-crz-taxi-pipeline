"""Stage 1 - Ingest. Retrieve raw inputs and prove the retrieval is complete.

Retrieval modes used:
  * Files over HTTPS  - TLC monthly trip Parquet + taxi zone lookup CSV
  * REST/JSON APIs    - Open-Meteo historical weather, Nager.Date public holidays
  * SQL               - downstream stages read the modelled data back out of SQLite

Raw files are written once, never edited, and recorded in data/raw/manifest.json
(URL, retrieval time, byte size, SHA-256, row count). A rerun re-uses a raw file
only if its checksum still matches the manifest; otherwise it is re-downloaded.
"""
import calendar
import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import pyarrow.parquet as pq
import requests

from . import config
from .errors import PipelineError

log = logging.getLogger(__name__)
MANIFEST_PATH = config.RAW_DIR / "manifest.json"


# ---------------------------------------------------------------- helpers
def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_manifest() -> dict:
    if MANIFEST_PATH.exists():
        return json.loads(MANIFEST_PATH.read_text())
    return {}


def _save_manifest(manifest: dict) -> None:
    tmp = MANIFEST_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    tmp.replace(MANIFEST_PATH)


def _get_with_retry(url: str, params: dict | None = None, stream: bool = False,
                    attempts: int = 4) -> requests.Response:
    """GET with exponential backoff. Raises PipelineError after the last attempt."""
    for i in range(1, attempts + 1):
        try:
            resp = requests.get(url, params=params, stream=stream, timeout=60)
            if resp.status_code in (403, 404):  # TLC's CloudFront answers 403 for files not yet published
                raise PipelineError(f"Source not available ({resp.status_code}) - not published yet? {url}")
            resp.raise_for_status()
            return resp
        except PipelineError:
            raise
        except requests.RequestException as exc:
            if i == attempts:
                raise PipelineError(f"Could not retrieve {url} after {attempts} attempts: {exc}") from exc
            wait = 2 ** i
            log.warning("GET %s failed (attempt %d/%d): %s - retrying in %ds",
                        url, i, attempts, exc, wait)
            time.sleep(wait)
    raise AssertionError("unreachable")


def _cached(manifest: dict, key: str, path: Path, force: bool) -> bool:
    """True if a previously retrieved raw file is present and unmodified."""
    entry = manifest.get(key)
    if force or entry is None or not path.exists():
        return False
    if _sha256(path) != entry["sha256"]:
        log.warning("Checksum mismatch for %s - raw file was altered, re-downloading", path.name)
        return False
    log.info("Raw input %s already retrieved at %s (checksum OK) - reusing",
             path.name, entry["retrieved_at"])
    return True


def _record(manifest: dict, key: str, path: Path, url: str, **extra) -> None:
    manifest[key] = {
        "file": path.name,
        "url": url,
        "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
        **extra,
    }
    _save_manifest(manifest)


# ---------------------------------------------------------------- file sources
def fetch_trip_file(month: str, force: bool = False) -> Path:
    """Download one monthly yellow-taxi Parquet file and verify it is complete."""
    manifest = _load_manifest()
    key = f"yellow_tripdata_{month}"
    path = config.RAW_DIR / f"{key}.parquet"
    if _cached(manifest, key, path, force):
        return path

    url = config.TRIP_URL.format(month=month)
    log.info("Downloading %s", url)
    resp = _get_with_retry(url, stream=True)
    expected = int(resp.headers.get("Content-Length", 0))
    tmp = path.with_suffix(".part")
    with open(tmp, "wb") as f:
        for chunk in resp.iter_content(1 << 20):
            f.write(chunk)
    got = tmp.stat().st_size
    # Completeness check 1: every byte the server advertised arrived.
    if expected and got != expected:
        tmp.unlink()
        raise PipelineError(f"Truncated download for {month}: {got} of {expected} bytes")
    # Completeness check 2: the Parquet footer is readable (a partial file has no footer).
    try:
        meta = pq.ParquetFile(tmp).metadata
    except Exception as exc:
        tmp.unlink()
        raise PipelineError(f"Downloaded file for {month} is not valid Parquet") from exc
    tmp.replace(path)
    _record(manifest, key, path, url, content_length=expected, parquet_rows=meta.num_rows)
    log.info("Saved %s: %s bytes, %s rows (from Parquet footer)", path.name, got, meta.num_rows)
    return path


def fetch_zone_lookup(force: bool = False) -> Path:
    manifest = _load_manifest()
    key = "taxi_zone_lookup"
    path = config.RAW_DIR / "taxi_zone_lookup.csv"
    if _cached(manifest, key, path, force):
        return path
    resp = _get_with_retry(config.ZONE_URL)
    path.write_bytes(resp.content)
    n_rows = resp.text.strip().count("\n")  # minus header
    if n_rows < 263:
        raise PipelineError(f"Zone lookup looks incomplete: {n_rows} rows (expected 265)")
    _record(manifest, key, path, config.ZONE_URL, rows=n_rows)
    log.info("Saved zone lookup: %d zones", n_rows)
    return path


# ---------------------------------------------------------------- API sources
def fetch_weather(month: str, force: bool = False) -> Path:
    """Daily weather for the month from Open-Meteo (JSON API). Raw response saved verbatim."""
    manifest = _load_manifest()
    key = f"weather_{month}"
    path = config.RAW_DIR / f"{key}.json"
    if _cached(manifest, key, path, force):
        return path
    year, mon = map(int, month.split("-"))
    last_day = calendar.monthrange(year, mon)[1]
    params = {**config.WEATHER_PARAMS,
              "start_date": f"{month}-01", "end_date": f"{month}-{last_day:02d}"}
    resp = _get_with_retry(config.WEATHER_URL, params=params)
    body = resp.json()
    days = body.get("daily", {}).get("time", [])
    # Completeness check: one record per calendar day.
    if len(days) != last_day:
        raise PipelineError(f"Weather API returned {len(days)} days for {month}, expected {last_day}")
    path.write_text(json.dumps(body, indent=2))
    _record(manifest, key, path, resp.url, rows=len(days))
    log.info("Saved weather for %s: %d days", month, len(days))
    return path


def fetch_holidays(year: int, force: bool = False) -> Path:
    manifest = _load_manifest()
    key = f"holidays_{year}"
    path = config.RAW_DIR / f"{key}.json"
    if _cached(manifest, key, path, force):
        return path
    url = config.HOLIDAY_URL.format(year=year)
    resp = _get_with_retry(url)
    body = resp.json()
    if not isinstance(body, list) or len(body) < 8:
        raise PipelineError(f"Holiday API returned an unexpected payload for {year}")
    path.write_text(json.dumps(body, indent=2))
    _record(manifest, key, path, url, rows=len(body))
    log.info("Saved %d US public holidays for %d", len(body), year)
    return path


def ingest_month(month: str, force: bool = False) -> dict:
    """Retrieve every raw input needed for one month. Returns their paths."""
    return {
        "trips": fetch_trip_file(month, force),
        "zones": fetch_zone_lookup(force),
        "weather": fetch_weather(month, force),
        "holidays": fetch_holidays(int(month[:4]), force),
    }


def manifest_entry(key: str) -> dict:
    return _load_manifest()[key]
