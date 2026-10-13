"""Source adapters. Record sources return raw rows per fire; evidence sources return satellite detections."""
from __future__ import annotations

import glob
import io
import logging
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import Point, box

from ..config import Settings, SourceSpec
from ..fires import Fire
from ..normalize import resolve_column
from .arcgis import ArcGISLayer
from .http import Http

log = logging.getLogger(__name__)
CA_BBOX = (-124.6, 32.4, -113.9, 42.1)
DETECTION_COLUMNS = ["time_utc", "lat", "lon", "satellite", "sensor", "frp", "confidence", "source"]


class RecordSource:
    """Base class: produce raw candidate rows (GeoDataFrame, EPSG:4326) near one fire."""

    bulk = False

    def __init__(self, spec: SourceSpec, settings: Settings, defaults: dict[str, Any], http: Http | None):
        self.spec = spec
        self.settings = settings
        self.defaults = defaults
        self.http = http
        self.buffer = float(defaults.get("query_buffer_deg", 0.05))

    def available(self) -> tuple[bool, str]:
        return True, ""

    def prefetch(self) -> None:  # bulk sources load everything once
        pass

    def fetch(self, fire: Fire) -> gpd.GeoDataFrame:
        raise NotImplementedError

    # common client-side filter for bulk sources
    def _near(self, gdf: gpd.GeoDataFrame, fire: Fire, year_col: str | None = None) -> gpd.GeoDataFrame:
        if gdf is None or len(gdf) == 0:
            return gdf
        env = box(*fire.envelope(self.buffer))
        sel = gdf[gdf.geometry.notna() & gdf.geometry.intersects(env)]
        if year_col and year_col in sel.columns:
            yrs = pd.to_numeric(sel[year_col], errors="coerce")
            sel = sel[(yrs.isna()) | ((yrs >= fire.year - 1) & (yrs <= fire.year + 1))]
        return sel.copy()


class ArcGISSource(RecordSource):
    _layer: ArcGISLayer | None = None

    @property
    def layer(self) -> ArcGISLayer:
        if self._layer is None:
            self._layer = ArcGISLayer(self.spec.url, self.http, int(self.defaults.get("arcgis_page_size", 1000)))
        return self._layer

    def _where(self, fire: Fire) -> str | None:
        tpl = self.spec.where
        if not tpl:
            return None
        t0, t1 = fire.window(20, 20)
        return tpl.format(year=fire.year, t0=t0.strftime("%Y-%m-%d %H:%M:%S"),
                          t1=t1.strftime("%Y-%m-%d %H:%M:%S"))

    def fetch(self, fire: Fire) -> gpd.GeoDataFrame:
        gdf, used = self.layer.query_with_fallback(self._where(fire), fire.envelope(self.buffer))
        if used == "1=1" and self.spec.where:
            ycol = resolve_column(gdf.columns, self.spec.fields.get("year"))
            gdf = self._near(gdf, fire, ycol)
        return gdf


class CalFireIncidents(RecordSource):
    bulk = True

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._all: gpd.GeoDataFrame | None = None

    def _bulk_path(self) -> Path:
        return self.settings.cache_dir / "bulk" / f"{self.spec.id}.json"

    def _years(self) -> list[int]:
        yrs = self.spec.raw.get("years") or []
        return list(range(int(yrs[0]), int(yrs[1]) + 1)) if len(yrs) == 2 else [None]

    def prefetch(self) -> None:
        p = self._bulk_path()
        if p.exists():
            data = pd.read_json(p, dtype=False)
        else:
            # the List endpoint returns one season per call: `&year=YYYY` (only the current year without it)
            raw: list[dict] = []
            for yr in self._years():
                url = self.spec.url if yr is None else f"{self.spec.url}&year={yr}"
                part = self.http.get_json(url)
                if isinstance(part, dict):  # GeoJsonList shape or wrapped list
                    part = part.get("features") or part.get("Incidents") or part.get("incidents") or []
                    part = [r.get("properties", r) for r in part]
                log.info("%s: %d incidents for %s", self.spec.id, len(part), yr or "current year")
                raw.extend(part)
            p.parent.mkdir(parents=True, exist_ok=True)
            data = pd.DataFrame(raw)
            data.to_json(p, orient="records", date_format="iso")
        lat = resolve_column(data.columns, self.spec.fields.get("lat"))
        lon = resolve_column(data.columns, self.spec.fields.get("lon"))
        geoms = [Point(x, y) if pd.notna(x) and pd.notna(y) and (x, y) != (0, 0) else None
                 for x, y in zip(pd.to_numeric(data[lon], errors="coerce"), pd.to_numeric(data[lat], errors="coerce"))]
        self._all = gpd.GeoDataFrame(data, geometry=geoms, crs="EPSG:4326")
        started = resolve_column(data.columns, self.spec.fields.get("discovery"))
        self._all["_year"] = pd.to_datetime(self._all[started], errors="coerce").dt.year if started else np.nan

    def fetch(self, fire: Fire) -> gpd.GeoDataFrame:
        if self._all is None:
            self.prefetch()
        return self._near(self._all, fire, "_year").drop(columns=["_year"], errors="ignore")


