"""On-disk cache for raw source pulls, so analysis can rerun without refetching."""
from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import pandas as pd

EMPTY_FC = '{"type": "FeatureCollection", "features": []}'


def raw_path(cache_dir: Path, source_id: str, fire_key: str) -> Path:
    return cache_dir / "raw" / source_id / f"{fire_key}.geojson"


def evidence_path(cache_dir: Path, source_id: str, fire_key: str) -> Path:
    return cache_dir / "evidence" / source_id / f"{fire_key}.csv"


def error_path(cache_dir: Path, source_id: str, fire_key: str) -> Path:
    return cache_dir / "errors" / source_id / f"{fire_key}.txt"


def _jsonable(df: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    df = df.copy()
    for c in df.columns:
        if c == df.geometry.name:
            continue
        s = df[c]
        if pd.api.types.is_datetime64_any_dtype(s):
            df[c] = s.map(lambda v: None if pd.isna(v) else v.isoformat())
        elif s.dtype == object:
            df[c] = s.map(lambda v: v.isoformat() if isinstance(v, pd.Timestamp)
                          else (None if v is pd.NaT else v))
    return df


def save_gdf(path: Path, gdf: gpd.GeoDataFrame | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if gdf is None or len(gdf) == 0:
        path.write_text(EMPTY_FC)
        return
    if gdf.crs is None:
        gdf = gdf.set_crs(4326)
    path.write_text(_jsonable(gdf.to_crs(4326)).to_json(na="null", drop_id=True, default=str))


def load_gdf(path: Path) -> gpd.GeoDataFrame:
    data = json.loads(path.read_text())
    feats = data.get("features", [])
    if not feats:
        return gpd.GeoDataFrame({"geometry": []}, geometry="geometry", crs="EPSG:4326")
    return gpd.GeoDataFrame.from_features(feats, crs="EPSG:4326")


def save_error(path: Path, msg: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(msg)
