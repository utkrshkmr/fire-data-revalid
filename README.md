# fire-data-revalidate

Corroborates the FireSET California fire list (586 VIIRS-qualified fires, 2018–2025) against every federal and
California agency source that reports the same fires: ignition point, final perimeter, discovery and containment
time, size and cause.

- `fire-source-audit/` — the `fsa` tool (probe → fetch → analyze). See its [README](fire-source-audit/README.md),
  [source catalog](fire-source-audit/docs/SOURCES.md) and [methods](fire-source-audit/docs/METHODS.md).
- [`FINDINGS.md`](FINDINGS.md) — what the 2026-10-13 run found about the sources and about FireSET.
- `scripts/download_local_sources.sh` — fetches the file-based sources (FPA-FOD 7th ed., MTBS, USGS combined
  polygons, FIRMS VIIRS archives) into `downloads/`.

## Reproduce

```bash
scripts/download_local_sources.sh              # ~2.5 GB into downloads/
cd fire-source-audit
python -m venv .venv && .venv/bin/pip install -e ".[dev,goes]"
cp /path/to/SUBSET_expanded_20260830.xlsx data/
.venv/bin/pytest -q                            # synthetic fires with known answers
.venv/bin/fsa probe                            # every endpoint and field mapping
.venv/bin/fsa fetch --workers 6                # network sources into cache/ (resumable)
.venv/bin/fsa --only viirs_firms_csv,hms_points fetch
.venv/bin/fsa --only goes_fdc fetch            # optional, ~100 GOES files per fire from AWS
.venv/bin/fsa analyze                          # writes out/
```

The FireSET workbook, the methodology documents, the downloads and the run outputs are not in this public
repository.
