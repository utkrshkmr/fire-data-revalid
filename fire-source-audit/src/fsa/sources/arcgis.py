"""Generic ArcGIS REST layer querying (FeatureServer and MapServer)."""
from __future__ import annotations

import logging
from typing import Any

import geopandas as gpd
from shapely.geometry import MultiPolygon, Point, Polygon, shape
from shapely.geometry.base import BaseGeometry

from .http import Http

log = logging.getLogger(__name__)


class ArcGISError(RuntimeError):
    pass


def _signed_area(ring: list[list[float]]) -> float:
    s = 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        s += x1 * y2 - x2 * y1
    return s / 2


def esri_to_shapely(g: dict[str, Any] | None) -> BaseGeometry | None:
    """Convert an Esri JSON geometry (point or polygon) to shapely."""
    if not g:
        return None
    if "x" in g and "y" in g:
        if g["x"] is None or g["y"] is None:
            return None
        return Point(g["x"], g["y"])
    if "rings" in g:
        outers: list[list] = []
        holes: list[list] = []
        for ring in g["rings"]:
            ring = [tuple(p[:2]) for p in ring]
            if len(ring) < 4:
                continue
            # Esri: exterior rings clockwise (negative shoelace area), holes counter-clockwise
            (outers if _signed_area([list(p) for p in ring]) < 0 else holes).append(ring)
        if not outers and holes:
            outers, holes = holes, []
        polys = []
        for o in outers:
            op = Polygon(o)
            mine = [h for h in holes if op.contains(Point(h[0]))]
            polys.append(Polygon(o, mine))
        if not polys:
            return None
        return polys[0] if len(polys) == 1 else MultiPolygon(polys)
    if "paths" in g:
        return None
    return None


def features_to_gdf(features: list[dict[str, Any]], fmt: str) -> gpd.GeoDataFrame:
    rows, geoms = [], []
    for f in features:
        props = f.get("properties") if fmt == "geojson" else f.get("attributes")
        geom = f.get("geometry")
        rows.append(dict(props or {}))
        if fmt == "geojson":
            geoms.append(shape(geom) if geom else None)
        else:
            geoms.append(esri_to_shapely(geom))
    return gpd.GeoDataFrame(rows, geometry=geoms, crs="EPSG:4326")


class ArcGISLayer:
    def __init__(self, url: str, http: Http, page_size: int = 1000):
        self.url = url.rstrip("/")
        self.http = http
        self.page_size = page_size

    def metadata(self) -> dict[str, Any]:
        data = self.http.get_json(self.url, {"f": "json"})
        if "error" in data:
            raise ArcGISError(data["error"])
        return data

    def _query_pages(self, params: dict[str, Any], fmt: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        offset = 0
        while True:
            p = dict(params, f=fmt, resultOffset=offset, resultRecordCount=self.page_size)
            data = self.http.get_json(self.url + "/query", p, method="POST")
            if isinstance(data, dict) and "error" in data:
                raise ArcGISError(data["error"])
            feats = data.get("features", [])
            out.extend(feats)
            more = data.get("exceededTransferLimit") or (
                data.get("properties", {}) or {}).get("exceededTransferLimit")
            if not more or not feats:
                break
            offset += len(feats)
            if offset > 200000:
                log.warning("stopping pagination at %d features for %s", offset, self.url)
                break
        return out

    def query(self, where: str = "1=1", envelope: tuple[float, float, float, float] | None = None,
              return_geometry: bool = True) -> gpd.GeoDataFrame:
        params: dict[str, Any] = {
            "where": where or "1=1",
            "outFields": "*",
            "returnGeometry": "true" if return_geometry else "false",
            "outSR": 4326,
        }
        if envelope is not None:
            xmin, ymin, xmax, ymax = envelope
            params.update({
                "geometry": f"{xmin},{ymin},{xmax},{ymax}",
                "geometryType": "esriGeometryEnvelope",
                "inSR": 4326,
                "spatialRel": "esriSpatialRelIntersects",
            })
        last: Exception | None = None
        for fmt in ("geojson", "json"):
            try:
                feats = self._query_pages(params, fmt)
                return features_to_gdf(feats, fmt)
            except (ArcGISError, ValueError) as e:  # ValueError: bad JSON / unsupported format
                last = e
                log.debug("query with f=%s failed on %s: %s", fmt, self.url, e)
        raise ArcGISError(f"query failed on {self.url}: {last}")

    def query_with_fallback(self, where: str | None, envelope) -> tuple[gpd.GeoDataFrame, str]:
        """Try the configured where clause; if the service rejects it, fall back to a spatial-only query."""
        if where:
            try:
                return self.query(where, envelope), where
            except ArcGISError as e:
                log.info("where clause rejected (%s); retrying spatial-only on %s", e, self.url)
        return self.query("1=1", envelope), "1=1"
