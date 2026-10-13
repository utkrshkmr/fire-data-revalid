#!/usr/bin/env bash
# Download the agency files that fire-source-audit reads locally (about 2.5 GB unpacked).
# Paths match fire-source-audit/config/settings.yaml. Safe to rerun: existing files are kept.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DL="$ROOT/downloads"
UA="fire-source-audit/0.1 (research)"
get() {  # get URL OUTFILE
  [[ -s "$2" ]] && { echo "have $2"; return; }
  echo "get  $2"
  curl -sfL -A "$UA" -o "$2.part" "$1" && mv "$2.part" "$2"
}

# FPA-FOD 7th edition, 1992-2024 (Short 2026, doi:10.2737/RDS-2013-0009.7), GeoPackage
mkdir -p "$DL/fpa_fod"
get https://www.fs.usda.gov/rds/archive/products/RDS-2013-0009.7/RDS-2013-0009.7_Data_Format3_GPKG.zip \
    "$DL/fpa_fod/RDS-2013-0009.7_Data_Format3_GPKG.zip"
[[ -f "$DL/fpa_fod/Data/FPA_FOD_20260615.gpkg" ]] || unzip -o -q "$DL/fpa_fod/RDS-2013-0009.7_Data_Format3_GPKG.zip" -d "$DL/fpa_fod"

# MTBS composite burned-area boundaries and fire-occurrence points (USGS EROS / USFS GTAC)
MTBS=https://edcintl.cr.usgs.gov/downloads/sciweb1/shared/MTBS_Fire/data/composite_data
mkdir -p "$DL/mtbs"
get "$MTBS/burned_area_extent_shapefile/mtbs_perimeter_data.zip" "$DL/mtbs/mtbs_perimeter_data.zip"
get "$MTBS/fod_pt_shapefile/mtbs_fod_pts_data.zip" "$DL/mtbs/mtbs_fod_pts_data.zip"
[[ -f "$DL/mtbs/perims/mtbs_perims_DD.shp" ]] || unzip -o -q "$DL/mtbs/mtbs_perimeter_data.zip" -d "$DL/mtbs/perims"
[[ -f "$DL/mtbs/pts/mtbs_FODpoints_DD.shp" ]] || unzip -o -q "$DL/mtbs/mtbs_fod_pts_data.zip" -d "$DL/mtbs/pts"

# USGS combined wildland fire polygons (Welty & Jeffries 2022, ScienceBase 61aa537d...), 1.5 GB zip
mkdir -p "$DL/usgs_combined"
get "https://www.sciencebase.gov/catalog/file/get/61aa537dd34eb622f699df81?name=Fire_Feature_Data_Pro2_8_Geodatabase.zip" \
    "$DL/usgs_combined/Fire_Feature_Data_Pro2_8_Geodatabase.zip"
[[ -d "$DL/usgs_combined/Fire_Feature_Data.gdb" ]] || (cd "$DL/usgs_combined" && unzip -o -q Fire_Feature_Data_Pro2_8_Geodatabase.zip)

# NASA FIRMS VIIRS 375 m yearly US archives: S-NPP (viirs-snpp) and NOAA-20 (viirs-jpss1)
mkdir -p "$DL/firms"
for y in 2018 2019 2020 2021 2022 2023 2024 2025; do
  for sat in viirs-snpp viirs-jpss1; do
    get "https://firms.modaps.eosdis.nasa.gov/data/country/$sat/$y/${sat}_${y}_United_States.csv" \
        "$DL/firms/${sat}_${y}_United_States.csv"
    sleep 1
  done
done
echo "done: $DL"
