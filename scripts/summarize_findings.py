"""Headline numbers for FINDINGS.md from a finished `fsa analyze` run.

Usage: python scripts/summarize_findings.py fire-source-audit/out > findings_numbers.md
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

pd.set_option("display.width", 200)


def md(df: pd.DataFrame, floatfmt: str = ".3g") -> str:
    if df is None or not len(df):
        return "_none_\n"
    d = df.copy()
    for c in d.columns:
        if pd.api.types.is_float_dtype(d[c]):
            d[c] = d[c].map(lambda v: "" if pd.isna(v) else format(v, floatfmt))
    head = "| " + " | ".join(map(str, d.columns)) + " |\n|" + "---|" * len(d.columns) + "\n"
    return head + "".join("| " + " | ".join(map(str, r)) + " |\n" for r in d.astype(str).values)


def main(out: Path) -> None:
    s = pd.read_csv(out / "fire_summary.csv", low_memory=False)
    e = pd.read_csv(out / "source_errors.csv", low_memory=False)
    m = pd.read_csv(out / "matched_records.csv", low_memory=False)
    flags = pd.read_csv(out / "flags.csv")
    patch = pd.read_csv(out / "fireset_ignition_patch.csv") if (out / "fireset_ignition_patch.csv").stat().st_size > 1 \
        else pd.DataFrame()
    pairs = pd.read_csv(out / "identical_point_pairs.csv")
    n = len(s)

    print(f"## Run summary\n\nFires: {n}\n")
    cov = m[m.source != "fireset_subset"].groupby("source").agg(
        fires=("fire_key", "nunique"),
        by_id=("match_method", lambda x: int(x.isin(["irwin_id", "unique_fire_id", "incident_number"]).sum())),
        fuzzy=("match_method", lambda x: int((x == "fuzzy").sum())),
        ambiguous=("ambiguous", lambda x: int(x.fillna(False).astype(bool).sum()))).reset_index()
    cov["share"] = (cov.fires / n).round(3)
    print("### Matches per source\n\n" + md(cov.sort_values("fires", ascending=False)))
    lin = m[m.source.isin(["fpa_fod", "inform_fodr"])].groupby(["source", "lineage"]).size().rename("fires").reset_index()
    print("### Per-record lineage of compiled sources\n\n" + md(lin))

    ag = s["ign_agreement"].value_counts().rename_axis("class").rename("fires").reset_index()
    ag["share"] = (ag.fires / n).round(3)
    print("### Ignition agreement across lineages\n\n" + md(ag))
    print("Fires where copied points were merged into one lineage: "
          f"{int(s['ign_lineages_merged'].fillna('').astype(bool).sum())}\n")
    nl = s["ign_n_lineages"].value_counts().sort_index().rename_axis("lineages").rename("fires").reset_index()
    print("### Independent reporting lineages per fire\n\n" + md(nl))

    print("### Sources that copy each other's ignition points\n\n" + md(pairs.head(25)))

    ig = e[e.aspect == "ignition"].copy()
    for c in ("dist_to_others_m", "signed_dist_to_perimeter_m", "dist_to_first_viirs_m", "initial_offset_m",
              "coord_decimals"):
        ig[c] = pd.to_numeric(ig[c], errors="coerce")
    g = ig.groupby("source").agg(
        n=("dist_to_others_m", "count"),
        median_m=("dist_to_others_m", "median"),
        p90_m=("dist_to_others_m", lambda x: x.quantile(.9)),
        gt2km=("dist_to_others_m", lambda x: round(float((x > 2000).mean()), 3)),
        outside_perim_375m=("signed_dist_to_perimeter_m", lambda x: round(float((x.dropna() > 375).mean()), 3)),
        median_to_first_viirs_m=("dist_to_first_viirs_m", "median"),
        coarse_2dec=("coord_decimals", lambda x: round(float((x.dropna() <= 2).mean()), 3))).reset_index()
    print("### Ignition error by source (vs the other lineages)\n\n" + md(g.sort_values("median_m")))
    io = ig[ig.initial_offset_m.notna()].groupby("source").initial_offset_m.agg(
        n="count", median="median", gt500=lambda x: int((x > 500).sum()), gt5km=lambda x: int((x > 5000).sum()))
    print("### IRWIN first-report point vs current point of origin\n\n" + md(io.reset_index()))

    ti = e[e.aspect == "timing"].copy()
    ti["off"] = pd.to_numeric(ti.get("discovery_offset_h"), errors="coerce")
    ti["day"] = pd.to_numeric(ti.get("discovery_day_offset"), errors="coerce")
    for c in ("tz_suspect", "tz_suspect_other_side"):
        ti[c] = ti[c].fillna(False).astype(bool) if c in ti else False
    tg = ti.groupby(["source", "lineage"]).agg(
        n_datetime=("off", "count"), median_h=("off", "median"),
        within_15min=("off", lambda x: round(float((x.dropna().abs() <= .25).mean()), 3) if x.notna().any() else None),
        at_plus2h=("off", lambda x: int(((x - 2).abs() <= .1).sum())),
        at_minus5h=("off", lambda x: int(((x + 5).abs() <= .1).sum())),
        at_7or8h=("off", lambda x: int(((x.abs() - 7).abs() <= .1).sum() + ((x.abs() - 8).abs() <= .1).sum())),
        tz_error_attributed=("tz_suspect", "sum"),
        cleared_by_satellite=("tz_suspect_other_side", "sum"),
        n_date=("day", "count"),
        same_day=("day", lambda x: round(float((x.dropna() == 0).mean()), 3) if x.notna().any() else None)).reset_index()
    print("### Discovery time offset by source and lineage (vs the other lineages)\n\n" + md(tg))
    tz = ti[ti.tz_suspect].assign(year=lambda d: d.fire_key.str[:4]).groupby(["source", "year"]).size().unstack(fill_value=0)
    print("### Clock / time-zone errors attributed by the satellite referee, by fire year\n\n" + md(tz.reset_index()))

    sat = pd.DataFrame({k: pd.to_numeric(s.get(k), errors="coerce") for k in
                        ("viirs_minus_discovery_h", "goes_minus_discovery_h", "hms_minus_discovery_h")})
    print("### Satellite first detection minus reported discovery (h)\n\n"
          + md(sat.describe(percentiles=[.1, .25, .5, .75, .9]).T.reset_index().rename(columns={"index": "bound"})))

    pe = e[e.aspect == "perimeter"].copy()
    for c in ("iou_vs_others", "area_ratio_vs_others"):
        pe[c] = pd.to_numeric(pe[c], errors="coerce")
    pg = pe.groupby("source").agg(n=("iou_vs_others", "count"), iou_median=("iou_vs_others", "median"),
                                  iou_p10=("iou_vs_others", lambda x: x.quantile(.1)),
                                  area_ratio_median=("area_ratio_vs_others", "median"),
                                  iou_lt_07=("iou_vs_others", lambda x: int((x < .7).sum()))).reset_index()
    print("### Perimeter agreement by source\n\n" + md(pg.sort_values("iou_median", ascending=False)))

    sz = e[e.aspect == "size"].copy()
    sz["r"] = pd.to_numeric(sz.rel_error_vs_reference, errors="coerce")
    sg = sz.groupby("source").r.agg(n="count", median="median", median_abs=lambda x: x.abs().median(),
                                    gt25pct=lambda x: int((x.abs() > .25).sum())).reset_index()
    print("### Reported size vs consensus perimeter area\n\n" + md(sg))

    cz = s[["cause_consensus", "cause_agreement"]].copy()
    hn = flags[(flags.category == "cause")].fire_key.nunique()
    print(f"### Cause\n\nFires with a cause from 2+ sources: {int(s['cause_by_source'].fillna('').str.count('=').ge(2).sum())}; "
          f"human-vs-natural conflicts: {hn}\n")
    print(md(cz.cause_consensus.value_counts().rename_axis("consensus").rename("fires").reset_index()))

    fl = flags.groupby(["category", "severity"]).fire_key.nunique().rename("fires").reset_index()
    print("### Flags (fires per category and severity)\n\n" + md(fl))

    d = pd.to_numeric(s.get("fireset_ign_delta_m"), errors="coerce").dropna()
    bins = [0, 100, 500, 1000, 2000, 5000, 1e9]
    labels = ["<100 m", "100-500 m", "0.5-1 km", "1-2 km", "2-5 km", ">5 km"]
    print("### FireSET ignition vs recommended point\n\n"
          + md(pd.cut(d, bins, labels=labels, right=False).value_counts().reindex(labels).rename("fires")
               .rename_axis("distance").reset_index()))
    if len(patch):
        pc = patch.groupby(["new_source", "confidence"]).size().rename("fires").reset_index()
        print(f"Proposed FireSET ignition patches: {len(patch)}\n\n" + md(pc))
        cols = ["fire_key", "move_m", "new_source", "confidence", "Ignition_Lat_old", "Ignition_Lon_old",
                "Ignition_Lat_new", "Ignition_Lon_new"]
        print("Largest moves:\n\n" + md(patch.sort_values("move_m", ascending=False)[cols].head(15)))


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "fire-source-audit/out"))
