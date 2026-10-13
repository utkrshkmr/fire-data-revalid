"""Link each fire in the FireSET list to the matching record(s) in every source."""
from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd
from shapely.geometry import Point, box
from shapely.ops import unary_union

from .fires import Fire
from .geo import haversine_m
from .normalize import incident_number, name_similarity, norm_name


def _days(a: pd.Timestamp | None, b: pd.Timestamp | None) -> float | None:
    if a is None or b is None or pd.isna(a) or pd.isna(b):
        return None
    return abs((a - b).total_seconds()) / 86400.0


def _point_box_distance_m(lat: float, lon: float, fire: Fire) -> float:
    xmin, ymin, xmax, ymax = fire.envelope(0.0)
    cx, cy = min(max(lon, xmin), xmax), min(max(lat, ymin), ymax)
    return 0.0 if (cx, cy) == (lon, lat) else haversine_m(lat, lon, cy, cx)


def score_candidate(rec: pd.Series, fire: Fire, cfg: dict[str, Any]) -> tuple[float, str, dict[str, float]]:
    """Return (score, method, components). ID matches outrank any fuzzy match."""
    comp: dict[str, float] = {}
    if fire.irwin_id and rec.get("irwin_id") and rec["irwin_id"] == fire.irwin_id:
        return 1.0, "irwin_id", comp
    if fire.unique_fire_id and rec.get("unique_fire_id") and rec["unique_fire_id"] == fire.unique_fire_id:
        return 0.97, "unique_fire_id", comp
    fire_num = incident_number(fire.unique_fire_id)
    rec_num = incident_number(rec.get("unique_fire_id")) or incident_number(rec.get("local_number"))
    year_ok = rec.get("year") in (None, fire.year) or (isinstance(rec.get("year"), float) and math.isnan(rec["year"]))

    # name
    s_name = max(name_similarity(rec.get("name"), fire.name),
                 name_similarity(rec.get("complex_name"), fire.name) * 0.9)
    comp["name"] = s_name

    # time
    window = float(cfg.get("date_window_days", 5))
    d = _days(rec.get("discovery_utc"), fire.start_utc)
    s_time = 0.5 if d is None else max(0.0, 1.0 - d / window)
    comp["time"] = s_time

    # space
    geom = rec.get("geometry")
    slack = float(cfg.get("spatial_slack_m", 5000))
    if rec.get("lat") is not None and not pd.isna(rec.get("lat")):
        dist = _point_box_distance_m(rec["lat"], rec["lon"], fire)
        s_space = max(0.0, 1.0 - dist / slack)
    elif geom is not None and not getattr(geom, "is_empty", True):
        env = box(*fire.envelope(0.0))
        s_space = min(1.0, geom.intersection(env).area / max(geom.area, 1e-12)) if geom.intersects(env) else 0.0
    else:
        s_space = 0.0
    comp["space"] = s_space

    # size
    if rec.get("size_acres") and fire.gis_acres and rec["size_acres"] > 0 and fire.gis_acres > 0:
        s_size = math.exp(-abs(math.log(rec["size_acres"] / fire.gis_acres)))
    else:
        s_size = 0.5
    comp["size"] = s_size

    if fire_num and rec_num and fire_num == rec_num and year_ok and s_space > 0.5:
        return 0.9, "incident_number", comp

    fuzzy = 0.45 * s_name + 0.25 * s_time + 0.20 * s_space + 0.10 * s_size
    if s_name < float(cfg.get("name_min_similarity", 0.55)):
        # an unnamed or renamed record can still match on place, date and size together
        if not (s_space >= 0.99 and s_time >= 0.8 and s_size >= 0.6):
            fuzzy *= 0.5
    return min(fuzzy, 0.89), "fuzzy", comp


def _identity(rec: pd.Series) -> str:
    return rec.get("irwin_id") or rec.get("unique_fire_id") or norm_name(rec.get("name")) or str(rec.get("source_record_id"))


def match_fire(fire: Fire, cands: pd.DataFrame, cfg: dict[str, Any], is_perimeter: bool) -> list[dict[str, Any]]:
    """Pick the matching record per lineage within one source's candidates for one fire."""
    if cands is None or len(cands) == 0:
        return []
    scored = []
    for _, rec in cands.iterrows():
        s, method, comp = score_candidate(rec, fire, cfg)
        scored.append((s, method, comp, rec))
    accept = float(cfg.get("accept_score", 0.6))
    margin = float(cfg.get("ambiguity_margin", 0.05))
    out: list[dict[str, Any]] = []
    by_lineage: dict[str, list] = {}
    for item in scored:
        by_lineage.setdefault(item[3]["lineage"], []).append(item)
    for lineage, items in by_lineage.items():
        items.sort(key=lambda t: -t[0])
        best_s, best_m, best_c, best = items[0]
        if best_s < accept:
            continue
        ident = _identity(best)
        same = [t for t in items if _identity(t[3]) == ident and t[0] >= accept]
        others = [t for t in items[1:] if _identity(t[3]) != ident and t[0] >= best_s - margin and t[0] >= accept]
        row = best.to_dict()
        if is_perimeter and len(same) > 1:
            geoms = [t[3].get("geometry") for t in same if t[3].get("geometry") is not None]
            dated = [t for t in same if t[3].get("perimeter_datetime_utc") is not None
                     and not pd.isna(t[3].get("perimeter_datetime_utc"))]
            if dated and len({str(t[3]["perimeter_datetime_utc"]) for t in dated}) > 1:
                # several dated perimeters of one fire (operational feeds): keep the latest
                latest = max(dated, key=lambda t: t[3]["perimeter_datetime_utc"])[3]
                row = latest.to_dict()
            elif geoms:
                row["geometry"] = unary_union(geoms)
                row["size_acres"] = np.nansum([t[3].get("size_acres") or np.nan for t in same]) or row.get("size_acres")
        row.update({
            "fire_key": fire.key, "match_method": best_m, "match_score": round(best_s, 3),
            "match_components": {k: round(v, 3) for k, v in best_c.items()},
            "n_candidates": len(items), "n_merged": len(same),
            "ambiguous": bool(others),
            "runner_up": None if not others else f"{others[0][3].get('name')} ({others[0][0]:.2f})",
        })
        if best_m in ("irwin_id", "unique_fire_id", "incident_number") and row.get("lat") is not None \
                and not pd.isna(row.get("lat")):
            row["id_location_conflict_m"] = round(_point_box_distance_m(row["lat"], row["lon"], fire), 1)
        out.append(row)
    return out