class LocalVector(RecordSource):
    bulk = True

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._all: gpd.GeoDataFrame | None = None
        self.path = self.settings.local_path(self.spec.path_key) if self.spec.path_key else None

    def available(self) -> tuple[bool, str]:
        if self.path is None:
            return False, "no path configured"
        if not self.path.exists():
            return False, f"file not found: {self.path}"
        return True, ""

    def prefetch(self) -> None:
        import pyogrio
        kwargs: dict[str, Any] = {}
        if self.spec.layer:
            layers = [l[0] for l in pyogrio.list_layers(self.path)]
            match = [l for l in layers if l.lower() == self.spec.layer.lower()]
            if match:
                kwargs["layer"] = match[0]
        if self.spec.filter:
            # filter while reading (FPA-FOD has 2.7 M national rows); the loop below re-checks case-insensitively
            names = set(pyogrio.read_info(self.path, **kwargs)["fields"])
            conds = [f"{c} = '{v}'" for c, v in self.spec.filter.items() if c in names]
            if conds:
                kwargs["where"] = " AND ".join(conds)
        # national layers: read only features intersecting California (geopandas reprojects the box)
        kwargs["bbox"] = gpd.GeoSeries([box(*CA_BBOX)], crs="EPSG:4326")
        gdf = gpd.read_file(self.path, **kwargs)
        if not isinstance(gdf, gpd.GeoDataFrame) or gdf.geometry is None or gdf.geometry.isna().all():
            lat = resolve_column(gdf.columns, self.spec.fields.get("lat"))
            lon = resolve_column(gdf.columns, self.spec.fields.get("lon"))
            gdf = gpd.GeoDataFrame(gdf, geometry=gpd.points_from_xy(gdf[lon], gdf[lat]), crs="EPSG:4326")
        if gdf.crs is None:
            gdf = gdf.set_crs(4326)
        gdf = gdf.to_crs(4326)
        for col, val in (self.spec.filter or {}).items():
            c = resolve_column(gdf.columns, [col])
            if c is not None:
                gdf = gdf[gdf[c].astype(str).str.upper() == str(val).upper()]
        gdf = gdf[gdf.geometry.notna() & gdf.geometry.intersects(box(*CA_BBOX))]
        self._all = gdf
        log.info("%s: %d rows in California", self.spec.id, len(gdf))

    def fetch(self, fire: Fire) -> gpd.GeoDataFrame:
        if self._all is None:
            self.prefetch()
        ycol = resolve_column(self._all.columns, self.spec.fields.get("year"))
        return self._near(self._all, fire, ycol)


RECORD_KINDS = {"arcgis": ArcGISSource, "calfire_json": CalFireIncidents, "local_vector": LocalVector}


# ---------------------------------------------------------------- satellite evidence


class EvidenceSource:
    def __init__(self, spec: SourceSpec, settings: Settings, http: Http | None):
        self.spec, self.settings, self.http = spec, settings, http

    def available(self) -> tuple[bool, str]:
        return True, ""

    def detections(self, fire: Fire) -> pd.DataFrame:
        raise NotImplementedError


