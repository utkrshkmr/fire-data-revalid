"""Per-fire cross-source comparison: consensus values, per-source errors, flags, corrections."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
import shapely
from shapely.geometry import Point

from .config import Settings, SourceSpec
from .fires import Fire
from .geo import (LocalFrame, area_acres, centroid_offset_m, clean_polygon, geometric_median, hausdorff_m,
                  iou, medoid, pairwise, signed_distance_to_polygon_m, vote_consensus)
from .geo import zone as buffer_zone

TRUST_W = {"A": 1.0, "B": 0.7, "C": 0.4, "X": 0.0}


def _ok(v: Any) -> bool:
    return v is not None and not (isinstance(v, float) and math.isnan(v)) and not (v is pd.NaT)


def _ts_ok(v: Any) -> bool:
    return v is not None and not pd.isna(v)


COPY_TOLERANCE_M = 5.0  # float32 storage and Web Mercator round trips move a copied point by ~1-2 m


def _copied_point(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Same point within storage noise, or one is the other rounded to fewer decimals (MTBS Event_ID, PLSS)."""
    if math.hypot(a["x"] - b["x"], a["y"] - b["y"]) < COPY_TOLERANCE_M:
        return True
    da, db = a.get("decimals"), b.get("decimals")
    if not _ok(da) or not _ok(db) or da == db:
        return False
    lo, hi = (a, b) if da < db else (b, a)
    d = int(lo["decimals"])
    # at 2 decimals (~1 km cells) any independent point within ~500 m rounds to the coarse one, so only
    # 3-4 decimal roundings (MTBS Event_ID, 0.001 deg) count; exact coarse copies are caught by distance above
    if not 3 <= d <= 4:
        return False
    tol = 0.1 * 10 ** -d
    return abs(round(hi["lat"], d) - lo["lat"]) < tol and abs(round(hi["lon"], d) - lo["lon"]) < tol


@dataclass
class FireResult:
    summary: dict[str, Any]
    errors: list[dict[str, Any]] = field(default_factory=list)
    pairs: list[dict[str, Any]] = field(default_factory=list)
    flags: list[dict[str, Any]] = field(default_factory=list)
    patch: dict[str, Any] | None = None
    consensus_perimeter: Any = None          # WGS84 geometry
    points: list[dict[str, Any]] = field(default_factory=list)


