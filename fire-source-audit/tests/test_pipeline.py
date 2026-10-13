"""End-to-end on synthetic fixtures: match -> analyze -> report, with known answers."""
import pandas as pd
import pytest

from fsa.config import load_settings, load_sources
from fsa.fires import load_fires
from fsa.pipeline import Pipeline
from fsa.report import write_all

import synth


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("fsa")
    xlsx = tmp / "fires.xlsx"
    synth.write_fires_xlsx(xlsx)
    settings = load_settings(root=tmp)
    settings.raw["paths"]["cache_dir"] = str(tmp / "cache")
    settings.raw["paths"]["out_dir"] = str(tmp / "out")
    synth.write_cache(tmp / "cache")
    specs, defaults = load_sources()
    fires = load_fires(xlsx, "Initial 30")
    pipe = Pipeline(settings, specs, defaults)
    matched = pipe.match(fires)
    results = pipe.analyze(fires, matched)
    paths = write_all(settings.out_dir, results, matched, specs, settings.raw, len(fires))
    summary = pd.DataFrame([r.summary for r in results]).set_index("fire_key")
    flags = pd.DataFrame([f for r in results for f in r.flags])
    patches = {r.patch["fire_key"]: r.patch for r in results if r.patch}
    errors = pd.DataFrame([e for r in results for e in r.errors])
    return dict(fires=fires, matched=matched, summary=summary, flags=flags, patches=patches,
                errors=errors, paths=paths)


def test_fire_list_loaded(run):
    assert [f.key for f in run["fires"]] == ["2018_ALPHA", "2019_BRAVO", "2020_CHARLIE"]
    a = run["fires"][0]
    assert a.start_utc == pd.Timestamp("2018-11-08 14:29", tz="UTC")
    assert run["fires"][2].start_precision == "date"


def test_decoy_not_matched(run):
    m = run["matched"]
    names = set(m.loc[m["fire_key"] == "2018_ALPHA", "name"].astype(str))
    assert "Pulga" not in names
    assert (m.loc[(m["fire_key"] == "2018_ALPHA") & (m["source"] == "wfigs_locations"), "match_method"] == "irwin_id").all()
    # CAL FIRE incident has no IDs: matched by name/date/place
    cf = m[(m["fire_key"] == "2018_ALPHA") & (m["source"] == "calfire_incidents")]
    assert len(cf) == 1 and cf["match_method"].iloc[0] == "fuzzy"


def test_alpha_ignition_agrees_and_fireset_patched(run):
    s = run["summary"].loc["2018_ALPHA"]
    assert s["ign_agreement"] in ("agree", "minor")
    assert s["ign_n_lineages"] == 3
    assert s["ign_max_pairwise_m"] < 600
    # FireSET point was placed 3 km east
    assert 2500 < s["fireset_ign_delta_m"] < 3500
    p = run["patches"]["2018_ALPHA"]
    assert p["new_source"] in ("wfigs_locations", "calfire_incidents", "fpa_fod")


def test_bravo_outlier_detected(run):
    s = run["summary"].loc["2019_BRAVO"]
    assert s["ign_agreement"] == "major"
    assert s["ign_recommended_source"] in ("wfigs_locations", "calfire_incidents")
    e = run["errors"]
    usfs = e[(e["fire_key"] == "2019_BRAVO") & (e["source"] == "usfs_edw_points") & (e["aspect"] == "ignition")]
    assert usfs["dist_to_others_m"].iloc[0] > 5000
    fl = run["flags"]
    assert ((fl["fire_key"] == "2019_BRAVO") & (fl["category"] == "ignition") & (fl["severity"] == "high")).any()


def test_timezone_bug_flagged(run):
    e = run["errors"]
    t = e[(e["fire_key"] == "2019_BRAVO") & (e["source"] == "usfs_edw_points") & (e["aspect"] == "timing")]
    assert abs(abs(t["discovery_offset_h"].iloc[0]) - 7) < 0.3  # PDT is UTC-7 in September
    assert bool(t["tz_suspect"].iloc[0])


def test_perimeters(run):
    s = run["summary"].loc["2018_ALPHA"]
    assert s["n_perimeter_lineages"] == 3
    assert s["perimeter_iou_median"] > 0.85
    assert s["consensus_acres"] == pytest.approx(2400, rel=0.25)


def test_cause_conflict_on_bravo(run):
    fl = run["flags"]
    assert ((fl["fire_key"] == "2019_BRAVO") & (fl["category"] == "cause")).any()


def test_unmatched_fire_flagged(run):
    fl = run["flags"]
    assert ((fl["fire_key"] == "2020_CHARLIE") & (fl["category"] == "match")).any()


def test_outputs_written(run):
    out = run["paths"]["html"].parent
    for name in ("fire_summary.csv", "source_errors.csv", "flags.csv", "fireset_ignition_patch.csv",
                 "source_summary.csv", "report.html", "ignition_points.geojson", "consensus_perimeters.geojson",
                 "fire_source_audit.xlsx"):
        assert (out / name).exists(), name
    ss = pd.read_csv(out / "source_summary.csv")
    assert "wfigs_locations" in set(ss["source"])
