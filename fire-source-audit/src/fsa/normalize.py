"""Turn raw source rows into the canonical record schema."""
from __future__ import annotations

import math
import re
from difflib import SequenceMatcher
from typing import Any, Iterable

import numpy as np
import pandas as pd

from .config import SourceSpec

CANONICAL_COLUMNS = [
    "source", "lineage", "trust", "source_record_id",
    "name", "irwin_id", "unique_fire_id", "local_number", "unit", "year", "complex_name",
    "lat", "lon", "ignition_kind", "coord_decimals",
    "discovery_utc", "discovery_precision", "containment_utc", "control_utc", "out_utc",
    "size_acres", "cause_raw", "cause_class",
    "perimeter_datetime_utc", "map_method", "perimeter_source",
]

# ---------------------------------------------------------------- field lookup


def resolve_column(columns: Iterable[str], candidates: list[str] | None) -> str | None:
    if not candidates:
        return None
    lower = {c.lower(): c for c in columns}
    for cand in candidates:
        hit = lower.get(cand.lower())
        if hit is not None:
            return hit
    return None


def pick(row: pd.Series | dict, spec: SourceSpec, key: str, columns: Iterable[str]) -> Any:
    col = resolve_column(columns, spec.fields.get(key))
    if col is None:
        return None
    val = row[col]
    if val is None:
        return None
    if isinstance(val, float) and math.isnan(val):
        return None
    if isinstance(val, str) and val.strip() in ("", "<Null>", "None", "nan", "NaN"):
        return None
    return val


# ---------------------------------------------------------------- identifiers and names

_IRWIN_RE = re.compile(r"[{}\s]")


def norm_irwin(val: Any) -> str | None:
    if val is None:
        return None
    s = _IRWIN_RE.sub("", str(val)).upper()
    return s if len(s) >= 32 else None


def norm_ufi(val: Any) -> str | None:
    if val is None:
        return None
    s = str(val).strip().upper()
    return s or None


def incident_number(val: Any) -> str | None:
    """Digits of a local incident number or the last segment of a UniqueFireIdentifier, no leading zeros."""
    if val is None:
        return None
    s = str(val).strip()
    if "-" in s:
        s = s.split("-")[-1]
    digits = re.sub(r"\D", "", s)
    return digits.lstrip("0") or None


_NAME_DROP = {"FIRE", "FIRES", "COMPLEX", "CPX", "WILDFIRE", "INCIDENT", "WF", "THE"}


def norm_name(val: Any) -> str:
    if val is None:
        return ""
    s = re.sub(r"[^A-Z0-9 ]+", " ", str(val).upper())
    toks = [t for t in s.split() if t not in _NAME_DROP]
    return " ".join(toks)


