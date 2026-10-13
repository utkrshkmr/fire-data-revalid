"""Write tables, GIS layers, and a static HTML summary."""
from __future__ import annotations

import base64
import html
import io
import json
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd

from .analysis import FireResult
from .config import SourceSpec

# reference categorical slot 1 and neutral inks (light chart surface)
C_SERIES = "#2a78d6"
C_SURFACE = "#fcfcfb"
C_TEXT = "#0b0b0b"
C_TEXT2 = "#52514e"
C_GRID = "#e4e3df"


def _tz_naive(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for c in df.columns:
        if isinstance(df[c].dtype, pd.DatetimeTZDtype):
            df[c] = df[c].dt.tz_convert("UTC").dt.tz_localize(None)
        elif df[c].dtype == object:
            df[c] = df[c].map(lambda v: v.tz_convert("UTC").tz_localize(None) if isinstance(v, pd.Timestamp)
                              and v.tzinfo is not None else v)
    return df


def tables(results: list[FireResult]) -> dict[str, pd.DataFrame]:
    summary = pd.DataFrame([r.summary for r in results])
    errors = pd.DataFrame([e for r in results for e in r.errors])
    pairs = pd.DataFrame([p for r in results for p in r.pairs])
    flags = pd.DataFrame([f for r in results for f in r.flags],
                         columns=["fire_key", "category", "severity", "message"])
    patch = pd.DataFrame([r.patch for r in results if r.patch])
    points = pd.DataFrame([p for r in results for p in r.points])
    return {"fire_summary": summary, "source_errors": errors, "perimeter_pairs": pairs,
            "flags": flags, "fireset_ignition_patch": patch, "points": points}


def _q(s: pd.Series, q: float) -> float | None:
    s = pd.to_numeric(s, errors="coerce").dropna()
    return None if s.empty else round(float(s.quantile(q)), 3)


def source_summary(errors: pd.DataFrame, matched: pd.DataFrame, n_fires: int,
                   specs: dict[str, SourceSpec], settings_raw: dict[str, Any]) -> pd.DataFrame:
    ign = settings_raw.get("ignition", {})
    agree, minor, tol = ign.get("agree_m", 500), ign.get("minor_m", 2000), ign.get("perimeter_tolerance_m", 375)
    rows = []
    sources = sorted(set(matched["source"]) if len(matched) else set())
    for sid in sources:
        sp = specs.get(sid)
        m = matched[matched["source"] == sid]
        row: dict[str, Any] = {"source": sid, "name": sp.name if sp else sid, "agency": sp.agency if sp else "",
                               "lineage": sp.lineage if sp else "", "trust": sp.trust if sp else "",
                               "fires_matched": int(m["fire_key"].nunique()),
                               "match_rate": round(m["fire_key"].nunique() / max(1, n_fires), 3)}
        if "match_method" in m:
            for meth, cnt in m["match_method"].value_counts().items():
                row[f"match_by_{meth}"] = int(cnt)
        e = errors[errors["source"] == sid] if len(errors) else errors
        if len(e):
            ig = e[e["aspect"] == "ignition"]
            if len(ig):
                d = pd.to_numeric(ig["dist_to_others_m"], errors="coerce")
                row.update({"ign_n": int(d.notna().sum()), "ign_median_m": _q(d, .5), "ign_p90_m": _q(d, .9),
                            f"ign_share_gt_{int(agree)}m": round(float((d > agree).mean()), 3) if d.notna().any() else None,
                            f"ign_share_gt_{int(minor)}m": round(float((d > minor).mean()), 3) if d.notna().any() else None,
                            "ign_median_nearest_other_m": _q(ig["dist_to_nearest_other_m"], .5),
                            "ign_median_dx_m": _q(ig["dx_m"], .5), "ign_median_dy_m": _q(ig["dy_m"], .5)})
                sp_d = pd.to_numeric(ig["signed_dist_to_perimeter_m"], errors="coerce")
                if sp_d.notna().any():
                    row["ign_share_outside_perimeter"] = round(float((sp_d[sp_d.notna()] > tol).mean()), 3)
                vd = pd.to_numeric(ig["dist_to_first_viirs_m"], errors="coerce")
                row["ign_median_dist_first_viirs_m"] = _q(vd, .5)
                dec = pd.to_numeric(ig["coord_decimals"], errors="coerce")
                if dec.notna().any():
                    row["ign_share_coarse_coords"] = round(float((dec[dec.notna()] <= 2).mean()), 3)
            pe = e[e["aspect"] == "perimeter"]
            if len(pe):
                v = pd.to_numeric(pe["iou_vs_others"], errors="coerce")
                row.update({"per_n": int(v.notna().sum()), "per_iou_median": _q(v, .5), "per_iou_p10": _q(v, .1),
                            "per_area_ratio_median": _q(pe["area_ratio_vs_others"], .5),
                            "per_hausdorff_median_m": _q(pe["hausdorff_m_vs_others"], .5)})
            ti = e[e["aspect"] == "timing"]
            if len(ti):
                if "discovery_offset_h" in ti:
                    off = pd.to_numeric(ti["discovery_offset_h"], errors="coerce")
                    row.update({"disc_n_datetime": int(off.notna().sum()), "disc_median_offset_h": _q(off, .5),
                                "disc_median_abs_offset_h": _q(off.abs(), .5)})
                if "tz_suspect" in ti:
                    row["disc_share_tz_suspect"] = round(float(ti["tz_suspect"].fillna(False).astype(bool).mean()), 3)
                if "discovery_day_offset" in ti:
                    dd = pd.to_numeric(ti["discovery_day_offset"], errors="coerce").dropna()
                    if len(dd):
                        row["disc_share_same_day"] = round(float((dd == 0).mean()), 3)
            sz = e[e["aspect"] == "size"]
            if len(sz):
                re_ = pd.to_numeric(sz["rel_error_vs_reference"], errors="coerce")
                row.update({"size_median_rel_error": _q(re_, .5), "size_median_abs_rel_error": _q(re_.abs(), .5)})
        rows.append(row)
    return pd.DataFrame(rows)


def identical_pairs(summary: pd.DataFrame, specs: dict[str, SourceSpec]) -> pd.DataFrame:
    """How often two sources report the same ignition coordinates (< 1 m) for a fire."""
    if "ign_identical_pairs" not in summary:
        return pd.DataFrame(columns=["source_a", "source_b", "fires_identical", "same_lineage_in_config"])
    counts: dict[str, int] = {}
    for v in summary["ign_identical_pairs"].dropna():
        for pair in str(v).split(";"):
            if pair:
                counts[pair] = counts.get(pair, 0) + 1
    rows = []
    for pair, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        a, b = pair.split("=")
        la = specs[a].lineage if a in specs else a
        lb = specs[b].lineage if b in specs else b
        rows.append({"source_a": a, "source_b": b, "fires_identical": n, "same_lineage_in_config": la == lb})
    return pd.DataFrame(rows, columns=["source_a", "source_b", "fires_identical", "same_lineage_in_config"])


# ---------------------------------------------------------------- charts


def _fig_png(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=144, bbox_inches="tight", facecolor=C_SURFACE)
    return base64.b64encode(buf.getvalue()).decode()


def _style(ax):
    ax.set_facecolor(C_SURFACE)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(C_GRID)
    ax.tick_params(colors=C_TEXT2, labelsize=9)
    ax.grid(axis="x", color=C_GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def _hbar(labels, values, title, xlabel, fmt="{:,.0f}", log=False):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(7, 0.42 * len(labels) + 1.1))
    fig.patch.set_facecolor(C_SURFACE)
    y = np.arange(len(labels))
    ax.barh(y, values, color=C_SERIES, height=0.6)
    ax.set_yticks(y, labels)
    ax.invert_yaxis()
    if log:
        ax.set_xscale("log")
    elif fmt == "{:,.0f}":
        from matplotlib.ticker import MaxNLocator
        ax.xaxis.set_major_locator(MaxNLocator(integer=True))
    _style(ax)
    ax.set_title(title, loc="left", color=C_TEXT, fontsize=11)
    ax.set_xlabel(xlabel, color=C_TEXT2, fontsize=9)
    for yi, v in zip(y, values):
        ax.text(v, yi, " " + fmt.format(v), va="center", color=C_TEXT2, fontsize=8.5)
    out = _fig_png(fig)
    plt.close(fig)
    return out


def charts(t: dict[str, pd.DataFrame], ss: pd.DataFrame) -> list[tuple[str, str]]:
    out = []
    s = t["fire_summary"]
    if "ign_agreement" in s:
        order = ["agree", "minor", "major", "single_lineage", "no_reported_point"]
        vc = s["ign_agreement"].value_counts().reindex(order).dropna()
        if len(vc):
            out.append(("Ignition agreement across reporting lineages",
                        _hbar(list(vc.index), vc.values, "Fires by ignition agreement", "fires")))
    if len(ss) and "ign_median_m" in ss:
        d = ss.dropna(subset=["ign_median_m"]).sort_values("ign_median_m")
        d = d[d["ign_median_m"] > 0]
        if len(d):
            out.append(("Median distance from each source's ignition point to the other lineages' consensus",
                        _hbar(list(d["source"]), d["ign_median_m"].values, "Ignition error by source (median)",
                              "metres (log scale)", log=True)))
    if len(ss) and "per_iou_median" in ss:
        d = ss.dropna(subset=["per_iou_median"]).sort_values("per_iou_median", ascending=False)
        if len(d):
            out.append(("Median perimeter IoU against the other lineages",
                        _hbar(list(d["source"]), d["per_iou_median"].values, "Perimeter agreement by source (median IoU)",
                              "IoU", fmt="{:.2f}")))
    if "fireset_ign_delta_m" in s:
        dv = pd.to_numeric(s["fireset_ign_delta_m"], errors="coerce").dropna()
        if len(dv):
            bins = [0, 100, 250, 500, 1000, 2000, 5000, 10000, np.inf]
            labels = ["<100 m", "100-250 m", "250-500 m", "0.5-1 km", "1-2 km", "2-5 km", "5-10 km", ">10 km"]
            cnt = pd.cut(dv, bins=bins, labels=labels, right=False).value_counts().reindex(labels)
            out.append(("How far the current FireSET ignition is from the recommended point",
                        _hbar(labels, cnt.values, "FireSET ignition vs recommended point", "fires")))
    return out


# ---------------------------------------------------------------- writers


def write_all(out_dir: Path, results: list[FireResult], matched: gpd.GeoDataFrame,
              specs: dict[str, SourceSpec], settings_raw: dict[str, Any], n_fires: int) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    t = tables(results)
    ss = source_summary(t["source_errors"], matched, n_fires, specs, settings_raw)
    t["source_summary"] = ss
    t["identical_point_pairs"] = identical_pairs(t["fire_summary"], specs)
    paths: dict[str, Path] = {}
    for name, df in t.items():
        p = out_dir / f"{name}.csv"
        _tz_naive(df).to_csv(p, index=False)
        paths[name] = p
    # workbook for people who prefer one file
    try:
        with pd.ExcelWriter(out_dir / "fire_source_audit.xlsx") as xw:
            for name in ("source_summary", "fire_summary", "flags", "fireset_ignition_patch", "source_errors",
                         "perimeter_pairs"):
                _tz_naive(t[name]).to_excel(xw, sheet_name=name[:31], index=False)
        paths["xlsx"] = out_dir / "fire_source_audit.xlsx"
    except Exception:  # noqa: BLE001 - csv outputs are the record
        pass
    # GIS layers
    pts = t["points"]
    if len(pts):
        g = gpd.GeoDataFrame(pts, geometry=gpd.points_from_xy(pts["lon"], pts["lat"]), crs="EPSG:4326")
        (out_dir / "ignition_points.geojson").write_text(g.to_json(default=str))
        paths["points"] = out_dir / "ignition_points.geojson"
    polys = [(r.summary["fire_key"], r.consensus_perimeter) for r in results if r.consensus_perimeter is not None]
    if polys:
        g = gpd.GeoDataFrame({"fire_key": [k for k, _ in polys]}, geometry=[p for _, p in polys], crs="EPSG:4326")
        (out_dir / "consensus_perimeters.geojson").write_text(g.to_json())
    if len(matched) and matched.geometry.notna().any():
        mp = matched[matched.geometry.notna() & matched.geom_type.isin(["Polygon", "MultiPolygon"])]
        if len(mp):
            keep = [c for c in ("fire_key", "source", "lineage", "name", "irwin_id", "size_acres", "match_method",
                                "match_score") if c in mp.columns]
            (out_dir / "source_perimeters.geojson").write_text(mp[keep + ["geometry"]].to_json(default=str))
    paths["html"] = write_html(out_dir / "report.html", t, ss, n_fires)
    return paths


def _table_html(df: pd.DataFrame, max_rows: int = 60) -> str:
    if df is None or not len(df):
        return "<p class='muted'>No rows.</p>"
    d = _tz_naive(df.head(max_rows))
    return d.to_html(index=False, na_rep="", border=0, classes="t", float_format=lambda v: f"{v:,.3g}")


def write_html(path: Path, t: dict[str, pd.DataFrame], ss: pd.DataFrame, n_fires: int) -> Path:
    s = t["fire_summary"]
    flags = t["flags"]
    patch = t["fireset_ignition_patch"]
    n_matched = int((pd.to_numeric(s.get("n_sources_matched", pd.Series(dtype=float)), errors="coerce") > 0).sum())
    agree = s["ign_agreement"].value_counts().to_dict() if "ign_agreement" in s else {}
    high = flags[flags["severity"] == "high"]["fire_key"].nunique() if len(flags) else 0
    tiles = [("Fires audited", f"{n_fires}"), ("Matched in at least one source", f"{n_matched}"),
             ("Ignition: lineages agree", f"{agree.get('agree', 0)}"),
             ("Ignition: major disagreement", f"{agree.get('major', 0)}"),
             ("Proposed FireSET ignition fixes", f"{len(patch)}"),
             ("Fires with a high-severity flag", f"{high}")]
    chart_html = "".join(f"<figure><img alt='{html.escape(c)}' src='data:image/png;base64,{b}'>"
                         f"<figcaption>{html.escape(c)}</figcaption></figure>" for c, b in charts(t, ss))
    flag_counts = (flags.groupby(["category", "severity"]).size().rename("fires").reset_index()
                   if len(flags) else pd.DataFrame())
    worst = s.sort_values("ign_max_pairwise_m", ascending=False) if "ign_max_pairwise_m" in s else s
    worst_cols = [c for c in ("fire_key", "ign_agreement", "ign_n_lineages", "ign_max_pairwise_m", "ign_lineages",
                              "ign_recommended_source", "ign_confidence", "fireset_ign_delta_m",
                              "perimeter_iou_median", "discovery_spread_h") if c in worst]
    doc = f"""<!doctype html><html><head><meta charset="utf-8"><title>Fire source audit</title>
<style>
body{{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;background:{C_SURFACE};color:{C_TEXT};margin:0;padding:24px 16px}}
main{{max-width:1100px;margin:0 auto}} h1{{margin:0 0 4px}} h2{{margin:32px 0 8px}}
.muted{{color:{C_TEXT2}}} .tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin:16px 0}}
.tile{{border:1px solid {C_GRID};border-radius:8px;padding:10px 12px}} .tile b{{display:block;font-size:26px}}
.tile span{{color:{C_TEXT2};font-size:13px}} figure{{margin:12px 0}} img{{max-width:100%}}
figcaption{{color:{C_TEXT2};font-size:13px}} .wrap{{overflow-x:auto}}
table.t{{border-collapse:collapse;font-size:12.5px;font-variant-numeric:tabular-nums}}
table.t th,table.t td{{padding:4px 8px;border-bottom:1px solid {C_GRID};text-align:left;white-space:nowrap}}
table.t th{{color:{C_TEXT2};font-weight:600}}
</style></head><body><main>
<h1>Fire source audit</h1>
<p class="muted">Cross-source comparison of ignition points, perimeters, timing, size and cause for the FireSET fire list.
Consensus counts each upstream lineage once; per-source errors compare each source with the other lineages only.</p>
<div class="tiles">{''.join(f"<div class='tile'><b>{v}</b><span>{html.escape(k)}</span></div>" for k, v in tiles)}</div>
{chart_html}
<h2>Per-source error summary</h2><div class="wrap">{_table_html(ss, 100)}</div>
<h2>Flags by category</h2><div class="wrap">{_table_html(flag_counts, 100)}</div>
<h2>Largest ignition disagreements</h2><div class="wrap">{_table_html(worst[worst_cols], 40)}</div>
<h2>Sources that report identical ignition coordinates</h2>
<p class="muted">Pairs that copy each other often but sit in different lineages in sources.yaml should be merged into one lineage,
or the consensus counts the same upstream record twice.</p>
<div class="wrap">{_table_html(t.get("identical_point_pairs"), 40)}</div>
<h2>Proposed FireSET ignition corrections</h2><div class="wrap">{_table_html(patch, 80)}</div>
<p class="muted">Full tables: fire_summary.csv, source_errors.csv, perimeter_pairs.csv, flags.csv,
fireset_ignition_patch.csv, source_summary.csv, fire_source_audit.xlsx. GIS layers: ignition_points.geojson,
consensus_perimeters.geojson, source_perimeters.geojson.</p>
</main></body></html>"""
    path.write_text(doc)
    return path
