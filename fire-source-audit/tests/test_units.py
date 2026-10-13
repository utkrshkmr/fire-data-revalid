import h5py
import numpy as np
import pandas as pd
import pytest
from shapely.geometry import Point, Polygon, box

from fsa.geo import LocalFrame, geometric_median, haversine_m, iou, vote_consensus
from fsa.normalize import classify_cause, coord_decimals, incident_number, name_similarity, norm_irwin, to_utc
from fsa.sources import ViirsFireSET, parse_hms_text
from fsa.sources.arcgis import esri_to_shapely, features_to_gdf


def test_irwin_and_incident_number():
    assert norm_irwin("{cb032a65-0df9-4443-83f0-c34e04507855}") == "CB032A65-0DF9-4443-83F0-C34E04507855"
    assert norm_irwin("") is None
    assert incident_number("2018-CABTU-016737") == "16737"
    assert incident_number("016737") == "16737"


def test_name_similarity():
    assert name_similarity("Camp Fire", "CAMP") == 1.0
    assert name_similarity("SCU Lightning Complex", "SCU LIGHTNING") == 1.0
    assert name_similarity("Delta", "Carr") < 0.5


def test_to_utc_variants():
    t, p = to_utc(1541687340000)  # epoch ms
    assert t == pd.Timestamp("2018-11-08 14:29:00", tz="UTC") and p == "datetime"
    t, p = to_utc("2018-11-08T06:29:00", "America/Los_Angeles")
    assert t == pd.Timestamp("2018-11-08 14:29:00", tz="UTC")
    t, p = to_utc("2018-11-08", "America/Los_Angeles", "0629")
    assert t == pd.Timestamp("2018-11-08 14:29:00", tz="UTC") and p == "datetime"
    t, p = to_utc("2018-11-08", "America/Los_Angeles")
    assert p == "date"
    t, p = to_utc(2458430.5)  # Julian day, 2018-11-08
    assert t.date() == pd.Timestamp("2018-11-08").date()


def test_cause_classes():
    assert classify_cause(1, "frap") == "Natural"
    assert classify_cause(11, "frap") == "Human"
    assert classify_cause(14, "frap") == "Undetermined"
    assert classify_cause("Power generation/transmission/distribution") == "Human"
    assert classify_cause("Lightning") == "Natural"
    assert classify_cause("Undetermined") == "Undetermined"


def test_coord_decimals():
    assert coord_decimals(39.82, -121.44) == 2
    assert coord_decimals(39.820005, -121.440013) == 6


def test_esri_polygon_with_hole():
    outer = [[0, 0], [0, 10], [10, 10], [10, 0], [0, 0]]       # clockwise -> exterior
    hole = [[2, 2], [4, 2], [4, 4], [2, 4], [2, 2]]             # counter-clockwise -> hole
    g = esri_to_shapely({"rings": [outer, hole]})
    assert g.geom_type == "Polygon" and abs(g.area - 96) < 1e-9
    gdf = features_to_gdf([{"attributes": {"a": 1}, "geometry": {"x": 1, "y": 2}}], "json")
    assert gdf.geometry.iloc[0].equals(Point(1, 2))


def test_geometric_median_resists_outlier():
    pts = np.array([[0, 0], [10, 0], [0, 10], [5000, 5000]], dtype=float)
    m = geometric_median(pts)
    assert np.linalg.norm(m) < 50


def test_vote_consensus_three():
    a, b, c = box(0, 0, 1000, 1000), box(100, 0, 1100, 1000), box(5000, 5000, 6000, 6000)
    cons = vote_consensus([a, b, c], 10, 0.5, 10_000_000)
    assert iou(cons, a.intersection(b)) > 0.95


def test_local_frame_distance_matches_haversine():
    f = LocalFrame(-121.4, 39.8)
    x1, y1 = f.xy(-121.4, 39.8)
    x2, y2 = f.xy(-121.38, 39.81)
    assert abs(np.hypot(x2 - x1, y2 - y1) - haversine_m(39.8, -121.4, 39.81, -121.38)) < 5


def test_parse_hms_text():
    txt = "Lon, Lat, YearDay, Time, Satellite, Method, Ecosys, FRP\n-121.4, 39.8, 2018312, 1450, GOES-16, FDC, 22, 51.2\n"
    d = parse_hms_text(txt)
    assert d["time_utc"].iloc[0] == pd.Timestamp("2018-11-08 14:50", tz="UTC")


