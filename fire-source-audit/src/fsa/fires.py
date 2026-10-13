"""Load the FireSET fire list (SUBSET_expanded_*.xlsx) into Fire objects."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from .normalize import norm_irwin, norm_ufi, to_utc, valid_latlon


@dataclass
class Fire:
    key: str
    name: str
    year: int
    objectid: Any = None
    irwin_id: str | None = None
    unique_fire_id: str | None = None
    unit: str | None = None
    gis_acres: float | None = None
    ign_lat: float | None = None
    ign_lon: float | None = None
    lon_min: float | None = None
    lat_min: float | None = None
    lon_max: float | None = None
    lat_max: float | None = None
    start_utc: pd.Timestamp | None = None
    start_precision: str | None = None
    end_utc: pd.Timestamp | None = None
    first_detection_utc: pd.Timestamp | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def center(self) -> tuple[float, float]:
        if None not in (self.lon_min, self.lon_max, self.lat_min, self.lat_max):
            return (self.lon_min + self.lon_max) / 2, (self.lat_min + self.lat_max) / 2
        if self.ign_lon is not None:
            return self.ign_lon, self.ign_lat
        raise ValueError(f"fire {self.key} has no location")

    def envelope(self, buffer_deg: float = 0.0) -> tuple[float, float, float, float]:
        if None in (self.lon_min, self.lat_min, self.lon_max, self.lat_max):
            lon, lat = self.center
            return lon - 0.1 - buffer_deg, lat - 0.1 - buffer_deg, lon + 0.1 + buffer_deg, lat + 0.1 + buffer_deg
        return (self.lon_min - buffer_deg, self.lat_min - buffer_deg,
                self.lon_max + buffer_deg, self.lat_max + buffer_deg)

    def window(self, days_before: int = 15, days_after: int = 15) -> tuple[pd.Timestamp, pd.Timestamp]:
        start = self.start_utc or pd.Timestamp(f"{self.year}-01-01", tz="UTC")
        end = self.start_utc or pd.Timestamp(f"{self.year}-12-31", tz="UTC")
        return start - pd.Timedelta(days=days_before), end + pd.Timedelta(days=days_after)

    @property
    def reference_time(self) -> pd.Timestamp | None:
        return self.start_utc or self.first_detection_utc


def slug(s: Any) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", str(s)).strip("_").upper()


def _num(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) else x


def load_fires(xlsx: Path, sheet: str | int = 0) -> list[Fire]:
    """The workbook has a field-group header row, then the field-name row, then data."""
    df = pd.read_excel(xlsx, sheet_name=sheet, header=1)
    df.columns = [str(c).strip() for c in df.columns]
    fires: list[Fire] = []
    seen: set[str] = set()
    for _, r in df.iterrows():
        name = r.get("FIRE_NAME")
        year = r.get("YEAR_")
        if pd.isna(name) or pd.isna(year):
            continue
        key = f"{int(year)}_{slug(name)}"
        if key in seen:
            key = f"{key}_{r.get('OBJECTID')}"
        seen.add(key)
        start, prec = to_utc(r.get("Start_Local"), "America/Los_Angeles")
        if isinstance(r.get("Start_Local"), pd.Timestamp):
            sl = r.get("Start_Local")
            prec = "date" if (sl.hour, sl.minute, sl.second) == (0, 0, 0) else "datetime"
        end, _ = to_utc(r.get("End_Final"), "America/Los_Angeles")
        first_det, _ = to_utc(r.get("First_Detection"), "America/Los_Angeles")
        lat, lon = _num(r.get("Ignition_Lat")), _num(r.get("Ignition_Lon"))
        if not valid_latlon(lat, lon):
            lat = lon = None
        fires.append(Fire(
            key=key,
            name=str(name).strip(),
            year=int(year),
            objectid=r.get("OBJECTID"),
            irwin_id=norm_irwin(r.get("IRWINID")),
            unique_fire_id=norm_ufi(r.get("UniqueFireIdentifier")),
            unit=None if pd.isna(r.get("UNIT_ID")) else str(r.get("UNIT_ID")),
            gis_acres=_num(r.get("GIS_ACRES")),
            ign_lat=lat, ign_lon=lon,
            lon_min=_num(r.get("lon_min")), lat_min=_num(r.get("lat_min")),
            lon_max=_num(r.get("lon_max")), lat_max=_num(r.get("lat_max")),
            start_utc=start, start_precision=prec, end_utc=end, first_detection_utc=first_det,
            extra={k: r.get(k) for k in ("Detections", "Overpasses", "First_Det_vs_Start_Hrs",
                                         "Destroyed", "Damaged_Total", "LANDFIRE_Version")
                   if k in df.columns},
        ))
    return fires


def fireset_records(fires: list[Fire]) -> pd.DataFrame:
    """The FireSET table itself as canonical rows (source 'fireset_subset'), for comparison only."""
    rows = []
    for f in fires:
        rows.append({
            "fire_key": f.key, "source": "fireset_subset", "lineage": "FIRESET", "trust": "X",
            "source_record_id": f"fireset:{f.objectid}", "name": f.name, "irwin_id": f.irwin_id,
            "unique_fire_id": f.unique_fire_id, "year": f.year, "lat": f.ign_lat, "lon": f.ign_lon,
            "ignition_kind": "FireSET Ignition_Lat/Lon", "discovery_utc": f.start_utc,
            "discovery_precision": f.start_precision, "out_utc": f.end_utc, "size_acres": f.gis_acres,
            "match_method": "self", "match_score": 1.0,
        })
    return pd.DataFrame(rows)
