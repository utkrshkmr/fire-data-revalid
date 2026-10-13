# Government data sources for California wildfire attributes

Every source below is published by a U.S. federal agency or the State of California. Each entry gives what it
contributes, how it links to other sources, and the caveats that matter when comparing it with the rest.

Checked against live service metadata on 2026-10-13 unless marked *unverified*. Run `fsa probe` before a full
run; it checks every endpoint and field mapping and writes `out/probe_report.csv`.

**Trust tiers.** A = record of the responsible agency, or independent physical measurement.
B = official, but operational or second-hand. C = derivative merge, kept for comparison and left out of the
consensus.

**Lineage.** Sources that copy one upstream record share a lineage. The consensus counts each lineage once.

## Quick map: which source answers which question

| Question | Reported by agencies | Independent check |
|---|---|---|
| Where did it start? | WFIGS / IRWIN, InFORM, CAL FIRE incidents, USFS FireStat, FPA-FOD, MTBS points | first VIIRS overpass, first GOES-R detection, HMS |
| Final perimeter | CAL FIRE FRAP, NIFC perimeter history, WFIGS perimeters, USFS EDW, FIRIS | MTBS (Landsat), NASA FEDS (VIIRS) |
| When was it discovered? | IRWIN, InFORM, CAL FIRE, FireStat, FPA-FOD, FRAP (date only) | GOES-R (5 min), VIIRS (about 6 passes a day) |
| When was it contained or out? | IRWIN, InFORM, FRAP, FPA-FOD, FireStat | none |
| Final size | all of the above | area of the consensus perimeter |
| Cause | IRWIN, InFORM, FRAP, FPA-FOD, FireStat | none |
| Structure damage | CAL FIRE DINS (already in FireSET) | none |

## A. Incident records: point of origin, times, size, cause

### 1. NIFC WFIGS Incident Locations (IRWIN)
- **Agency:** National Interagency Fire Center, from the NWCG Integrated Reporting of Wildland-Fire Information (IRWIN).
- **Endpoint:** `https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/WFIGS_Incident_Locations/FeatureServer/0` (point layer, 2,000 records per page).
- **Gives:** `InitialLatitude`/`InitialLongitude` (point of origin as first entered), `FireDiscoveryDateTime`, `ContainmentDateTime`, `ControlDateTime`, `FireOutDateTime`, `FinalAcres`, `DiscoveryAcres`, `IncidentSize`, `FireCause`, `FireCauseGeneral`, `FireCauseSpecific`, plus `POO*` jurisdiction fields, `CpxName`/`IsCpxChild` for complexes.
- **Keys:** `IrwinID`, `UniqueFireIdentifier`, `LocalIncidentIdentifier`.
- **Lineage:** IRWIN. **Tier:** A. **Times:** UTC epoch milliseconds.
- **Caveats:** The point is the initial report location. It is not always updated after investigation. FireSET already uses this source, so it cannot confirm itself.

### 2. NIFC InFORM Fire Occurrence Data Records (public)
- **Agency:** NIFC, from the interagency InFORM final fire reporting system.
- **Endpoint:** `https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/InFORM_FireOccurrence_Public/FeatureServer/0`
- **Gives:** final-report point of origin, `FireDiscoveryDateTime`, `FinalAcres`, `CalculatedAcres`, `DiscoveryAcres`, POO jurisdiction.
- **Coverage:** complete from 2020 to the present; earlier years are being back-filled.
- **Lineage:** IRWIN (InFORM writes back to IRWIN). **Tier:** A.
- **Caveats:** NIFC asks users not to run repeated relative-date queries such as "in the last N days". The project uses fixed date ranges.

### 3. CAL FIRE incident list (fire.ca.gov)
- **Agency:** CAL FIRE.
- **Endpoint:** `https://www.fire.ca.gov/umbraco/api/IncidentApi/List?inactive=true`, or the GeoJSON variant `.../GeoJsonList?inactive=true`.
- **Gives:** `Latitude`/`Longitude` (incident map location), `Started` (Pacific local time), `AcresBurned`, `PercentContained`, `County`, `AdminUnit`, `Url`, `UniqueId`.
- **Coverage:** CAL FIRE and State Responsibility Area incidents, 2013 onward. Federal-only fires are often missing.
- **Keys:** none shared with IRWIN, so the project matches on name, date and location.
- **Lineage:** CALFIRE-OPS. **Tier:** A.
- **Caveats:** The site's robots.txt disallows crawling. The project makes one cached API call per run and does not scrape incident pages.