def _firms_time(acq_date: pd.Series, acq_time: pd.Series) -> pd.Series:
    t = pd.to_numeric(acq_time, errors="coerce").fillna(0).astype(int).astype(str).str.zfill(4)
    return pd.to_datetime(acq_date.astype(str).str[:10] + " " + t, format="%Y-%m-%d %H%M", errors="coerce", utc=True)


def _standardize_firms(df: pd.DataFrame, source: str) -> pd.DataFrame:
    if df is not None and "type" in df.columns:
        # FIRMS archive type: 0 presumed vegetation fire, 1 volcano, 2 static land source (industry), 3 offshore
        df = df[pd.to_numeric(df["type"], errors="coerce").fillna(0) == 0]
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=DETECTION_COLUMNS)
    out = pd.DataFrame({
        "time_utc": _firms_time(df["acq_date"], df["acq_time"]),
        "lat": pd.to_numeric(df["latitude"], errors="coerce"),
        "lon": pd.to_numeric(df["longitude"], errors="coerce"),
        "satellite": df.get("satellite", pd.Series([None] * len(df))).astype(str).values,
        "sensor": df.get("instrument", pd.Series(["VIIRS"] * len(df))).astype(str).values,
        "frp": pd.to_numeric(df.get("frp", pd.Series([np.nan] * len(df))), errors="coerce").values,
        "confidence": df.get("confidence", pd.Series([None] * len(df))).astype(str).values,
        "source": source,
    })
    return out.dropna(subset=["time_utc", "lat", "lon"])


def _in_fire(df: pd.DataFrame, fire: Fire, buffer: float, days_before: int = 3, days_after: int = 60) -> pd.DataFrame:
    xmin, ymin, xmax, ymax = fire.envelope(buffer)
    m = df["lon"].between(xmin, xmax) & df["lat"].between(ymin, ymax)
    ref = fire.reference_time
    if ref is not None:
        m &= df["time_utc"].between(ref - pd.Timedelta(days=days_before), ref + pd.Timedelta(days=days_after))
    return df[m].sort_values("time_utc")


class ViirsFireSET(EvidenceSource):
    """Reads FireSET's detection_{YEAR}_filtered_350.h5 (one group per fire)."""

    def _path(self, year: int) -> Path | None:
        pat = self.settings.raw.get("paths", {}).get("local", {}).get(self.spec.path_key or "")
        if not pat:
            return None
        p = Path(pat.format(year=year))
        return p if p.is_absolute() else self.settings.root / p

    def available(self) -> tuple[bool, str]:
        pat = self.settings.raw.get("paths", {}).get("local", {}).get(self.spec.path_key or "")
        return (True, "") if pat else (False, "no viirs_h5_pattern configured")

    @staticmethod
    def read_group(grp) -> pd.DataFrame:
        cols: dict[str, Any] = {}
        for i in range(int(grp.attrs["n_columns"])):
            c = grp[f"col_{i}"]
            name, kind = str(c.attrs["name"]), str(c.attrs["type"])
            if kind == "numeric":
                cols[name] = c["data"][()].astype(str(c.attrs["dtype"]))
            elif kind == "datetime":
                cols[name] = pd.to_datetime(c["data"][()])
            else:
                data = c["data"].asstr()[()].astype(object)
                data[c["null_mask"][()]] = None
                cols[name] = data
        return pd.DataFrame(cols)

    def detections(self, fire: Fire) -> pd.DataFrame:
        import h5py
        p = self._path(fire.year)
        if p is None or not p.exists():
            return pd.DataFrame(columns=DETECTION_COLUMNS)
        name = fire.name.upper()
        with h5py.File(p, "r") as h5:
            keys = [k for k in h5.keys() if k.upper() == name or k.upper().startswith(name + "_")]
            if not keys:
                return pd.DataFrame(columns=DETECTION_COLUMNS)
            frames = [_standardize_firms(self.read_group(h5[k]), self.spec.id).assign(_group=k) for k in keys]
        if len(frames) > 1:  # duplicate names: keep the group closest to this fire
            lon, lat = fire.center
            frames.sort(key=lambda d: np.hypot(d["lon"].median() - lon, d["lat"].median() - lat)
                        if len(d) else 1e9)
        return frames[0].drop(columns="_group").sort_values("time_utc")