def test_viirs_group_reader(tmp_path):
    p = tmp_path / "det.h5"
    with h5py.File(p, "w") as h5:
        g = h5.create_group("CAMP")
        g.attrs["fire_id"] = "00016737"
        cols = {"latitude": np.array([39.8, 39.81]), "longitude": np.array([-121.4, -121.41]),
                "acq_time": np.array([1450, 1452])}
        g.attrs["n_columns"] = 4
        for i, (k, v) in enumerate(cols.items()):
            c = g.create_group(f"col_{i}")
            c.attrs.update({"name": k, "type": "numeric", "dtype": str(v.dtype)})
            c.create_dataset("data", data=v)
        c = g.create_group("col_3")
        c.attrs.update({"name": "acq_date", "type": "string"})
        c.create_dataset("data", data=np.array([b"2018-11-08", b"2018-11-08"]))
        c.create_dataset("null_mask", data=np.array([False, False]))
    with h5py.File(p) as h5:
        df = ViirsFireSET.read_group(h5["CAMP"])
    assert list(df["latitude"]) == [39.8, 39.81]


# ---------------------------------------------------------------- fixes found against live data (2026-10-13)

def test_mtbs_event_id_point():
    from fsa.normalize import mtbs_event_point
    assert mtbs_event_point("CA3982012144020181108") == (39.82, -121.44)
    assert mtbs_event_point("CA_A") is None


def test_calendar_dates_stay_on_their_day():
    from fsa.normalize import calendar_date
    # FRAP / FPA-FOD store a calendar date as 00:00 UTC; it must not slide into the previous Pacific day
    assert calendar_date(1541635200000) == "2018-11-08"
    assert calendar_date("2018-11-08T00:00:00+00:00") == "2018-11-08"
    t, p = to_utc(calendar_date(1541635200000), "America/Los_Angeles")
    assert t == pd.Timestamp("2018-11-08 08:00", tz="UTC") and p == "date"


def test_pick_falls_through_null_columns_and_related_ids():
    from fsa.config import SourceSpec
    from fsa.normalize import normalize_rows
    spec = SourceSpec(id="inform_fodr", name="x", kind="arcgis", roles=["size"], lineage="IRWIN", cause_codes="inform",
                      fields={"size_acres": ["FinalAcres", "IncidentSize"], "irwin_id": ["IrwinID", "RelatedIrwinIDs"],
                              "cause": ["FireCauseID"]},
                      lineage_from_field={"field": "CreatedBySystem", "map": {"FireStat": "USFS-FIRESTAT"},
                                          "default": "IRWIN"})
    df = pd.DataFrame([{"FinalAcres": None, "IncidentSize": 63458.0, "FireCauseID": 3, "CreatedBySystem": "FireStat",
                        "RelatedIrwinIDs": "{155BFE23-90B7-4CB4-B867-3C6594CFA327}, {DCA86EEE-D1AB-4578-B01C-30ED22C6817B}"},
                       {"FinalAcres": None, "IncidentSize": 5.0, "FireCauseID": 1, "CreatedBySystem": "cfcad",
                        "RelatedIrwinIDs": None}])
    out = normalize_rows(df, spec, geometry_col=None)
    assert out["size_acres"].tolist() == [63458.0, 5.0]
    assert out["lineage"].tolist() == ["USFS-FIRESTAT", "IRWIN"]
    assert out["cause_class"].tolist() == ["Human", "Natural"]
    assert out["irwin_id"].iloc[0] == "155BFE23-90B7-4CB4-B867-3C6594CFA327"
    assert "DCA86EEE-D1AB-4578-B01C-30ED22C6817B" in out["irwin_ids"].iloc[0]


def test_rounded_copy_detected():
    from fsa.analysis import _copied_point
    fr = LocalFrame(-121.44, 39.82)
    def pt(lat, lon, dec):
        x, y = fr.xy(lon, lat)
        return {"lat": lat, "lon": lon, "x": x, "y": y, "decimals": dec}
    fireset = pt(39.820005, -121.440013, 6)          # WFIGS geometry with float noise
    assert _copied_point(fireset, pt(39.82, -121.44, 3))       # MTBS Event_ID
    assert not _copied_point(fireset, pt(39.8134, -121.4347, 4))  # CAL FIRE map pin, ~900 m away