### 4. USFS EDW Fire Occurrence Point (FireStat)
- **Agency:** USDA Forest Service Enterprise Data Warehouse.
- **Endpoint:** `https://apps.fs.usda.gov/arcx/rest/services/EDW/EDW_FireOccurrenceAndPerimeter_01/MapServer/9` (all years). Layers 0, 16, 19, 22, 25 and 28 are single years.
- **Gives:** `latdd83`/`longdd83`, `discoverydatetime`, `fireoutdatetime`, `totalacres`, `statcause`, `sizeclass`, `pointtype`, `accuracy`.
- **Keys:** `uniqfireid`, `localfirenum`, `fireoccurid` (no IRWIN ID).
- **Coverage:** fires on National Forest System land or under USFS protection only.
- **Lineage:** USFS-FIRESTAT. **Tier:** A.
- **Caveats:** Date fields may hold local clock time tagged as UTC. The timing analysis flags offsets of 7 or 8 hours as likely time-zone errors.

### 5. FPA Fire Occurrence Database (FPA-FOD), 7th edition, 1992–2024
- **Agency:** USDA Forest Service Research Data Archive. Short, K.C. 2026, doi:10.2737/RDS-2013-0009.7.
- **Access:** download from the Research Data Archive (GPKG, GDB, SQLite or ACCDB; table `Fires`). There is also a map service at `apps.fs.usda.gov/ArcX/rest/services/EDW/EDW_FireOccurrence6thEdition_01` (6th edition).
- **Gives:** `LATITUDE`/`LONGITUDE` (at least PLSS-section precision), `DISCOVERY_DATE` + `DISCOVERY_TIME` (local), `CONT_DATE` + `CONT_TIME`, `FIRE_SIZE`, NWCG cause classification, `MTBS_ID`, ICS-209-PLUS join IDs.
- **Coverage:** 2.66 million U.S. records, compiled from federal, state and local reporting systems including CAL FIRE's.
- **Lineage:** FPA-FOD. **Tier:** A.
- **Caveats:** Column names in the config come from the 6th edition. `fsa probe` reports any that changed. 2025 fires are not covered.

### 6. MTBS fire occurrence locations *(unverified)*
- **Agency:** USGS EROS and USDA Forest Service GTAC (Monitoring Trends in Burn Severity).
- **Access:** national download from the USGS burn-severity portal (`https://burnseverity.cr.usgs.gov/direct-download?product=MTBS`).
- **Gives:** `BurnBndLat`/`BurnBndLon` (reported location), `Ig_Date`, `BurnBndAc`, `Event_ID`, and `irwinID` in recent releases.
- **Lineage:** MTBS-REPORTED. **Tier:** B, because the point is carried in from agency reports.

## B. Perimeters

### 7. CAL FIRE FRAP California Fire Perimeters (all)
- **Agency:** CAL FIRE Fire and Resource Assessment Program.
- **Endpoint:** `https://services1.arcgis.com/jUJYIo9tSA7EHvfZ/arcgis/rest/services/California_Historic_Fire_Perimeters/FeatureServer/0` (firep25_1).
- **Gives:** final perimeter, `ALARM_DATE`, `CONT_DATE` (both date only), `GIS_ACRES`, `CAUSE` (coded 1–19), `C_METHOD` (collection method), `OBJECTIVE`, `COMPLEX_NAME`.
- **Keys:** `IRWINID`, `INC_NUM`, `UNIT_ID`.
- **Lineage:** CALFIRE-FRAP. **Tier:** A. FireSET uses this as its ground truth.

### 8. NIFC InterAgency Fire Perimeter History (all years)
- **Agency:** NIFC. Merges USFS, BLM, BIA, FWS, NPS, Alaska, CAL FIRE and WFIGS final perimeters.
- **Endpoint:** `https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/InterAgencyFirePerimeterHistory_All_Years_View/FeatureServer/0`
- **Gives:** perimeter, `GIS_ACRES`, `MAP_METHOD`, `SOURCE`, `FEATURE_CA`.
- **Keys:** `IRWINID`, `UNQE_FIRE_ID`, `LOCAL_NUM`.
- **Coverage:** through the 2021 season.
- **Lineage:** taken from the `SOURCE` field, so a CAL FIRE-sourced perimeter here counts as the same lineage as FRAP. **Tier:** A.

### 9. NIFC WFIGS Interagency Perimeters
- **Endpoint:** `https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/WFIGS_Interagency_Perimeters/FeatureServer/0`
- **Gives:** latest perimeter per incident (`poly_GISAcres`, `poly_PolygonDateTime`, `poly_MapMethod`, `poly_FeatureCategory`), joined to IRWIN attributes (`attr_*`).
- **Coverage:** wildfire and prescribed-fire incidents from about 2021 onward.
- **Lineage:** NIFC-WFIGS. **Tier:** A.

