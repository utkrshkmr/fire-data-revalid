"""Synthetic fixtures with known answers: fires, raw source pulls, and satellite detections."""
from __future__ import annotations

import math
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.affinity import scale, translate
from shapely.geometry import Point

from fsa import cache
from fsa.geo import LocalFrame

IRWIN_A = "{11111111-2222-3333-4444-555555555555}"
IRWIN_B = "{AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE}"

# true ignition points
A_LON, A_LAT = -121.44, 39.82
B_LON, B_LAT = -122.43, 40.94
C_LON, C_LAT = -118.62, 36.22


def offset(lon: float, lat: float, dx_m: float, dy_m: float) -> tuple[float, float]:
    f = LocalFrame(lon, lat)
    x, y = f.xy(lon, lat)
    return f.lonlat(x + dx_m, y + dy_m)


def blob(lon: float, lat: float, radius_m: float, dx_m: float = 0, dy_m: float = 0, sx: float = 1.0):
    """A circle-ish perimeter (WGS84) centred east of the ignition so the ignition sits inside near the edge."""
    f = LocalFrame(lon, lat)
    x, y = f.xy(lon, lat)
    poly = Point(x + radius_m * 0.6 + dx_m, y + dy_m).buffer(radius_m, quad_segs=32)
    poly = scale(poly, xfact=sx, yfact=1.0)
    return f.geom_to_wgs(poly)


