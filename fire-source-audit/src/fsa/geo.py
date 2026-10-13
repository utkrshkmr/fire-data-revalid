"""Geometry helpers: projection, distances, perimeter comparison, consensus."""
from __future__ import annotations

import math
from typing import Iterable, Sequence

import numpy as np
from pyproj import CRS, Transformer
from shapely import make_valid
from shapely.geometry import MultiPolygon, Point, Polygon, shape
from shapely.geometry.base import BaseGeometry
import shapely
from shapely.ops import unary_union

M2_PER_ACRE = 4046.8564224
EARTH_R = 6371008.8


def utm_crs(lon: float, lat: float) -> CRS:
    zone = int(math.floor((lon + 180) / 6) + 1)
    return CRS.from_epsg((32600 if lat >= 0 else 32700) + zone)


class LocalFrame:
    """A per-fire metric frame: WGS84 <-> UTM zone of the fire centre."""

    def __init__(self, lon: float, lat: float):
        self.crs = utm_crs(lon, lat)
        self._fwd = Transformer.from_crs(4326, self.crs, always_xy=True)
        self._inv = Transformer.from_crs(self.crs, 4326, always_xy=True)

    def xy(self, lon: float, lat: float) -> tuple[float, float]:
        return self._fwd.transform(lon, lat)

    def lonlat(self, x: float, y: float) -> tuple[float, float]:
        return self._inv.transform(x, y)

    def geom(self, g: BaseGeometry) -> BaseGeometry:
        return shapely.transform(g, lambda x, y: self._fwd.transform(x, y), interleaved=False)

    def geom_to_wgs(self, g: BaseGeometry) -> BaseGeometry:
        return shapely.transform(g, lambda x, y: self._inv.transform(x, y), interleaved=False)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(min(1.0, math.sqrt(a)))


def clean_polygon(g: BaseGeometry | None) -> BaseGeometry | None:
    """Return a valid (Multi)Polygon or None."""
    if g is None or g.is_empty:
        return None
    if not g.is_valid:
        g = make_valid(g)
    if g.geom_type == "GeometryCollection":
        polys = [p for p in g.geoms if p.geom_type in ("Polygon", "MultiPolygon")]
        if not polys:
            return None
        g = unary_union(polys)
    if g.geom_type not in ("Polygon", "MultiPolygon"):
        return None
    return g if not g.is_empty else None


def area_acres(g_m: BaseGeometry | None) -> float | None:
    return None if g_m is None else g_m.area / M2_PER_ACRE


def iou(a: BaseGeometry | None, b: BaseGeometry | None) -> float | None:
    if a is None or b is None:
        return None
    union = a.union(b).area
    if union <= 0:
        return None
    return a.intersection(b).area / union


def hausdorff_m(a: BaseGeometry | None, b: BaseGeometry | None) -> float | None:
    if a is None or b is None:
        return None
    return a.boundary.hausdorff_distance(b.boundary)


def centroid_offset_m(a: BaseGeometry | None, b: BaseGeometry | None) -> float | None:
    if a is None or b is None:
        return None
    return a.centroid.distance(b.centroid)


def signed_distance_to_polygon_m(pt: Point, poly: BaseGeometry | None) -> float | None:
    """Negative inside the polygon (depth), positive outside (distance to edge)."""
    if poly is None:
        return None
    d = poly.boundary.distance(pt)
    return -d if poly.contains(pt) else d


def geometric_median(xy: np.ndarray, weights: Sequence[float] | None = None,
                     tol: float = 0.01, max_iter: int = 500) -> np.ndarray:
    """Weiszfeld's algorithm in a metric frame. xy has shape (n, 2)."""
    xy = np.asarray(xy, dtype=float)
    if len(xy) == 1:
        return xy[0].copy()
    w = np.ones(len(xy)) if weights is None else np.asarray(weights, dtype=float)
    est = np.average(xy, axis=0, weights=w)
    for _ in range(max_iter):
        d = np.linalg.norm(xy - est, axis=1)
        if np.any(d < 1e-9):
            return xy[np.argmin(d)].copy()
        inv = w / d
        new = (xy * inv[:, None]).sum(axis=0) / inv.sum()
        if np.linalg.norm(new - est) < tol:
            return new
        est = new
    return est


def medoid(xy: np.ndarray) -> np.ndarray:
    xy = np.asarray(xy, dtype=float)
    if len(xy) <= 2:
        return xy.mean(axis=0)
    d = np.linalg.norm(xy[:, None, :] - xy[None, :, :], axis=2).sum(axis=1)
    return xy[np.argmin(d)].copy()


def vote_consensus(polys_m: list[BaseGeometry], res_m: float, vote: float,
                   max_cells: int) -> BaseGeometry | None:
    """Cells covered by at least `vote` share of the polygons, as a polygon in the same metric frame."""
    polys = [p for p in polys_m if p is not None and not p.is_empty]
    if not polys:
        return None
    if len(polys) == 1:
        return polys[0]
    if len(polys) == 2:
        # with two inputs a 0.5 vote is the union and >0.5 the intersection
        inter = polys[0].intersection(polys[1])
        return clean_polygon(polys[0].union(polys[1]) if vote <= 0.5 else inter)
    import rasterio.features
    from rasterio.transform import from_origin

    minx, miny, maxx, maxy = unary_union(polys).bounds
    nx = int(math.ceil((maxx - minx) / res_m)) + 2
    ny = int(math.ceil((maxy - miny) / res_m)) + 2
    if nx * ny > max_cells:
        return None
    tr = from_origin(minx - res_m, maxy + res_m, res_m, res_m)
    count = np.zeros((ny, nx), dtype=np.uint16)
    for p in polys:
        count += rasterio.features.rasterize([(p, 1)], out_shape=(ny, nx), transform=tr, fill=0, dtype="uint8")
    need = math.ceil(vote * len(polys) - 1e-9)
    mask = (count >= need).astype("uint8")
    if not mask.any():
        return None
    shapes = [shape(g) for g, v in rasterio.features.shapes(mask, mask=mask.astype(bool), transform=tr) if v == 1]
    return clean_polygon(unary_union(shapes))


def as_multipolygon(g: BaseGeometry | None) -> MultiPolygon | None:
    if g is None:
        return None
    if isinstance(g, Polygon):
        return MultiPolygon([g])
    return g if isinstance(g, MultiPolygon) else None


def pairwise(items: Iterable) -> list[tuple]:
    items = list(items)
    return [(items[i], items[j]) for i in range(len(items)) for j in range(i + 1, len(items))]
