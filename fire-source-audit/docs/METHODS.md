# Methods

## 1. Fetch

For every fire in the FireSET table, each spatial source is queried with the fire's buffered bounding box
(+0.05°) and a year or date filter (`where` in `sources.yaml`). If a service rejects the filter, the query falls
back to spatial-only and filters by year on the client. Bulk sources are loaded once and filtered locally: the
CAL FIRE incident list and the local downloads (FPA-FOD, MTBS, USGS). Raw pulls are cached per source and fire
under `cache/raw/`, so analysis reruns without the network. Failures go to `cache/errors/` and `out/fetch_status.csv`.

## 2. Normalize

Each raw row is mapped to one schema: name, IDs, point, discovery / containment / control / out times, size,
cause, perimeter metadata. Rules:

- **Times:**
  - ArcGIS epoch milliseconds are read as UTC.
  - Naive strings use the source's `datetime_tz`: Pacific for CAL FIRE and FPA-FOD, UTC otherwise.
  - FPA-FOD `DISCOVERY_TIME` (HHMM) is joined to the date.
  - Date-only values are marked `discovery_precision = date` and compared at day level only.
- **Cause:** classed as Human, Natural or Undetermined. FRAP's numeric codes are decoded (1 and 17 are natural, 14 is unknown).
- **Coordinate precision:** the number of decimal places is kept. Two decimals is about 1 km, which flags section-centroid or rounded reports.

## 3. Match

Each candidate record is scored against the FireSET fire:

| Evidence | Score |
|---|---|
| Same IRWIN ID | 1.00 |
| Same UniqueFireIdentifier | 0.97 |
| Same incident number, same year, location consistent | 0.90 |
| Otherwise: 0.45·name + 0.25·date + 0.20·place + 0.10·size | at most 0.89 |

The fuzzy score is halved when the names differ, unless place, date and size all agree strongly. A record is
accepted at ≥ 0.60. It is flagged *ambiguous* when a different record scores within 0.05 of it. Within one
source, the single best record is kept (its lineage may be set per record). Multi-part perimeters of the same fire are dissolved. For
operational feeds with several dated polygons, the latest polygon is kept. An ID match whose point lies more
than 20 km from the fire is flagged as an identifier conflict.

## 4. Lineages and consensus

Sources copied from one upstream record would double-count it, so each source has a lineage. Consensus is built
across lineages, never across raw sources.

- **Ignition consensus:**
  - Take the medoid of each lineage's points.
  - Take the trust-weighted geometric median (Weiszfeld) across lineages.
  - Robustness: one wild point cannot drag the median far once three lineages report.
- **Agreement class** from the largest distance between any two lineages:
  - `agree` ≤ 500 m
  - `minor` ≤ 2 km
  - `major` > 2 km
  - `single_lineage` or `no_reported_point` when there are too few lineages to compare
- **Perimeter consensus:**
  - One perimeter per lineage, rasterised at 30 m.
  - A cell is burned when at least half the lineages include it.
  - With two lineages this is their union.
- **Leave-one-out:** every per-source error compares the source with the consensus of the *other* lineages, so a source is never graded against itself.
- **Lineage check:** pairs of sources that report identical coordinates (< 1 m) are counted in `identical_point_pairs.csv`. A pair that copies often but sits in different lineages should be merged into one lineage in `sources.yaml`.

## 5. Per-source errors (`source_errors.csv`)

| Aspect | Metrics |
|---|---|
| Ignition | distance to the other lineages' consensus (m); east/north offset (dx, dy) for systematic bias; signed distance to the consensus perimeter (negative = inside); distance to the first VIIRS overpass centre; coordinate decimals |
| Perimeter | IoU, area ratio, boundary Hausdorff distance and centroid offset against the other lineages' consensus; GIS acres vs reported acres |
| Timing | discovery offset from the other lineages' median (h), or day offset for date-only sources; offsets of 7 or 8 h flagged as likely time-zone errors; end-time spread |
| Size | relative error of reported acres against the consensus perimeter area |
| Cause | per-source class; agreement share; Human-vs-Natural conflicts flagged |

`source_summary.csv` aggregates these per source: match rate, median and 90th-percentile ignition error, share
beyond 500 m and 2 km, share outside the perimeter, mean bias, median IoU, median time offset, share of
time-zone suspects, and median size error.

## 6. Recommended ignition and FireSET corrections

Every agency point is scored on four kinds of evidence:

- **Support (0.35):** share of other lineages within 2 km.
- **Perimeter (0.25):** inside, or within 375 m of, the consensus perimeter.
- **Satellite:**
  - first VIIRS overpass within 1.5 km (0.15)
  - first GOES detection within 3 km (0.05)
- **Record quality:**
  - source trust tier (0.10)
  - coordinate precision (0.10)

Weights renormalise when evidence is missing. The best-scoring *reported* point is recommended, so every
correction traces to an agency record. Confidence:

- **High:** all lineages agree.
- **Medium:** one lineage only, or a disagreement with strong support for the winner.
- **Low:** anything else.

If no agency point exists, the centre of the first VIIRS overpass is offered with low confidence.
`fireset_ignition_patch.csv` lists every fire where the recommended point is more than 500 m from FireSET's
current `Ignition_Lat`/`Ignition_Lon` (or FireSET has none), with old and new coordinates, distance moved,
source, confidence and reason.

## 7. Satellite timing

The first VIIRS detections inside the consensus perimeter (+1 km, to exclude neighbouring fires) give the
first-overpass time and centre. `viirs_minus_discovery_h` < −3 h is flagged: the satellite saw heat before the
reported discovery. That means either a holdover fire, a contaminated window, or a late discovery time. The
optional GOES-R scan gives the earliest 5-minute detection within 5 km of the FireSET point, from 6 h before to
3 h after discovery.

## 8. Known limits

- The perimeter IoU threshold (0.7 per source, 0.8 median) and the distance thresholds are settings, not truths. Tune them in `settings.yaml`.
- MTBS excludes unburned islands, so its IoU against agency perimeters is lower by construction. Read its area ratio, not only its IoU.
- Operational FIRIS perimeters can predate the final one. Use their IoU as a lower bound.
- Complex components: when a source only has the complex record, matching by name fails on purpose. The fire is reported as unmatched in that source rather than forced onto the complex.