def name_similarity(a: Any, b: Any) -> float:
    na, nb = norm_name(a), norm_name(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ta, tb = set(na.split()), set(nb.split())
    jacc = len(ta & tb) / max(1, len(ta | tb))
    seq = SequenceMatcher(None, na.replace(" ", ""), nb.replace(" ", "")).ratio()
    return max(jacc, seq)


# ---------------------------------------------------------------- times


def _julian_to_ts(x: float) -> pd.Timestamp:
    return pd.Timestamp("1970-01-01") + pd.to_timedelta(x - 2440587.5, unit="D")


def to_utc(val: Any, naive_tz: str = "UTC", time_hhmm: Any = None) -> tuple[pd.Timestamp | None, str | None]:
    """Parse a date value to a UTC timestamp. Returns (timestamp, precision) with precision 'datetime' or 'date'."""
    if val is None:
        return None, None
    precision = "datetime"
    ts: pd.Timestamp | None
    if isinstance(val, (int, float, np.integer, np.floating)) and not isinstance(val, bool):
        x = float(val)
        if math.isnan(x):
            return None, None
        if 2.3e6 < x < 2.6e6:           # Julian day (older FPA-FOD releases)
            ts = _julian_to_ts(x)
            precision = "date"
        elif x > 1e11:                    # ArcGIS epoch milliseconds
            ts = pd.Timestamp(int(x), unit="ms", tz="UTC")
        elif x > 1e8:                     # epoch seconds
            ts = pd.Timestamp(int(x), unit="s", tz="UTC")
        else:
            return None, None
    else:
        s = str(val).strip()
        if not s:
            return None, None
        try:
            ts = pd.Timestamp(s)
        except (ValueError, TypeError):
            try:
                ts = pd.to_datetime(s, errors="coerce")
            except (ValueError, TypeError):
                return None, None
            if ts is pd.NaT or ts is None:
                return None, None
        if re.fullmatch(r"\d{1,4}[-/]\d{1,2}[-/]\d{1,4}", s) or (len(s) <= 10 and ":" not in s):
            precision = "date"
    if ts is None or pd.isna(ts):
        return None, None
    if time_hhmm is not None and not (isinstance(time_hhmm, float) and math.isnan(time_hhmm)):
        digits = re.sub(r"\D", "", str(time_hhmm).split(".")[0]).zfill(4)[-4:]
        try:
            hh, mm = int(digits[:2]), int(digits[2:])
            if hh < 24 and mm < 60:
                base = ts.tz_localize(None) if ts.tzinfo is not None else ts
                ts = base.normalize() + pd.Timedelta(hours=hh, minutes=mm)
                precision = "datetime"
        except ValueError:
            pass
    if ts.tzinfo is None:
        try:
            ts = ts.tz_localize(naive_tz, ambiguous="NaT", nonexistent="shift_forward")
        except (TypeError, ValueError):
            ts = ts.tz_localize("UTC")
        if pd.isna(ts):
            return None, None
    return ts.tz_convert("UTC"), precision


# ---------------------------------------------------------------- cause

_FRAP_CAUSE = {1: "Natural", 17: "Natural", 14: "Undetermined"}
_HUMAN_WORDS = ("human", "equipment", "arson", "incendiary", "debris", "power", "vehicle", "campfire",
                "smok", "railroad", "children", "playing", "firearm", "fireworks", "misc", "structure",
                "aircraft", "escaped", "burning", "recreation", "utilit")


def classify_cause(val: Any, codes: str | None = None) -> str | None:
    if val is None:
        return None
    if codes == "frap":
        try:
            code = int(float(val))
        except (TypeError, ValueError):
            code = None
        if code is not None:
            if code <= 0:
                return None
            return _FRAP_CAUSE.get(code, "Human")
    s = str(val).strip().lower()
    if not s:
        return None
    if "natural" in s or "lightning" in s or "volcan" in s:
        return "Natural"
    if "undeterm" in s or "unknown" in s or "unidentified" in s or "missing" in s:
        return "Undetermined"
    if any(w in s for w in _HUMAN_WORDS):
        return "Human"
    return "Undetermined" if s in ("u", "9") else None


# ---------------------------------------------------------------- coordinates


def coord_decimals(lat: Any, lon: Any) -> int | None:
    """Fewest decimal places carried by either coordinate (a proxy for reported precision)."""
    def dec(v: Any) -> int | None:
        if v is None:
            return None
        s = repr(float(v))
        if "e" in s:
            return 0
        return len(s.split(".")[1].rstrip("0")) if "." in s else 0
    a, b = dec(lat), dec(lon)
    if a is None or b is None:
        return None
    return min(a, b)


def valid_latlon(lat: Any, lon: Any) -> bool:
    try:
        la, lo = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    if math.isnan(la) or math.isnan(lo):
        return False
    if la == 0 and lo == 0:
        return False
    return -90 <= la <= 90 and -180 <= lo <= 180


# ---------------------------------------------------------------- row normalisation


def normalize_rows(df: pd.DataFrame, spec: SourceSpec, geometry_col: str | None = "geometry") -> pd.DataFrame:
    """Map raw rows of one source into CANONICAL_COLUMNS (+ geometry if present)."""
    cols = [c for c in df.columns if c != geometry_col]
    out: list[dict[str, Any]] = []
    for idx, row in df.iterrows():
        rec: dict[str, Any] = {c: None for c in CANONICAL_COLUMNS}
        rec["source"] = spec.id
        rec["lineage"] = spec.lineage
        rec["trust"] = spec.trust
        if spec.lineage_from_field:
            fcol = resolve_column(cols, [spec.lineage_from_field.get("field", "")])
            if fcol is not None and row[fcol] is not None:
                key = str(row[fcol]).strip()
                mapping = {str(k).upper(): v for k, v in (spec.lineage_from_field.get("map") or {}).items()}
                rec["lineage"] = mapping.get(key.upper(), f"{spec.lineage}:{key.upper()}")
        sid = pick(row, spec, "source_id", cols)
        rec["source_record_id"] = str(sid) if sid is not None else f"{spec.id}:{idx}"
        rec["name"] = pick(row, spec, "name", cols)
        rec["irwin_id"] = norm_irwin(pick(row, spec, "irwin_id", cols))
        rec["unique_fire_id"] = norm_ufi(pick(row, spec, "unique_fire_id", cols))
        ln = pick(row, spec, "local_number", cols)
        rec["local_number"] = str(ln).strip() if ln is not None else None
        unit = pick(row, spec, "unit", cols)
        rec["unit"] = str(unit).strip() if unit is not None else None
        yr = pick(row, spec, "year", cols)
        try:
            rec["year"] = int(float(yr)) if yr is not None else None
        except (TypeError, ValueError):
            rec["year"] = None
        rec["complex_name"] = pick(row, spec, "complex_name", cols)

        lat, lon = pick(row, spec, "lat", cols), pick(row, spec, "lon", cols)
        if (lat is None or lon is None) and geometry_col in df.columns and spec.geometry == "point":
            g = row[geometry_col]
            if g is not None and not getattr(g, "is_empty", True) and g.geom_type == "Point":
                lat, lon = g.y, g.x
        if valid_latlon(lat, lon):
            rec["lat"], rec["lon"] = float(lat), float(lon)
            rec["coord_decimals"] = coord_decimals(lat, lon)
            rec["ignition_kind"] = spec.ignition_kind

        tz = spec.datetime_tz or "UTC"
        disc, prec = to_utc(pick(row, spec, "discovery", cols), tz, pick(row, spec, "discovery_time", cols))
        if spec.date_only and disc is not None:
            prec = "date"
        rec["discovery_utc"], rec["discovery_precision"] = disc, prec
        rec["containment_utc"] = to_utc(pick(row, spec, "containment", cols), tz,
                                        pick(row, spec, "containment_time", cols))[0]
        rec["control_utc"] = to_utc(pick(row, spec, "control", cols), tz)[0]
        rec["out_utc"] = to_utc(pick(row, spec, "out", cols), tz)[0]
        rec["perimeter_datetime_utc"] = to_utc(pick(row, spec, "perimeter_datetime", cols), tz)[0]

        size = pick(row, spec, "size_acres", cols)
        try:
            rec["size_acres"] = float(size) if size is not None else None
        except (TypeError, ValueError):
            rec["size_acres"] = None
        craw = pick(row, spec, "cause", cols)
        rec["cause_raw"] = None if craw is None else str(craw)
        rec["cause_class"] = classify_cause(craw, spec.cause_codes)
        mm = pick(row, spec, "map_method", cols)
        rec["map_method"] = None if mm is None else str(mm)
        ps = pick(row, spec, "perimeter_source", cols)
        rec["perimeter_source"] = None if ps is None else str(ps)
        if rec["year"] is None and disc is not None:
            rec["year"] = int(disc.tz_convert("America/Los_Angeles").year)
        if geometry_col in df.columns:
            rec["geometry"] = row[geometry_col]
        out.append(rec)
    res = pd.DataFrame(out, columns=CANONICAL_COLUMNS + (["geometry"] if geometry_col in df.columns else []))
    return res
