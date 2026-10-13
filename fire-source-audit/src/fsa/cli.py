"""Command line: fsa probe | fetch | analyze | run"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd

from .config import load_settings, load_sources
from .fires import load_fires
from .normalize import resolve_column
from .pipeline import Pipeline
from .report import write_all
from .sources.arcgis import ArcGISLayer
from .sources.http import Http

log = logging.getLogger("fsa")


def _load(args):
    settings = load_settings(Path(args.settings) if args.settings else None)
    specs, defaults = load_sources(Path(args.sources) if args.sources else None)
    if args.out:
        settings.raw.setdefault("paths", {})["out_dir"] = args.out
    xlsx = Path(args.fires) if args.fires else settings.path("fires_xlsx")
    fires = load_fires(xlsx, settings.raw.get("paths", {}).get("fires_sheet", 0)) if xlsx else []
    if args.fire:
        wanted = {w.upper() for w in args.fire}
        fires = [f for f in fires if f.key.upper() in wanted or f.name.upper() in wanted]
    if args.year:
        fires = [f for f in fires if f.year in set(args.year)]
    if args.limit:
        fires = fires[: args.limit]
    only = args.only.split(",") if getattr(args, "only", None) else None
    return settings, specs, defaults, fires, only


def cmd_probe(args) -> int:
    settings, specs, defaults, _, only = _load(args)
    http = Http.from_settings(settings)
    rows = []
    for sid, sp in specs.items():
        if only and sid not in only:
            continue
        row = {"source": sid, "kind": sp.kind, "enabled": sp.enabled}
        try:
            if sp.kind == "arcgis":
                meta = ArcGISLayer(sp.url, http).metadata()
                cols = [f["name"] for f in meta.get("fields", [])]
                row["reachable"] = True
                row["maxRecordCount"] = meta.get("maxRecordCount")
            elif sp.kind == "calfire_json":
                data = http.get_json(sp.url)
                first = data[0] if isinstance(data, list) and data else {}
                cols = list(first.keys())
                row["reachable"] = True
                row["n_records"] = len(data) if isinstance(data, list) else None
            elif sp.kind == "local_vector":
                p = settings.local_path(sp.path_key) if sp.path_key else None
                if not p or not p.exists():
                    row.update({"reachable": False, "detail": f"missing file {p}"})
                    rows.append(row)
                    continue
                import pyogrio
                info = pyogrio.read_info(p, layer=sp.layer) if sp.layer else pyogrio.read_info(p)
                cols = list(info["fields"])
                row["reachable"] = True
                row["n_records"] = info.get("features")
            else:
                row["detail"] = "not probed (evidence source)"
                rows.append(row)
                continue
            missing = [k for k, cands in sp.fields.items() if resolve_column(cols, cands) is None]
            row["fields_missing"] = ";".join(missing)
            row["ok"] = not missing
        except Exception as e:  # noqa: BLE001
            row.update({"reachable": False, "detail": repr(e)[:300]})
        rows.append(row)
        log.info("%s: %s", sid, {k: v for k, v in row.items() if k != "source"})
    df = pd.DataFrame(rows)
    settings.out_dir.mkdir(parents=True, exist_ok=True)
    df.to_csv(settings.out_dir / "probe_report.csv", index=False)
    print(df.to_string(index=False))
    return 0


def cmd_fetch(args) -> int:
    settings, specs, defaults, fires, only = _load(args)
    log.info("fetching %d fires", len(fires))
    st = Pipeline(settings, specs, defaults, only).fetch(fires, force=args.force, workers=args.workers)
    print(st.groupby(["source", "status"]).size().unstack(fill_value=0).to_string())
    return 0


def cmd_analyze(args) -> int:
    settings, specs, defaults, fires, only = _load(args)
    pipe = Pipeline(settings, specs, defaults, only)
    matched = pipe.match(fires)
    results = pipe.analyze(fires, matched)
    paths = write_all(settings.out_dir, results, matched, specs, settings.raw, len(fires))
    print("wrote:")
    for k, p in paths.items():
        print(f"  {k}: {p}")
    return 0


def cmd_run(args) -> int:
    cmd_fetch(args)
    return cmd_analyze(args)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fsa", description="Cross-check fire attributes across government sources")
    ap.add_argument("--settings")
    ap.add_argument("--sources")
    ap.add_argument("--fires", help="path to SUBSET_expanded_*.xlsx (overrides settings)")
    ap.add_argument("--out", help="output directory (overrides settings)")
    ap.add_argument("--fire", action="append", help="limit to fire key or name (repeatable)")
    ap.add_argument("--year", action="append", type=int, help="limit to fire year (repeatable)")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--only", help="comma-separated source ids")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("probe", help="check every endpoint and field mapping")
    for name in ("fetch", "run"):
        p = sub.add_parser(name)
        p.add_argument("--force", action="store_true", help="refetch even if cached")
        p.add_argument("--workers", type=int, default=4, help="sources fetched in parallel")
    sub.add_parser("analyze", help="match, compare and report from the cache")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return {"probe": cmd_probe, "fetch": cmd_fetch, "analyze": cmd_analyze, "run": cmd_run}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