class FirmsCsv(EvidenceSource):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._all: pd.DataFrame | None = None

    def _glob(self) -> list[str]:
        pat = self.settings.raw.get("paths", {}).get("local", {}).get(self.spec.path_key or "")
        if not pat:
            return []
        p = Path(pat)
        p = p if p.is_absolute() else self.settings.root / p
        return sorted(glob.glob(str(p)))

    def available(self) -> tuple[bool, str]:
        return (True, "") if self._glob() else (False, "no FIRMS CSV files found")

    def detections(self, fire: Fire) -> pd.DataFrame:
        if self._all is None:
            parts = []
            for f in self._glob():
                d = pd.read_csv(f, low_memory=False)
                d = d[d["longitude"].between(CA_BBOX[0], CA_BBOX[2]) & d["latitude"].between(CA_BBOX[1], CA_BBOX[3])]
                parts.append(_standardize_firms(d, self.spec.id))
            self._all = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=DETECTION_COLUMNS)
        return _in_fire(self._all, fire, 0.02)


class HmsText(EvidenceSource):
    def detections(self, fire: Fire) -> pd.DataFrame:
        ref = fire.reference_time
        if ref is None:
            return pd.DataFrame(columns=DETECTION_COLUMNS)
        parts = []
        for day in pd.date_range((ref - pd.Timedelta(days=2)).normalize(), (ref + pd.Timedelta(days=1)).normalize()):
            url = self.spec.url.format(yyyy=day.strftime("%Y"), mm=day.strftime("%m"), yyyymmdd=day.strftime("%Y%m%d"))
            # one national file per day, shared by every fire burning that day: cache it
            day_file = self.settings.cache_dir / "bulk" / "hms" / url.rsplit("/", 1)[-1]
            if day_file.exists():
                txt = day_file.read_text()
            else:
                try:
                    txt = self.http.get(url).text
                except Exception as e:  # noqa: BLE001 - a missing day is not fatal
                    log.info("HMS %s unavailable: %s", day.date(), e)
                    continue
                day_file.parent.mkdir(parents=True, exist_ok=True)
                day_file.write_text(txt)
            parts.append(parse_hms_text(txt))
        if not parts:
            return pd.DataFrame(columns=DETECTION_COLUMNS)
        df = pd.concat(parts, ignore_index=True)
        df["source"] = self.spec.id
        return _in_fire(df, fire, 0.02, days_before=3, days_after=2)


def parse_hms_text(txt: str) -> pd.DataFrame:
    df = pd.read_csv(io.StringIO(txt), skipinitialspace=True)
    df.columns = [c.strip().lower() for c in df.columns]
    yd = df["yearday"].astype(int).astype(str)
    hhmm = df["time"].astype(int).astype(str).str.zfill(4)
    t = pd.to_datetime(yd + hhmm, format="%Y%j%H%M", errors="coerce", utc=True)
    return pd.DataFrame({
        "time_utc": t, "lat": pd.to_numeric(df["lat"], errors="coerce"),
        "lon": pd.to_numeric(df["lon"], errors="coerce"),
        "satellite": df.get("satellite", pd.Series([None] * len(df))).astype(str),
        "sensor": df.get("method", pd.Series([None] * len(df))).astype(str),
        "frp": pd.to_numeric(df.get("frp", pd.Series([np.nan] * len(df))), errors="coerce").where(lambda v: v > -999),
        "confidence": None, "source": "hms_points",
    }).dropna(subset=["time_utc", "lat", "lon"])


EVIDENCE_KINDS = {"viirs_h5": ViirsFireSET, "firms_csv": FirmsCsv, "hms_text": HmsText}


def build_sources(specs: dict[str, SourceSpec], settings: Settings, defaults: dict[str, Any],
                  http: Http | None, only: list[str] | None = None):
    records: dict[str, RecordSource] = {}
    evidence: dict[str, EvidenceSource] = {}
    for sid, spec in specs.items():
        if only and sid not in only:
            continue
        if not spec.enabled and not (only and sid in only):
            continue
        if spec.kind in RECORD_KINDS:
            records[sid] = RECORD_KINDS[spec.kind](spec, settings, defaults, http)
        elif spec.kind in EVIDENCE_KINDS:
            evidence[sid] = EVIDENCE_KINDS[spec.kind](spec, settings, http)
    return records, evidence
