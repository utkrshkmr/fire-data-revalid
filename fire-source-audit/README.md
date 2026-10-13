# fire-source-audit

Cross-checks every fire in the FireSET list against all the U.S. federal and California agency sources that
report it. For each fire it measures how far the sources disagree on:

- ignition point
- final perimeter
- discovery and containment time
- size
- cause

It then builds a consensus, grades each source against the others, and proposes corrected ignition points for
FireSET.

- `docs/SOURCES.md`: catalog of the sources, their endpoints, keys, coverage and caveats.
- `docs/METHODS.md`: how matching, consensus and error metrics work.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .              # core
pip install -e ".[goes]"      # optional GOES-R scan
pip install -e ".[dev]" && pytest   # 24 tests: synthetic fires with known answers + live-data regressions
```

Put the fire table at `data/SUBSET_expanded_20260830.xlsx`, or pass `--fires`.

### Local downloads (optional, skipped if absent)

| Source | Where to get it | settings.yaml key |
|---|---|---|
| FPA-FOD 7th ed. (1992–2024) | USFS Research Data Archive, RDS-2013-0009.7 (GPKG) | `paths.local.fpa_fod` |
| MTBS perimeters and points | USGS burn-severity portal, MTBS national downloads | `mtbs_perimeters`, `mtbs_points` |
| USGS combined fire polygons | ScienceBase, doi:10.5066/P9ZXGFY3 | `usgs_combined` |
| FireSET VIIRS detections | your `detection_{year}_filtered_350.h5` files | `viirs_h5_pattern` |

## Run

```bash
fsa probe                               # check every endpoint and field mapping first
fsa fetch --workers 6                   # pull everything into cache/ (resumable)
fsa analyze                             # match, compare, write out/
fsa run                                 # fetch + analyze

fsa --year 2020 --limit 20 run          # a quick subset
fsa --fire CAMP --fire DELTA run        # specific fires
fsa --only wfigs_locations,frap_perimeters fetch
```

Fetching is polite: it pauses 0.3 s between requests, retries with backoff, and caches every pull. A full pull
of 586 fires × 10 network sources is roughly 6,000 requests, about two hours with one source at a time. Sources
run in parallel (`--workers`), so expect 15–30 minutes. Reruns only fetch what is missing; add `--force` to
refresh.

Run it where the agency hosts are reachable (arcgis.com, apps.fs.usda.gov, fire.ca.gov, and AWS S3 for GOES).
`fsa probe` tells you in a minute whether they are. Cluster compute nodes often have no outbound internet, so
fetch on a login or transfer node, then run `fsa analyze` anywhere.

## Outputs (`out/`)

| File | What it holds |
|---|---|
| `report.html` | summary tiles, charts, per-source error table, worst disagreements, proposed fixes |
| `fire_summary.csv` | one row per fire: ignition consensus, agreement class, recommended point and confidence, perimeter IoU, discovery spread, size spread, cause agreement, flags |
| `source_errors.csv` | one row per fire × source × aspect, with the metrics in METHODS §5 |
| `source_summary.csv` | per-source error statistics: which source is most reliable for what |
| `perimeter_pairs.csv` | IoU, area ratio and Hausdorff distance for every pair of perimeters |
| `flags.csv` | every issue found, with category and severity |
| `fireset_ignition_patch.csv` | proposed new `Ignition_Lat`/`Ignition_Lon` for FireSET, with an `action` (replace / review) and the evidence |
| `timing_by_lineage.csv` | discovery-time offsets per source and record lineage (clock and time-zone errors) |
| `identical_point_pairs.csv` | sources that copy each other's coordinates, by record lineage |
| `matched_records.csv` | the crosswalk: which record in each source matched each fire, and how |
| `ignition_points.geojson`, `consensus_perimeters.geojson`, `source_perimeters.geojson` | layers for QGIS review |
| `fire_source_audit.xlsx` | the main tables in one workbook |

## Adding a source

Add an entry to `config/sources.yaml` with:

- `kind`: `arcgis`, `local_vector` or `calfire_json`
- endpoint or path, and `roles`
- a `lineage` (reuse an existing one if it copies that upstream)
- a `fields` map from the canonical names to the source's columns

Run `fsa probe --only <id>` to check it.