def ms(ts: str, tz: str = "UTC") -> int:
    return int(pd.Timestamp(ts, tz=tz).tz_convert("UTC").value // 1_000_000)


def write_fires_xlsx(path: Path) -> None:
    cols = ["OBJECTID", "YEAR_", "FIRE_NAME", "IRWINID", "UniqueFireIdentifier", "UNIT_ID", "GIS_ACRES",
            "Ignition_Lat", "Ignition_Lon", "lon_min", "lat_min", "lon_max", "lat_max",
            "Start_Local", "End_Final", "First_Detection"]
    a_fs = offset(A_LON, A_LAT, 3000, 0)  # FireSET point is 3 km off for fire A
    rows = [
        [1, 2018, "ALPHA", IRWIN_A, "2018-CABTU-016737", "BTU", 2400.0, a_fs[1], a_fs[0],
         A_LON - 0.08, A_LAT - 0.06, A_LON + 0.1, A_LAT + 0.06,
         pd.Timestamp("2018-11-08 06:29"), pd.Timestamp("2018-11-25 08:00"), pd.Timestamp("2018-11-08 11:50")],
        [2, 2019, "BRAVO", IRWIN_B, "2019-CASHF-001444", "SHF", 2400.0, B_LAT, B_LON,
         B_LON - 0.08, B_LAT - 0.06, B_LON + 0.1, B_LAT + 0.06,
         pd.Timestamp("2019-09-05 12:51"), pd.Timestamp("2019-09-29 14:30"), pd.Timestamp("2019-09-05 13:40")],
        [3, 2020, "CHARLIE", None, None, "SQF", 500.0, C_LAT, C_LON,
         C_LON - 0.05, C_LAT - 0.04, C_LON + 0.05, C_LAT + 0.04,
         pd.Timestamp("2020-10-04"), pd.Timestamp("2020-10-20"), None],
    ]
    groups = ["Identity"] + [None] * (len(cols) - 1)
    df = pd.DataFrame(rows, columns=cols)
    with pd.ExcelWriter(path) as xw:
        pd.DataFrame([groups]).to_excel(xw, sheet_name="Initial 30", header=False, index=False)
        df.to_excel(xw, sheet_name="Initial 30", startrow=1, index=False)


def gdf(rows: list[dict], geoms: list) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(rows, geometry=geoms, crs="EPSG:4326")


def write_cache(cache_dir: Path) -> None:
    r_m = 1758.0  # circle of ~2,400 ac
    # ---------------- fire A: sources agree within ~300 m; FireSET is 3 km off
    a_cf = offset(A_LON, A_LAT, 250, -150)
    a_fod = offset(A_LON, A_LAT, -200, 120)
    cache.save_gdf(cache.raw_path(cache_dir, "wfigs_locations", "2018_ALPHA"), gdf(
        [{"IncidentName": "Alpha", "IrwinID": IRWIN_A, "UniqueFireIdentifier": "2018-CABTU-016737",
          "InitialLatitude": round(A_LAT, 6), "InitialLongitude": round(A_LON, 6),
          "FireDiscoveryDateTime": ms("2018-11-08 06:29", "America/Los_Angeles"),
          "ContainmentDateTime": ms("2018-11-25 08:00", "America/Los_Angeles"),
          "FinalAcres": 2410.0, "FireCauseGeneral": "Power generation/transmission/distribution"},
         # decoy: another fire in the same box, different name and date
         {"IncidentName": "Pulga", "IrwinID": "{99999999-9999-9999-9999-999999999999}",
          "UniqueFireIdentifier": "2018-CABTU-099999",
          "InitialLatitude": A_LAT + 0.03, "InitialLongitude": A_LON + 0.05,
          "FireDiscoveryDateTime": ms("2018-08-01 13:00", "America/Los_Angeles"), "FinalAcres": 30.0}],
        [Point(A_LON, A_LAT), Point(A_LON + 0.05, A_LAT + 0.03)]))
    cache.save_gdf(cache.raw_path(cache_dir, "calfire_incidents", "2018_ALPHA"), gdf(
        [{"Name": "Alpha Fire", "Latitude": round(a_cf[1], 5), "Longitude": round(a_cf[0], 5),
          "Started": "2018-11-08T14:33:00Z", "AcresBurned": 2400, "UniqueId": "cf-a"}],
        [Point(*a_cf)]))
    cache.save_gdf(cache.raw_path(cache_dir, "fpa_fod", "2018_ALPHA"), gdf(
        [{"FIRE_NAME": "ALPHA", "LATITUDE": round(a_fod[1], 4), "LONGITUDE": round(a_fod[0], 4),
          "DISCOVERY_DATE": "2018-11-08", "DISCOVERY_TIME": "0630", "FIRE_SIZE": 2390.0, "FIRE_YEAR": 2018,
          "NWCG_CAUSE_CLASSIFICATION": "Human", "FOD_ID": 1}],
        [Point(*a_fod)]))
    cache.save_gdf(cache.raw_path(cache_dir, "frap_perimeters", "2018_ALPHA"), gdf(
        [{"FIRE_NAME": "ALPHA", "YEAR_": 2018, "IRWINID": IRWIN_A, "ALARM_DATE": ms("2018-11-08"),
          "CONT_DATE": ms("2018-11-25"), "GIS_ACRES": 2420.0, "CAUSE": 11}],
        [blob(A_LON, A_LAT, r_m)]))
    cache.save_gdf(cache.raw_path(cache_dir, "wfigs_perimeters", "2018_ALPHA"), gdf(
        [{"poly_IncidentName": "Alpha", "attr_IrwinID": IRWIN_A, "poly_GISAcres": 2450.0}],
        [blob(A_LON, A_LAT, r_m, dx_m=120)]))
    cache.save_gdf(cache.raw_path(cache_dir, "mtbs_perimeters", "2018_ALPHA"), gdf(
        [{"Incid_Name": "ALPHA", "irwinID": IRWIN_A, "BurnBndAc": 2300.0, "Event_ID": "CA_A"}],
        [blob(A_LON, A_LAT, r_m * 0.96, dy_m=-80)]))

    # ---------------- fire B: USFS point is 6 km away and its time is 8 h off (time-zone bug)
    b_far = offset(B_LON, B_LAT, -6000, 2500)
    b_cf = offset(B_LON, B_LAT, 150, 100)
    cache.save_gdf(cache.raw_path(cache_dir, "wfigs_locations", "2019_BRAVO"), gdf(
        [{"IncidentName": "Bravo", "IrwinID": IRWIN_B, "InitialLatitude": B_LAT, "InitialLongitude": B_LON,
          "FireDiscoveryDateTime": ms("2019-09-05 12:51", "America/Los_Angeles"), "FinalAcres": 2400.0,
          "FireCauseGeneral": "Natural"}],
        [Point(B_LON, B_LAT)]))
    cache.save_gdf(cache.raw_path(cache_dir, "calfire_incidents", "2019_BRAVO"), gdf(
        [{"Name": "Bravo Fire", "Latitude": b_cf[1], "Longitude": b_cf[0], "Started": "2019-09-05T19:55:00Z",
          "AcresBurned": 2500}],
        [Point(*b_cf)]))
    cache.save_gdf(cache.raw_path(cache_dir, "usfs_edw_points", "2019_BRAVO"), gdf(
        [{"firename": "BRAVO", "fireyear": 2019, "uniqfireid": "2019-CASHF-001444",
          "latdd83": b_far[1], "longdd83": b_far[0],
          # stored as local clock time labelled UTC: 8 h early relative to true UTC
          "discoverydatetime": ms("2019-09-05 12:51"), "totalacres": 2400.0, "statcause": "Human"}],
        [Point(*b_far)]))
    cache.save_gdf(cache.raw_path(cache_dir, "frap_perimeters", "2019_BRAVO"), gdf(
        [{"FIRE_NAME": "BRAVO", "YEAR_": 2019, "ALARM_DATE": ms("2019-09-05"), "GIS_ACRES": 2400.0, "CAUSE": 1}],
        [blob(B_LON, B_LAT, r_m)]))
    cache.save_gdf(cache.raw_path(cache_dir, "usfs_edw_perimeters", "2019_BRAVO"), gdf(
        [{"firename": "BRAVO", "fireyear": 2019, "uniqfireid": "2019-CASHF-001444", "gisacres": 2380.0}],
        [blob(B_LON, B_LAT, r_m, dx_m=-60)]))
    cache.save_gdf(cache.raw_path(cache_dir, "mtbs_perimeters", "2019_BRAVO"), gdf(
        [{"Incid_Name": "BRAVO", "BurnBndAc": 2350.0}],
        [blob(B_LON, B_LAT, r_m, dx_m=40, dy_m=30)]))

    # ---------------- fire C: nothing in any source (only empty pulls)
    for sid in ("wfigs_locations", "frap_perimeters"):
        cache.save_gdf(cache.raw_path(cache_dir, sid, "2020_CHARLIE"), None)

    # ---------------- VIIRS first overpass near the true ignitions
    rows = []
    for key, lon, lat, t0 in (("2018_ALPHA", A_LON, A_LAT, "2018-11-08 18:50"),
                              ("2019_BRAVO", B_LON, B_LAT, "2019-09-05 20:40")):
        rng = np.random.default_rng(0)
        det = []
        for i in range(6):
            lo, la = offset(lon, lat, 300 + rng.normal(0, 150), rng.normal(0, 150))
            det.append({"time_utc": pd.Timestamp(t0, tz="UTC") + pd.Timedelta(minutes=i), "lat": la, "lon": lo,
                        "satellite": "N", "sensor": "VIIRS", "frp": 5.0, "confidence": "n", "source": "viirs_fireset"})
        for i in range(10):  # later spread to the east
            lo, la = offset(lon, lat, 1500 + 300 * i, 0)
            det.append({"time_utc": pd.Timestamp(t0, tz="UTC") + pd.Timedelta(hours=12), "lat": la, "lon": lo,
                        "satellite": "N", "sensor": "VIIRS", "frp": 20.0, "confidence": "n", "source": "viirs_fireset"})
        p = cache.evidence_path(cache_dir, "viirs_fireset", key)
        p.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(det).to_csv(p, index=False)