### 10. USFS EDW Final Fire Perimeter
- **Endpoint:** `.../EDW/EDW_FireOccurrenceAndPerimeter_01/MapServer/10` (all years). Layer 11 holds all perimeter versions.
- **Gives:** `gisacres`, `totalacres`, `perimeterdatetime`, `featurecat`, `accuracy`, `uniqfireid`.
- **Coverage:** USFS fires only. **Lineage:** USFS-FIRESTAT. **Tier:** A.

### 11. MTBS burned area boundaries *(unverified)*
- **Agency:** USGS EROS and USFS GTAC.
- **Gives:** Landsat dNBR-derived burn boundary, `BurnBndAc`, `Ig_Date`, `Event_ID`, and `irwinID` in recent releases.
- **Coverage:** fires of 1,000 acres or more in the western U.S., 1984 to the latest release (about a 1–2 year lag).
- **Lineage:** MTBS. **Tier:** A. This is the one perimeter mapped independently of incident teams.
- **Caveats:** Unburned islands are excluded, so the area is often smaller than agency perimeters. Small fires are not mapped.

### 12. CA perimeters CAL FIRE / NIFC / FIRIS public view
- **Agency:** CAL FIRE, with the Cal OES-funded Fire Integrated Real-time Intelligence System (FIRIS) aerial infrared.
- **Endpoint:** `https://services1.arcgis.com/jUJYIo9tSA7EHvfZ/arcgis/rest/services/CA_Perimeters_NIFC_FIRIS_public_view/FeatureServer/0`
- **Gives:** dated operational perimeters (`poly_DateCurrent`, `source`, `mission`, `incident_number`, `area_acres`, `FireDiscoveryDate`).
- **Lineage:** FIRIS-OPS. **Tier:** B (operational, not final). The project uses the latest polygon per incident.

### 13. USGS Combined wildland fire polygons *(unverified fields)*
- Welty & Jeffries 2022, doi:10.5066/P9ZXGFY3. A merge of 40 agency datasets from the mid-1800s onward.
- **Tier:** C. Reported for comparison only and excluded from the consensus, because it re-uses sources 7–11.

## C. Independent satellite evidence (time and place of first heat)

### 14. NASA FIRMS VIIRS 375 m and MODIS 1 km
- **Agency:** NASA LANCE / FIRMS.
- **Access:** FIRMS archive downloads (yearly country CSVs) and the area API.
- **Use:** centre and time of the first overpass that sees the fire. FireSET's `detection_{YEAR}_filtered_350.h5` is read directly.
- **Lineage:** SAT-LEO.

### 15. NOAA GOES-R ABI Fire Detection and Characterization (FDC)
- **Agency:** NOAA NESDIS / NCEI. Coverage from 2017-05-24, 2 km pixels, every 5 minutes over CONUS.
- **Access:** NOAA Open Data Dissemination buckets on AWS (`noaa-goes16`, `noaa-goes17`, `noaa-goes18`, `noaa-goes19`), product `ABI-L2-FDCC`. Also NCEI and NOAA CLASS.
- **Use:** the earliest detection within 5 km of the candidate ignition. This is the tightest independent bound on ignition time.
- **Lineage:** SAT-GEO. Optional and off by default because it is heavy.

### 16. NOAA NESDIS Hazard Mapping System (HMS) fire points *(file pattern unverified)*
- Analyst-checked fire points from several satellites, published daily (`satepsanone.nesdis.noaa.gov/pub/FIRE/web/HMS/Fire_Points/`).
- **Lineage:** SAT-HMS. Optional.

### 17. NASA FEDS fire perimeters *(not wired in)*
- NASA Earth Information System: VIIRS-based event perimeters every 12 hours. Near-real-time services are at `gis.earthdata.nasa.gov/image/rest/services/FireTracking/`.
- **Status:** I could not confirm an archive endpoint covering 2018–2025. Add it once confirmed. The method is close to FireSET's own reconstruction, so it is not an independent check of it.

## D. Reference sources for manual review (not pulled automatically)

- **InciWeb** (USDA / NWCG): incident pages with location and narrative. There is no documented API; use it to adjudicate flagged fires.
- **NIROPS / National Incident Feature Service** (USFS National Infrared Operations; `ftp.wildfire.gov`): nightly infrared perimeters per incident. Useful for progression, but too heavy to automate here.
- **CAL FIRE incident pages, investigation reports and annual Redbooks**: authoritative narrative and cause for specific fires.
- **CAL FIRE DINS**: structure damage, already in FireSET.
- **ICS-209 situation reports**: the agency system (SIT-209) is not publicly queryable. **ICS-209-PLUS** is an academic compilation, not agency-published, so it is excluded; FPA-FOD carries its join IDs if needed.

## Sources considered and excluded

Commercial and NGO feeds (Watch Duty, Esri Living Atlas copies, Apify scrapers, community GeoJSON mirrors) are
excluded. Where a mirror re-publishes an agency layer, the agency endpoint above is used instead.