class FireAnalyzer:
    def __init__(self, specs: dict[str, SourceSpec], settings: Settings):
        self.specs = specs
        self.s = settings
        self.ign = settings.raw.get("ignition", {})
        self.per = settings.raw.get("perimeter", {})
        self.tim = settings.raw.get("timing", {})
        self.order = list(specs)

    # ------------------------------------------------------------ helpers
    def _spec(self, sid: str) -> SourceSpec | None:
        return self.specs.get(sid)

    def _eligible(self, sid: str) -> bool:
        sp = self._spec(sid)
        return bool(sp and sp.consensus and sp.trust != "X")

    def _flag(self, res: FireResult, fire: Fire, category: str, severity: str, msg: str) -> None:
        res.flags.append({"fire_key": fire.key, "category": category, "severity": severity, "message": msg})

    # ------------------------------------------------------------ main
    def analyze(self, fire: Fire, recs: pd.DataFrame, det: pd.DataFrame | None,
                goes_hit: dict[str, Any] | None = None) -> FireResult:
        lon0, lat0 = fire.center
        frame = LocalFrame(lon0, lat0)
        res = FireResult(summary={"fire_key": fire.key, "fire_name": fire.name, "year": fire.year,
                                  "irwin_id": fire.irwin_id, "unique_fire_id": fire.unique_fire_id})
        recs = recs.copy() if recs is not None else pd.DataFrame()
        res.summary["n_sources_matched"] = int(recs.loc[recs["source"] != "fireset_subset", "source"].nunique()) \
            if len(recs) else 0
        res.summary["sources_matched"] = ";".join(sorted(set(recs["source"]) - {"fireset_subset"})) if len(recs) else ""

        cons_poly_m = self._perimeters(fire, recs, frame, res)
        sat = self._satellite(fire, recs, det, cons_poly_m, frame, res, goes_hit)
        self._ignition(fire, recs, frame, cons_poly_m, sat, res)
        self._timing(fire, recs, sat, res)
        self._size(fire, recs, cons_poly_m, res)
        self._cause(fire, recs, res)
        self._identifiers(fire, recs, res)
        res.summary["n_flags"] = len(res.flags)
        res.summary["max_flag_severity"] = (
            "high" if any(f["severity"] == "high" for f in res.flags)
            else "medium" if any(f["severity"] == "medium" for f in res.flags)
            else "low" if res.flags else "none")
        return res

    # ------------------------------------------------------------ perimeters
    def _perimeters(self, fire: Fire, recs: pd.DataFrame, frame: LocalFrame, res: FireResult):
        if not len(recs) or "geometry" not in recs:
            res.summary["n_perimeter_sources"] = 0
            return None
        rows = []
        for _, r in recs.iterrows():
            sp = self._spec(r["source"])
            if not sp or "perimeter" not in sp.roles:
                continue
            g = r.get("geometry")
            if g is None or getattr(g, "is_empty", True) or g.geom_type not in ("Polygon", "MultiPolygon"):
                continue
            gm = clean_polygon(frame.geom(g))
            if gm is None:
                continue
            rows.append({"source": r["source"], "lineage": r["lineage"], "trust": r.get("trust"), "geom": gm,
                         "reported_acres": r.get("size_acres"), "eligible": self._eligible(r["source"])})
        res.summary["n_perimeter_sources"] = len(rows)
        if not rows:
            return None

        # one perimeter per lineage for the vote (source order in sources.yaml = priority)
        def lineage_polys(exclude: str | None = None) -> dict[str, Any]:
            chosen: dict[str, Any] = {}
            for r in sorted(rows, key=lambda r: (-TRUST_W.get(r["trust"] or "B", 0.5), self.order.index(r["source"])
                                                 if r["source"] in self.order else 99)):
                if not r["eligible"] or r["lineage"] == exclude or r["lineage"] in chosen:
                    continue
                chosen[r["lineage"]] = r["geom"]
            return chosen

        res_m = float(self.per.get("raster_res_m", 30))
        vote = float(self.per.get("consensus_vote", 0.5))
        maxc = int(self.per.get("max_cells", 40_000_000))
        lp = lineage_polys()
        cons = vote_consensus(list(lp.values()), res_m, vote, maxc)
        res.summary["n_perimeter_lineages"] = len(lp)
        res.summary["perimeter_lineages"] = ";".join(sorted(lp))
        if cons is not None:
            res.summary["consensus_acres"] = round(area_acres(cons), 1)
            res.consensus_perimeter = frame.geom_to_wgs(cons)

        loo_cache: dict[str, Any] = {}
        for r in rows:
            if r["lineage"] not in loo_cache:
                others = lineage_polys(exclude=r["lineage"])
                loo_cache[r["lineage"]] = vote_consensus(list(others.values()), res_m, vote, maxc) if others else None
            ref = loo_cache[r["lineage"]]
            a = area_acres(r["geom"])
            err = {"fire_key": fire.key, "source": r["source"], "lineage": r["lineage"], "aspect": "perimeter",
                   "perimeter_acres_gis": round(a, 1),
                   "reported_acres": r["reported_acres"],
                   "iou_vs_others": None if ref is None else round(iou(r["geom"], ref), 4),
                   "area_ratio_vs_others": None if ref is None or ref.area == 0 else round(r["geom"].area / ref.area, 4),
                   "hausdorff_m_vs_others": None if ref is None else round(hausdorff_m(r["geom"], ref), 1),
                   "centroid_offset_m_vs_others": None if ref is None else round(centroid_offset_m(r["geom"], ref), 1)}
            res.errors.append(err)
            if ref is not None and err["iou_vs_others"] is not None and err["iou_vs_others"] < 0.7 and r["eligible"]:
                self._flag(res, fire, "perimeter", "medium",
                           f"{r['source']} perimeter IoU {err['iou_vs_others']:.2f} against the other sources")
        ious = []
        for a, b in pairwise(rows):
            v = iou(a["geom"], b["geom"])
            ious.append(v)
            res.pairs.append({"fire_key": fire.key, "source_a": a["source"], "source_b": b["source"],
                              "iou": None if v is None else round(v, 4),
                              "area_ratio": round(a["geom"].area / b["geom"].area, 4) if b["geom"].area else None,
                              "hausdorff_m": round(hausdorff_m(a["geom"], b["geom"]), 1)})
        ious = [v for v in ious if v is not None]
        if ious:
            res.summary["perimeter_iou_min"] = round(min(ious), 4)
            res.summary["perimeter_iou_median"] = round(float(np.median(ious)), 4)
            if np.median(ious) < 0.8:
                self._flag(res, fire, "perimeter", "medium",
                           f"perimeter sources disagree (median pairwise IoU {np.median(ious):.2f})")
        frap = [e for e in res.errors if e["source"] == "frap_perimeters" and e["aspect"] == "perimeter"]
        if frap:
            res.summary["frap_iou_vs_others"] = frap[0]["iou_vs_others"]
        return cons

    # ------------------------------------------------------------ satellite evidence
    def _earliest_discovery(self, fire: Fire, recs: pd.DataFrame) -> pd.Timestamp | None:
        """Earliest datetime-precision discovery among consensus sources, else FireSET's start."""
        ts = []
        if len(recs) and "discovery_utc" in recs:
            for _, r in recs.iterrows():
                if self._eligible(r["source"]) and _ts_ok(r.get("discovery_utc")) \
                        and (r.get("discovery_precision") or "datetime") == "datetime":
                    ts.append(pd.Timestamp(r["discovery_utc"]))
        if ts:
            return min(ts)
        return fire.start_utc

    def _satellite(self, fire: Fire, recs: pd.DataFrame, det: pd.DataFrame | None, cons_m, frame: LocalFrame,
                   res: FireResult, goes_hit: dict[str, Any] | None) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if goes_hit:
            out["goes_time"] = pd.Timestamp(goes_hit["time_utc"])
            out["goes_lat"], out["goes_lon"] = goes_hit["lat"], goes_hit["lon"]
            res.summary["goes_first_utc"] = out["goes_time"]
        if det is not None and len(det) and "source" in det:
            # HMS mixes coarse GOES/AVHRR/MODIS points with analyst timing: report it apart from the VIIRS overpass
            hms = det[det["source"] == "hms_points"].dropna(subset=["lat", "lon", "time_utc"])
            det = det[det["source"] != "hms_points"]
            if len(hms) and cons_m is not None:
                zone = buffer_zone(cons_m, 2000)
                hms = hms[[zone.contains(Point(*frame.xy(lo, la))) for lo, la in zip(hms["lon"], hms["lat"])]]
            anchor = self._earliest_discovery(fire, recs)
            if len(hms) and anchor is not None:
                lead_h = float(self.ign.get("first_overpass_max_lead_h", 12))
                hms = hms[hms["time_utc"] >= anchor - pd.Timedelta(hours=lead_h)]
            if len(hms):
                h = hms.sort_values("time_utc").iloc[0]
                out["hms_time"] = h["time_utc"]
                res.summary.update({"hms_first_utc": h["time_utc"], "hms_first_satellite": h.get("satellite")})
        if det is None or len(det) == 0:
            res.summary["viirs_n"] = 0
            return out
        d = det.dropna(subset=["lat", "lon", "time_utc"]).copy()
        d["x"], d["y"] = frame._fwd.transform(d["lon"].to_numpy(), d["lat"].to_numpy())
        if cons_m is not None:
            zone = buffer_zone(cons_m, 1000)
            inside = shapely.contains_xy(zone, d["x"].to_numpy(), d["y"].to_numpy())
            if inside.any():
                d = d[inside]
        d = d.sort_values("time_utc")
        t_raw = d["time_utc"].iloc[0]
        res.summary["viirs_first_raw_utc"] = t_raw
        # Heat inside the perimeter long before any agency discovery is usually a neighbouring or earlier fire
        # (Delta/Hirz 2018), so the first overpass is taken from max_lead_h before the earliest reported discovery.
        anchor = self._earliest_discovery(fire, recs)
        lead = pd.Timedelta(hours=float(self.ign.get("first_overpass_max_lead_h", 12)))
        if anchor is not None:
            early = d["time_utc"] < anchor - lead
            res.summary["viirs_n_before_lead_window"] = int(early.sum())
            if (~early).any():
                d = d[~early]
        t_first = d["time_utc"].iloc[0]
        win = pd.Timedelta(minutes=float(self.ign.get("first_overpass_window_min", 60)))
        first = d[d["time_utc"] <= t_first + win]
        gm = geometric_median(first[["x", "y"]].to_numpy())
        lon, lat = frame.lonlat(*gm)
        out.update({"viirs_time": t_first, "viirs_x": gm[0], "viirs_y": gm[1], "viirs_lat": lat, "viirs_lon": lon,
                    "viirs_n_first": len(first)})
        res.summary.update({"viirs_n": int(len(d)), "viirs_first_utc": t_first, "viirs_first_lat": round(lat, 6),
                            "viirs_first_lon": round(lon, 6), "viirs_first_n": int(len(first))})
        return out

    # ------------------------------------------------------------ ignition
    def _ignition(self, fire: Fire, recs: pd.DataFrame, frame: LocalFrame, cons_m, sat: dict[str, Any],
                  res: FireResult) -> None:
        agree = float(self.ign.get("agree_m", 500))
        minor = float(self.ign.get("minor_m", 2000))
        tol = float(self.ign.get("perimeter_tolerance_m", 375))
        near_sat = float(self.ign.get("viirs_near_m", 1500))
        pts = []
        if len(recs):
            for _, r in recs.iterrows():
                sp = self._spec(r["source"])
                if r["source"] != "fireset_subset" and (not sp or "ignition" not in sp.roles):
                    continue
                if not _ok(r.get("lat")) or not _ok(r.get("lon")):
                    continue
                x, y = frame.xy(r["lon"], r["lat"])
                pts.append({"source": r["source"], "lineage": r["lineage"], "trust": r.get("trust") or "B",
                            "lat": float(r["lat"]), "lon": float(r["lon"]), "x": x, "y": y,
                            "decimals": r.get("coord_decimals"), "kind": r.get("ignition_kind"),
                            "initial_offset_m": r.get("initial_offset_m"),
                            "eligible": self._eligible(r["source"]) and r["source"] != "fireset_subset"})
        # identical coordinates across sources reveal shared upstream records (tests the lineage grouping)
        copies = [(a, b) for a, b in pairwise(pts) if a["source"] != b["source"] and _copied_point(a, b)]
        res.summary["ign_identical_pairs"] = ";".join(sorted({
            "=".join(sorted((f"{a['source']}[{a['lineage']}]", f"{b['source']}[{b['lineage']}]"))) for a, b in copies}))
        elig = [p for p in pts if p["eligible"]]
        # a point copied across lineages is one report, not two: merge those lineages for this fire
        parent = {p["lineage"]: p["lineage"] for p in elig}

        def root(k: str) -> str:
            while parent[k] != k:
                k = parent[k]
            return k
        for a, b in copies:
            if a["eligible"] and b["eligible"] and a["lineage"] != b["lineage"]:
                ra, rb = root(a["lineage"]), root(b["lineage"])
                if ra != rb:
                    parent[max(ra, rb)] = min(ra, rb)
        groups: dict[str, list[str]] = {}
        for k in parent:
            groups.setdefault(root(k), []).append(k)
        merged = {k: "+".join(sorted(v)) for r, v in groups.items() for k in v}
        res.summary["ign_lineages_merged"] = ";".join(sorted({m for m in merged.values() if "+" in m}))
        for p in elig:
            p["lineage"] = merged[p["lineage"]]
        lineages: dict[str, list[dict]] = {}
        for p in elig:
            lineages.setdefault(p["lineage"], []).append(p)

        def lineage_point(ps: list[dict]) -> np.ndarray:
            return medoid(np.array([[p["x"], p["y"]] for p in ps]))

        lin_xy = {k: lineage_point(v) for k, v in lineages.items()}
        lin_w = {k: max(TRUST_W.get(p["trust"], 0.5) for p in v) for k, v in lineages.items()}

        def consensus(exclude: str | None = None) -> np.ndarray | None:
            keys = [k for k in lin_xy if k != exclude]
            if not keys:
                return None
            if exclude is not None and len(keys) == 2 and np.linalg.norm(lin_xy[keys[0]] - lin_xy[keys[1]]) > minor:
                return None  # two disagreeing references: their midpoint means nothing
            return geometric_median(np.array([lin_xy[k] for k in keys]), [lin_w[k] for k in keys])

        def nearest_other(p: dict) -> float | None:
            ds = [float(np.linalg.norm(v - np.array([p["x"], p["y"]]))) for k, v in lin_xy.items()
                  if k != p["lineage"]]
            return round(min(ds), 1) if ds else None

        c = consensus()
        n_lin = len(lin_xy)
        res.summary["ign_n_sources"] = len(elig)
        res.summary["ign_n_lineages"] = n_lin
        res.summary["ign_lineages"] = ";".join(sorted(lin_xy))
        if c is not None:
            clon, clat = frame.lonlat(*c)
            spread = max(float(np.linalg.norm(v - c)) for v in lin_xy.values())
            pair = max([float(np.linalg.norm(lin_xy[a] - lin_xy[b])) for a, b in pairwise(lin_xy)] or [0.0])
            res.summary.update({"ign_consensus_lat": round(clat, 6), "ign_consensus_lon": round(clon, 6),
                                "ign_spread_m": round(spread, 1), "ign_max_pairwise_m": round(pair, 1)})
            if n_lin == 1:
                cls = "single_lineage"
            elif pair <= agree:
                cls = "agree"
            elif pair <= minor:
                cls = "minor"
            else:
                cls = "major"
        else:
            cls = "no_reported_point"
        res.summary["ign_agreement"] = cls

        # per-source errors (leave-one-lineage-out reference)
        for p in pts:
            ref = consensus(exclude=p["lineage"]) if p["eligible"] else c
            pt = Point(p["x"], p["y"])
            e = {"fire_key": fire.key, "source": p["source"], "lineage": p["lineage"], "aspect": "ignition",
                 "lat": p["lat"], "lon": p["lon"], "coord_decimals": p["decimals"],
                 "initial_offset_m": p.get("initial_offset_m"),
                 "dist_to_others_m": None if ref is None else round(float(np.linalg.norm(np.array([p["x"], p["y"]]) - ref)), 1),
                 "dist_to_nearest_other_m": nearest_other(p),
                 "dx_m": None if ref is None else round(p["x"] - ref[0], 1),
                 "dy_m": None if ref is None else round(p["y"] - ref[1], 1),
                 "signed_dist_to_perimeter_m": None if cons_m is None else round(signed_distance_to_polygon_m(pt, cons_m), 1),
                 "dist_to_first_viirs_m": None if "viirs_x" not in sat else round(math.hypot(p["x"] - sat["viirs_x"], p["y"] - sat["viirs_y"]), 1)}
            res.errors.append(e)
            res.points.append({"fire_key": fire.key, "role": "reported", "source": p["source"], "lat": p["lat"], "lon": p["lon"]})
            if p["eligible"] and e["signed_dist_to_perimeter_m"] is not None and e["signed_dist_to_perimeter_m"] > 2000:
                self._flag(res, fire, "ignition", "medium",
                           f"{p['source']} point is {e['signed_dist_to_perimeter_m']/1000:.1f} km outside the consensus perimeter")
            if p["eligible"] and p["decimals"] is not None and p["decimals"] <= 2:
                self._flag(res, fire, "ignition", "low",
                           f"{p['source']} coordinates carry only {p['decimals']} decimals (about 1 km precision)")
        if cls == "major":
            self._flag(res, fire, "ignition", "high",
                       f"reported ignition points disagree by up to {res.summary['ign_max_pairwise_m']/1000:.1f} km across {n_lin} lineages")

        # recommendation
        rec = self._recommend(elig, lin_xy, cons_m, sat, frame, agree, minor, tol, near_sat, cls)
        res.summary.update(rec)
        if c is not None:
            res.points.append({"fire_key": fire.key, "role": "consensus", "source": "consensus",
                               "lat": res.summary["ign_consensus_lat"], "lon": res.summary["ign_consensus_lon"]})
        if "viirs_lat" in sat:
            res.points.append({"fire_key": fire.key, "role": "first_viirs", "source": "viirs",
                               "lat": sat["viirs_lat"], "lon": sat["viirs_lon"]})
        if _ok(rec.get("ign_recommended_lat")):
            res.points.append({"fire_key": fire.key, "role": "recommended", "source": rec["ign_recommended_source"],
                               "lat": rec["ign_recommended_lat"], "lon": rec["ign_recommended_lon"]})
            # compare with FireSET
            if fire.ign_lat is not None:
                fx, fy = frame.xy(fire.ign_lon, fire.ign_lat)
                rx, ry = frame.xy(rec["ign_recommended_lon"], rec["ign_recommended_lat"])
                delta = math.hypot(fx - rx, fy - ry)
                res.summary["fireset_ign_delta_m"] = round(delta, 1)
                if cons_m is not None:
                    res.summary["fireset_ign_signed_dist_to_perimeter_m"] = round(
                        signed_distance_to_polygon_m(Point(fx, fy), cons_m), 1)
            else:
                delta = None
            thr = float(self.ign.get("patch_threshold_m", 500))
            if delta is None or delta > thr:
                res.patch = {"fire_key": fire.key, "FIRE_NAME": fire.name, "YEAR_": fire.year,
                             "OBJECTID": fire.objectid,
                             "Ignition_Lat_old": fire.ign_lat, "Ignition_Lon_old": fire.ign_lon,
                             "Ignition_Lat_new": rec["ign_recommended_lat"], "Ignition_Lon_new": rec["ign_recommended_lon"],
                             "move_m": None if delta is None else round(delta, 1),
                             "new_source": rec["ign_recommended_source"], "confidence": rec["ign_confidence"],
                             "reason": rec["ign_reason"]}
                sev = "high" if (delta is None or delta > minor) else "medium"
                self._flag(res, fire, "fireset_ignition", sev,
                           "FireSET ignition missing" if delta is None else
                           f"FireSET ignition is {delta/1000:.2f} km from the recommended point ({rec['ign_recommended_source']})")

    def _recommend(self, elig, lin_xy, cons_m, sat, frame, agree, minor, tol, near_sat, cls) -> dict[str, Any]:
        out = {"ign_recommended_lat": None, "ign_recommended_lon": None, "ign_recommended_source": None,
               "ign_confidence": None, "ign_reason": None, "ign_recommended_score": None}
        if not elig:
            if "viirs_lat" in sat:
                out.update({"ign_recommended_lat": round(sat["viirs_lat"], 6), "ign_recommended_lon": round(sat["viirs_lon"], 6),
                            "ign_recommended_source": "first_viirs_overpass", "ign_confidence": "low",
                            "ign_reason": "no agency point of origin found; centre of the first VIIRS overpass"})
            return out
        n_lin = len(lin_xy)
        scored = []
        for p in elig:
            xy = np.array([p["x"], p["y"]])
            comps, wts = [], []
            if n_lin > 1:
                others = [k for k in lin_xy if k != p["lineage"]]
                sup = sum(1 for k in others if np.linalg.norm(lin_xy[k] - xy) <= minor) / len(others)
                comps.append(sup); wts.append(0.35)
            if cons_m is not None:
                sd = signed_distance_to_polygon_m(Point(*xy), cons_m)
                comps.append(1.0 if sd <= tol else max(0.0, 1 - (sd - tol) / 2000)); wts.append(0.25)
            if "viirs_x" in sat:
                ds = math.hypot(xy[0] - sat["viirs_x"], xy[1] - sat["viirs_y"])
                comps.append(1.0 if ds <= near_sat else max(0.0, 1 - (ds - near_sat) / 8500)); wts.append(0.15)
            if "goes_lat" in sat:
                gx, gy = frame.xy(sat["goes_lon"], sat["goes_lat"])
                dg = math.hypot(xy[0] - gx, xy[1] - gy)
                comps.append(1.0 if dg <= 3000 else max(0.0, 1 - (dg - 3000) / 7000)); wts.append(0.05)
            comps.append(TRUST_W.get(p["trust"], 0.5)); wts.append(0.10)
            dec = p["decimals"]
            comps.append(1.0 if dec is None or dec >= 4 else 0.8 if dec == 3 else 0.5); wts.append(0.10)
            score = float(np.dot(comps, wts) / sum(wts))
            scored.append((score, p))
        scored.sort(key=lambda t: -t[0])
        best_score, best = scored[0]
        if cls == "agree":
            conf, why = "high", "all reporting lineages agree"
        elif cls == "single_lineage":
            conf = "medium" if best_score >= 0.75 else "low"
            why = "only one reporting lineage"
        elif cls == "minor":
            conf = "medium" if best_score >= 0.7 else "low"
            why = "reporting lineages differ by under the minor threshold"
        else:
            conf = "medium" if best_score >= 0.8 else "low"
            why = "reporting lineages disagree; chose the point best supported by the others, the perimeter and satellite detections"
        out.update({"ign_recommended_lat": round(best["lat"], 6), "ign_recommended_lon": round(best["lon"], 6),
                    "ign_recommended_source": best["source"], "ign_confidence": conf,
                    "ign_reason": why, "ign_recommended_score": round(best_score, 3)})
        return out

    # ------------------------------------------------------------ timing
    def _timing(self, fire: Fire, recs: pd.DataFrame, sat: dict[str, Any], res: FireResult) -> None:
        rows = []
        if len(recs):
            for _, r in recs.iterrows():
                sp = self._spec(r["source"])
                if r["source"] != "fireset_subset" and (not sp or "timing" not in sp.roles):
                    continue
                if not _ts_ok(r.get("discovery_utc")):
                    continue
                rows.append({"source": r["source"], "lineage": r["lineage"], "t": pd.Timestamp(r["discovery_utc"]),
                             "precision": r.get("discovery_precision") or "datetime",
                             "end": next((pd.Timestamp(r[k]) for k in ("containment_utc", "control_utc", "out_utc")
                                          if k in r and _ts_ok(r.get(k))), None),
                             "eligible": self._eligible(r["source"]) and r["source"] != "fireset_subset"})
        dt_rows = [r for r in rows if r["precision"] == "datetime" and r["eligible"]]
        by_lin: dict[str, list[pd.Timestamp]] = {}
        for r in dt_rows:
            by_lin.setdefault(r["lineage"], []).append(r["t"])

        def median_time(exclude: str | None = None) -> pd.Timestamp | None:
            vals = [min(v) for k, v in by_lin.items() if k != exclude]
            if not vals:
                return None
            if exclude is not None and len(vals) == 2 and abs((vals[0] - vals[1]).total_seconds()) > 3 * 3600:
                return None  # two disagreeing references: their midpoint means nothing
            return pd.Timestamp(int(np.median([v.value for v in vals])), tz="UTC")

        cons = median_time()
        if cons is not None:
            res.summary["discovery_consensus_utc"] = cons
            ts = [min(v) for v in by_lin.values()]
            spread_h = (max(ts) - min(ts)).total_seconds() / 3600
            res.summary["discovery_spread_h"] = round(spread_h, 2)
            if spread_h > 6:
                self._flag(res, fire, "timing", "medium", f"discovery times differ by {spread_h:.1f} h across lineages")
        tz_offsets = [float(x) for x in self.tim.get("tz_suspect_offsets_h", [7, 8])]
        tz_tol = float(self.tim.get("tz_suspect_tolerance_h", 0.25))
        for r in rows:
            ref = median_time(exclude=r["lineage"]) if r["eligible"] else cons
            e = {"fire_key": fire.key, "source": r["source"], "lineage": r["lineage"], "aspect": "timing",
                 "discovery_utc": r["t"], "discovery_precision": r["precision"]}
            if ref is not None:
                if r["precision"] == "date":
                    pac = self.tim.get("pacific_tz", "America/Los_Angeles")
                    e["discovery_day_offset"] = (r["t"].tz_convert(pac).normalize().tz_localize(None)
                                                 - ref.tz_convert(pac).normalize().tz_localize(None)).days
                else:
                    off = (r["t"] - ref).total_seconds() / 3600
                    e["discovery_offset_h"] = round(off, 2)
                    if any(abs(abs(off) - o) <= tz_tol for o in tz_offsets):
                        e["tz_suspect"] = True
                        self._flag(res, fire, "timing", "medium",
                                   f"{r['source']} discovery is offset {off:+.2f} h, which looks like a time-zone error")
            if r["end"] is not None:
                e["end_utc"] = r["end"]
            res.errors.append(e)
        ends = [r["end"] for r in rows if r["end"] is not None and r["eligible"]]
        if len(ends) >= 2:
            res.summary["end_spread_days"] = round((max(ends) - min(ends)).total_seconds() / 86400, 1)
        ref_t = cons or fire.start_utc
        if ref_t is not None and "viirs_time" in sat:
            lag = (sat["viirs_time"] - ref_t).total_seconds() / 3600
            res.summary["viirs_minus_discovery_h"] = round(lag, 2)
            if lag < -3:
                self._flag(res, fire, "timing", "medium",
                           f"first VIIRS detection near the fire is {-lag:.1f} h before the reported discovery")
        if ref_t is not None and "hms_time" in sat:
            res.summary["hms_minus_discovery_h"] = round((sat["hms_time"] - ref_t).total_seconds() / 3600, 2)
        if ref_t is not None and "goes_time" in sat:
            res.summary["goes_minus_discovery_h"] = round((sat["goes_time"] - ref_t).total_seconds() / 3600, 2)

    # ------------------------------------------------------------ size
    def _size(self, fire: Fire, recs: pd.DataFrame, cons_m, res: FireResult) -> None:
        vals = []
        if len(recs):
            for _, r in recs.iterrows():
                sp = self._spec(r["source"])
                if r["source"] != "fireset_subset" and (not sp or "size" not in sp.roles):
                    continue
                if _ok(r.get("size_acres")) and r["size_acres"] > 0:
                    vals.append((r["source"], r["lineage"], float(r["size_acres"]), self._eligible(r["source"])))
        ref = area_acres(cons_m) if cons_m is not None else (
            float(np.median([v for _, _, v, e in vals if e])) if any(e for *_, e in vals) else None)
        res.summary["size_reference_acres"] = None if ref is None else round(ref, 1)
        res.summary["size_reference"] = "consensus perimeter" if cons_m is not None else "median reported"
        for sid, lin, v, _ in vals:
            res.errors.append({"fire_key": fire.key, "source": sid, "lineage": lin, "aspect": "size",
                               "reported_acres": v,
                               "rel_error_vs_reference": None if not ref else round((v - ref) / ref, 4)})
        el = [v for _, _, v, e in vals if e]
        if len(el) >= 2:
            ratio = max(el) / min(el)
            res.summary["size_max_min_ratio"] = round(ratio, 3)
            if ratio > 1.25:
                self._flag(res, fire, "size", "low", f"reported sizes differ by a factor of {ratio:.2f}")

    # ------------------------------------------------------------ cause
    def _cause(self, fire: Fire, recs: pd.DataFrame, res: FireResult) -> None:
        if not len(recs) or "cause_class" not in recs:
            return
        c = recs[recs["cause_class"].notna() & recs["source"].map(self._eligible)]
        if not len(c):
            return
        counts = c["cause_class"].value_counts()
        res.summary["cause_consensus"] = counts.index[0]
        res.summary["cause_agreement"] = round(counts.iloc[0] / counts.sum(), 3)
        res.summary["cause_by_source"] = ";".join(f"{s}={v}" for s, v in zip(c["source"], c["cause_class"]))
        if {"Human", "Natural"} <= set(counts.index):
            self._flag(res, fire, "cause", "medium", "sources disagree between human and natural cause")

    # ------------------------------------------------------------ identifiers
    def _identifiers(self, fire: Fire, recs: pd.DataFrame, res: FireResult) -> None:
        if not len(recs) or not (recs["source"] != "fireset_subset").any():
            self._flag(res, fire, "match", "high", "no source matched this fire")
            return
        ids = {v for v in recs.loc[recs["source"] != "fireset_subset", "irwin_id"].dropna()} if "irwin_id" in recs else set()
        res.summary["irwin_ids_seen"] = ";".join(sorted(ids))
        if len(ids) > 1:
            self._flag(res, fire, "identifier", "medium", f"{len(ids)} different IRWIN IDs across sources")
        if fire.irwin_id and ids and fire.irwin_id not in ids:
            self._flag(res, fire, "identifier", "high", "FireSET IRWIN ID is not the one the sources carry")
        names = {str(v).strip().upper() for v in recs["name"].dropna()} if "name" in recs else set()
        res.summary["names_seen"] = ";".join(sorted(names))
        if "ambiguous" in recs and recs["ambiguous"].fillna(False).astype(bool).any():
            amb = recs.loc[recs["ambiguous"].fillna(False).astype(bool), "source"].tolist()
            self._flag(res, fire, "match", "low", f"ambiguous match in: {', '.join(amb)}")
        if "id_location_conflict_m" in recs:
            far = recs[pd.to_numeric(recs["id_location_conflict_m"], errors="coerce") > 20000]
            for _, r in far.iterrows():
                self._flag(res, fire, "identifier", "high",
                           f"{r['source']} record with the same ID sits {r['id_location_conflict_m']/1000:.0f} km from the fire")
