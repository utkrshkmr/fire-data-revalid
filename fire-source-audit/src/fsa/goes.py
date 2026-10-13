"""Optional: earliest GOES-R ABI fire detection near a candidate ignition point.

Reads ABI-L2-FDCC (CONUS, 5-minute) NetCDF files anonymously from the NOAA Open Data
Dissemination buckets on AWS. Requires the optional extras: s3fs, xarray, h5netcdf.
"""
from __future__ import annotations

import logging
import re
from typing import Any

import numpy as np
import pandas as pd
from pyproj import Proj

from .geo import haversine_m

log = logging.getLogger(__name__)
_START_RE = re.compile(r"_s(\d{4})(\d{3})(\d{2})(\d{2})(\d{2})")


def bucket_for(t: pd.Timestamp, schedule: list[dict[str, str]]) -> str:
    for row in schedule:
        if pd.Timestamp(row["from"], tz="UTC") <= t <= pd.Timestamp(row["to"], tz="UTC"):
            return row["bucket"]
    return schedule[-1]["bucket"]


def file_start(name: str) -> pd.Timestamp | None:
    m = _START_RE.search(name)
    if not m:
        return None
    y, doy, hh, mm, ss = (int(g) for g in m.groups())
    return pd.Timestamp(year=y, month=1, day=1, tz="UTC") + pd.Timedelta(days=doy - 1, hours=hh, minutes=mm, seconds=ss)


def geos_proj(attrs: dict[str, Any]) -> tuple[Proj, float]:
    h = float(attrs["perspective_point_height"])
    p = Proj(proj="geos", h=h, lon_0=float(attrs["longitude_of_projection_origin"]),
             a=float(attrs["semi_major_axis"]), b=float(attrs["semi_minor_axis"]),
             sweep=str(attrs.get("sweep_angle_axis", "x")))
    return p, h


def nearest_window(ds, lon: float, lat: float, half: int = 4):
    """Return (x index slice, y index slice) around the pixel containing lon/lat, or None if off-disk."""
    p, h = geos_proj(ds["goes_imager_projection"].attrs)
    xm, ym = p(lon, lat)
    if not np.isfinite(xm) or not np.isfinite(ym):
        return None
    xs, ys = ds["x"].values * h, ds["y"].values * h
    ix, iy = int(np.argmin(np.abs(xs - xm))), int(np.argmin(np.abs(ys - ym)))
    return slice(max(ix - half, 0), ix + half + 1), slice(max(iy - half, 0), iy + half + 1), p, h


def earliest_detection(lon: float, lat: float, t_ref: pd.Timestamp, cfg: dict[str, Any]) -> dict[str, Any] | None:
    import s3fs
    import xarray as xr

    fs = s3fs.S3FileSystem(anon=True)
    codes = set(cfg.get("fire_mask_codes", [10, 11, 12, 13, 14, 15, 30, 31, 32, 33, 34, 35]))
    radius = float(cfg.get("radius_m", 5000))
    t0 = t_ref - pd.Timedelta(hours=cfg.get("hours_before", 6))
    t1 = t_ref + pd.Timedelta(hours=cfg.get("hours_after", 3))
    product = cfg.get("product", "ABI-L2-FDCC")
    hour = t0.floor("h")
    while hour <= t1:
        bucket = bucket_for(hour, cfg["schedule"])
        prefix = f"{bucket}/{product}/{hour:%Y}/{hour.dayofyear:03d}/{hour:%H}/"
        try:
            files = sorted(fs.ls(prefix))
        except FileNotFoundError:
            files = []
        for f in files:
            ts = file_start(f)
            if ts is None or ts < t0 or ts > t1:
                continue
            with fs.open(f, "rb") as fh:
                ds = xr.open_dataset(fh, engine="h5netcdf")
                win = nearest_window(ds, lon, lat)
                if win is None:
                    return None
                sx, sy, p, h = win
                mask = ds["Mask"].isel(x=sx, y=sy).values
                power = ds["Power"].isel(x=sx, y=sy).values if "Power" in ds else None
                xx, yy = np.meshgrid(ds["x"].values[sx] * h, ds["y"].values[sy] * h)
            hits = np.isin(mask, list(codes))
            if not hits.any():
                continue
            plon, plat = p(xx[hits], yy[hits], inverse=True)
            d = np.array([haversine_m(lat, lon, b, a) for a, b in zip(plon, plat)])
            ok = d <= radius
            if ok.any():
                k = int(np.argmin(np.where(ok, d, np.inf)))
                return {"time_utc": ts, "lat": float(plat[k]), "lon": float(plon[k]),
                        "distance_m": float(d[k]), "mask": int(mask[hits][k]),
                        "power_mw": None if power is None else float(power[hits][k]),
                        "file": f.split("/")[-1], "bucket": bucket}
        hour += pd.Timedelta(hours=1)
    return None
