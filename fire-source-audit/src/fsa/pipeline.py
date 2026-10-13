"""Stages: fetch (network/local files -> cache), match, analyze, report."""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd

from . import cache
from .analysis import FireAnalyzer, FireResult
from .config import Settings, SourceSpec
from .fires import Fire, fireset_records
from .match import match_fire
from .normalize import normalize_rows
from .sources import DETECTION_COLUMNS, EvidenceSource, RecordSource, build_sources
from .sources.http import Http

log = logging.getLogger(__name__)


class Pipeline:
    def __init__(self, settings: Settings, specs: dict[str, SourceSpec], defaults: dict[str, Any],
                 only_sources: list[str] | None = None):
        self.settings = settings
        self.specs = specs
        self.defaults = defaults
        self.only = only_sources
        self.cache_dir = settings.cache_dir
        self.out_dir = settings.out_dir

    # ------------------------------------------------------------ fetch
    def _sources(self, http_factory=lambda: None):
        recs, evid = build_sources(self.specs, self.settings, self.defaults, None, self.only)
        for s in list(recs.values()) + list(evid.values()):
            s.http = http_factory()
        return recs, evid

    def fetch(self, fires: list[Fire], force: bool = False, workers: int = 4) -> pd.DataFrame:
        recs, evid = self._sources(lambda: Http.from_settings(self.settings))
        status: list[dict[str, Any]] = []

        def run_record(src: RecordSource) -> list[dict[str, Any]]:
            out = []
            ok, why = src.available()
            if not ok:
                log.warning("skip %s: %s", src.spec.id, why)
                return [{"source": src.spec.id, "fire_key": "*", "status": "skipped", "detail": why}]
            if src.bulk:
                try:
                    src.prefetch()
                except Exception as e:  # noqa: BLE001
                    log.error("prefetch failed for %s: %s", src.spec.id, e)
                    return [{"source": src.spec.id, "fire_key": "*", "status": "error", "detail": str(e)}]
            for i, f in enumerate(fires, 1):
                p = cache.raw_path(self.cache_dir, src.spec.id, f.key)
                if p.exists() and not force:
                    out.append({"source": src.spec.id, "fire_key": f.key, "status": "cached"})
                    continue
                try:
                    gdf = src.fetch(f)
                    cache.save_gdf(p, gdf)
                    cache.error_path(self.cache_dir, src.spec.id, f.key).unlink(missing_ok=True)
                    out.append({"source": src.spec.id, "fire_key": f.key, "status": "ok", "rows": len(gdf)})
                except Exception as e:  # noqa: BLE001 - record and continue
                    cache.save_error(cache.error_path(self.cache_dir, src.spec.id, f.key), repr(e))
                    out.append({"source": src.spec.id, "fire_key": f.key, "status": "error", "detail": repr(e)[:300]})
                if i % 50 == 0:
                    log.info("%s: %d/%d fires", src.spec.id, i, len(fires))
            return out

        def run_evidence(src: EvidenceSource) -> list[dict[str, Any]]:
            out = []
            ok, why = src.available()
            if not ok:
                return [{"source": src.spec.id, "fire_key": "*", "status": "skipped", "detail": why}]
            for f in fires:
                p = cache.evidence_path(self.cache_dir, src.spec.id, f.key)
                if p.exists() and not force:
                    out.append({"source": src.spec.id, "fire_key": f.key, "status": "cached"})
                    continue
                try:
                    d = src.detections(f)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    d.to_csv(p, index=False)
                    out.append({"source": src.spec.id, "fire_key": f.key, "status": "ok", "rows": len(d)})
                except Exception as e:  # noqa: BLE001
                    out.append({"source": src.spec.id, "fire_key": f.key, "status": "error", "detail": repr(e)[:300]})
            return out

        jobs = [(run_record, s) for s in recs.values()] + [(run_evidence, s) for s in evid.values()]
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            futs = [ex.submit(fn, s) for fn, s in jobs]
            for fut in as_completed(futs):
                status.extend(fut.result())

        goes_spec = self.specs.get("goes_fdc")
        if goes_spec and (goes_spec.enabled or (self.only and "goes_fdc" in self.only)):
            status.extend(self.fetch_goes(fires, force))
        df = pd.DataFrame(status)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        df.to_csv(self.out_dir / "fetch_status.csv", index=False)
        return df

    def fetch_goes(self, fires: list[Fire], force: bool = False) -> list[dict[str, Any]]:
        from .goes import earliest_detection
        cfg = self.settings.raw.get("goes", {})
        out = []
        for f in fires:
            p = self.cache_dir / "evidence" / "goes_fdc" / f"{f.key}.json"
            if p.exists() and not force:
                out.append({"source": "goes_fdc", "fire_key": f.key, "status": "cached"})
                continue
            lon, lat = (f.ign_lon, f.ign_lat) if f.ign_lat is not None else f.center
            t = f.start_utc or f.first_detection_utc
            if t is None:
                continue
            try:
                hit = earliest_detection(lon, lat, t, cfg)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps(hit, default=str))
                out.append({"source": "goes_fdc", "fire_key": f.key, "status": "ok", "rows": int(hit is not None)})
            except Exception as e:  # noqa: BLE001
                out.append({"source": "goes_fdc", "fire_key": f.key, "status": "error", "detail": repr(e)[:300]})
        return out

    # ------------------------------------------------------------ match
    def match(self, fires: list[Fire]) -> gpd.GeoDataFrame:
        mcfg = self.settings.raw.get("matching", {})
        rows: list[dict[str, Any]] = []
        for sid, spec in self.specs.items():
            if spec.kind in ("viirs_h5", "firms_csv", "hms_text", "goes_fdc", "fireset"):
                continue
            src_dir = self.cache_dir / "raw" / sid
            if not src_dir.exists():
                continue
            is_perim = "perimeter" in spec.roles
            for f in fires:
                p = cache.raw_path(self.cache_dir, sid, f.key)
                if not p.exists():
                    continue
                raw = cache.load_gdf(p)
                if len(raw) == 0:
                    continue
                norm = normalize_rows(raw, spec)
                rows.extend(match_fire(f, norm, mcfg, is_perim))
        fs = fireset_records(fires)
        df = pd.concat([pd.DataFrame(rows), fs], ignore_index=True) if rows else fs
        if "geometry" not in df:
            df["geometry"] = None
        gdf = gpd.GeoDataFrame(df, geometry="geometry", crs="EPSG:4326")
        self.out_dir.mkdir(parents=True, exist_ok=True)
        flat = gdf.drop(columns=["geometry"]).copy()
        if "match_components" in flat:
            flat["match_components"] = flat["match_components"].map(lambda v: json.dumps(v) if isinstance(v, dict) else v)
        flat.to_csv(self.out_dir / "matched_records.csv", index=False)
        return gdf

    # ------------------------------------------------------------ analyze
    def _detections(self, fire: Fire) -> pd.DataFrame:
        parts = []
        ev_dir = self.cache_dir / "evidence"
        if ev_dir.exists():
            for sdir in ev_dir.iterdir():
                p = sdir / f"{fire.key}.csv"
                if p.exists():
                    d = pd.read_csv(p)
                    if len(d):
                        d["time_utc"] = pd.to_datetime(d["time_utc"], utc=True, errors="coerce")
                        parts.append(d)
        return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=DETECTION_COLUMNS)

    def _goes(self, fire: Fire) -> dict[str, Any] | None:
        p = self.cache_dir / "evidence" / "goes_fdc" / f"{fire.key}.json"
        if not p.exists():
            return None
        v = json.loads(p.read_text())
        return v or None

    def analyze(self, fires: list[Fire], matched: gpd.GeoDataFrame) -> list[FireResult]:
        an = FireAnalyzer(self.specs, self.settings)
        out = []
        groups = {k: g for k, g in matched.groupby("fire_key")} if len(matched) else {}
        for i, f in enumerate(fires, 1):
            recs = groups.get(f.key, pd.DataFrame())
            try:
                out.append(an.analyze(f, recs, self._detections(f), self._goes(f)))
            except Exception as e:  # noqa: BLE001
                log.exception("analysis failed for %s", f.key)
                r = FireResult(summary={"fire_key": f.key, "fire_name": f.name, "year": f.year,
                                        "analysis_error": repr(e)[:300]})
                r.flags.append({"fire_key": f.key, "category": "analysis", "severity": "high",
                                "message": f"analysis error: {e!r}"[:300]})
                out.append(r)
            if i % 100 == 0:
                log.info("analyzed %d/%d", i, len(fires))
        return out
