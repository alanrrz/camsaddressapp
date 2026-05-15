import json
import math
import re

import folium
import pandas as pd
import requests
import streamlit as st
from folium.plugins import Draw
from streamlit_folium import st_folium

# -------------------------------------------------------------------
# CONFIGURATION
# -------------------------------------------------------------------

SCHOOLS_CSV_URL = (
    "https://raw.githubusercontent.com/alanrrz/la_buffer_app_clean/"
    "ab73deb13c0a02107f43001161ab70891630a9c7/schools.csv"
)

SCHOOL_NAME_COL = "LABEL"
LAT_COL = "LAT"
LON_COL = "LON"
SHORTNAME_COL = "SHORTNAME"

CAMS_URL = (
    "https://arcgis.gis.lacounty.gov/arcgis/rest/services/DRP/"
    "GISNET_Public/MapServer/402/query"
)

# Buffer radius presets in feet (common LAUSD outreach footprints)
RADIUS_PRESETS_FT = {
    "500 ft": 500,
    "1,000 ft": 1000,
    "1,500 ft": 1500,
    "2,000 ft": 2000,
    "Custom": None,
}

# -------------------------------------------------------------------
# DATA LOADING
# -------------------------------------------------------------------

@st.cache_data
def load_schools() -> pd.DataFrame:
    df = pd.read_csv(SCHOOLS_CSV_URL)
    df = df.dropna(subset=[LAT_COL, LON_COL])
    return df


# -------------------------------------------------------------------
# GEOMETRY HELPERS
# -------------------------------------------------------------------

def circle_to_polygon_coords(lat: float, lon: float, radius_ft: float, n_points: int = 64):
    """
    Approximate a circle around (lat, lon) with the given radius in feet
    as a polygon ring of [lon, lat] coordinates.
    """
    radius_m = radius_ft * 0.3048
    earth_radius_m = 6378137.0

    coords = []
    for i in range(n_points):
        bearing = (2 * math.pi * i) / n_points
        lat_rad = math.radians(lat)
        lon_rad = math.radians(lon)

        new_lat = math.asin(
            math.sin(lat_rad) * math.cos(radius_m / earth_radius_m)
            + math.cos(lat_rad) * math.sin(radius_m / earth_radius_m) * math.cos(bearing)
        )
        new_lon = lon_rad + math.atan2(
            math.sin(bearing) * math.sin(radius_m / earth_radius_m) * math.cos(lat_rad),
            math.cos(radius_m / earth_radius_m) - math.sin(lat_rad) * math.sin(new_lat),
        )
        coords.append([math.degrees(new_lon), math.degrees(new_lat)])

    coords.append(coords[0])  # close the ring
    return coords


def build_esri_polygon_from_geojson(geojson_geom: dict) -> dict:
    geom_type = geojson_geom.get("type")
    if geom_type != "Polygon":
        raise ValueError(f"Expected a Polygon geometry, got {geom_type}")
    coords = geojson_geom["coordinates"][0]
    return {"rings": [coords], "spatialReference": {"wkid": 4326}}


def build_esri_polygon_from_circle(lat: float, lon: float, radius_ft: float) -> dict:
    ring = circle_to_polygon_coords(lat, lon, radius_ft)
    return {"rings": [ring], "spatialReference": {"wkid": 4326}}


# -------------------------------------------------------------------
# CAMS QUERY
# -------------------------------------------------------------------

def query_cams_addresses(esri_polygon: dict) -> pd.DataFrame:
    """
    Query CAMS address points intersecting the polygon. Handles pagination
    so large outreach areas don't get silently truncated at 1,000 features.
    """
    all_rows = []
    offset = 0
    page_size = 1000

    while True:
        params = {
            "f": "geojson",
            "where": "1=1",
            "geometry": json.dumps(esri_polygon),
            "geometryType": "esriGeometryPolygon",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "*",
            "outSR": 4326,
            "resultOffset": offset,
            "resultRecordCount": page_size,
        }

        resp = requests.get(CAMS_URL, params=params, timeout=45)
        resp.raise_for_status()
        data = resp.json()

        features = data.get("features", [])
        if not features:
            break

        for feat in features:
            props = feat.get("properties", {})
            geom = feat.get("geometry", {})
            coords = geom.get("coordinates", [None, None])
            lon, lat = coords[0], coords[1]
            all_rows.append({**props, "longitude": lon, "latitude": lat})

        # Stop if we got less than a full page (last page)
        if len(features) < page_size:
            break
        offset += page_size

        # Safety cap so a runaway query can't hammer the service forever
        if offset >= 10000:
            break

    return pd.DataFrame(all_rows)


# -------------------------------------------------------------------
# ADDRESS POST-PROCESSING
# -------------------------------------------------------------------

def parse_unit_count(btype: str) -> int:
    """
    Estimate unit count from a building type string like '1-4', '5-9', '10-19'.
    Uses the LOW end of the range to stay conservative on mailing volume.
    Returns 1 if no range is detected.
    """
    if not btype:
        return 1
    match = re.search(r"(\d+)\s*-\s*(\d+)", str(btype))
    if match:
        low = int(match.group(1))
        return max(low, 1)
    return 1


def detect_apartment_note(row) -> str:
    btype = str(row.get("BldgTypePl", "") or row.get("BldgType", "")).strip()
    number = str(row.get("Number", "")).strip()

    if "-" in btype:
        return "MULTI-UNIT - PLEASE VERIFY"
    if "/" in number:
        return "MULTI-UNIT - PLEASE VERIFY"
    return ""


def build_street_field(row) -> str:
    """
    Build the 'Street' column in the format the team prefers, e.g.
        '2708 E 5Th  Street'
    Combining: Number + NumSuffix + PreDir + StreetName + PostType
    """
    parts = [
        str(row.get("Number", "")).strip(),
        str(row.get("NumSuffix", "")).strip(),
        str(row.get("PreDir", "")).strip(),
        str(row.get("StreetName", "")).strip(),
        str(row.get("PostType", "")).strip(),
    ]
    parts = [p for p in parts if p and p.lower() != "nan"]
    return " ".join(parts)


def prepare_address_output(df: pd.DataFrame, expand_multiunit: bool = False) -> pd.DataFrame:
    """
    Final dataset shaped to the team's preferred export format:
        Street | LegalComm | State | ZipCode
    Plus a helper 'address_note' column for QA.

    If expand_multiunit=True, multi-unit buildings are duplicated based on
    the low end of the BldgType range (so a '1-4' becomes 1 row, '5-9'
    becomes 5 rows, etc.) and a UnitLabel column is added.
    """
    if df.empty:
        return df

    df = df.copy()
    df["Street"] = df.apply(build_street_field, axis=1)
    df["address_note"] = df.apply(detect_apartment_note, axis=1)

    # State (CAMS doesn't always include it; LA County is CA)
    df["State"] = "CA"

    # Clean ZIP to 5 digits
    df["ZipCode"] = df.get("ZipCode", "").astype(str).str[:5]

    # City fallback chain
    df["LegalComm"] = df["LegalComm"].fillna("").astype(str)
    if "PostComm1" in df.columns:
        df.loc[df["LegalComm"] == "", "LegalComm"] = df["PostComm1"].fillna("").astype(str)

    # Sort: street name, then number numerically
    df["_sort_num"] = pd.to_numeric(df.get("Number"), errors="coerce")
    df = df.sort_values(by=["StreetName", "_sort_num"], ascending=[True, True])

    if expand_multiunit:
        # Estimate units from BldgTypePl / BldgType and duplicate rows
        btype_series = df.get("BldgTypePl", "")
        if btype_series is None or (hasattr(btype_series, "empty") and btype_series.empty):
            btype_series = df.get("BldgType", "")
        df["_unit_count"] = btype_series.fillna("").astype(str).apply(parse_unit_count)

        # Replicate rows
        df = df.loc[df.index.repeat(df["_unit_count"])].reset_index(drop=True)

        # Add unit label (just a counter within each repeated group)
        df["UnitLabel"] = df.groupby(level=0, group_keys=False).cumcount() + 1
        df.loc[df["_unit_count"] <= 1, "UnitLabel"] = ""

        export_cols = ["Street", "UnitLabel", "LegalComm", "State", "ZipCode", "address_note"]
    else:
        export_cols = ["Street", "LegalComm", "State", "ZipCode", "address_note"]

    existing_cols = [c for c in export_cols if c in df.columns]
    return df[existing_cols].reset_index(drop=True)


# -------------------------------------------------------------------
# STREAMLIT APP
# -------------------------------------------------------------------

def main():
    st.set_page_config(
        page_title="HOME - Household Outreach Mapping Engine",
        layout="wide",
    )

    st.title("HOME - Household Outreach Mapping Engine")
    st.caption(
        "Pick a school, choose a buffer radius OR draw a custom area, "
        "and download a mailing list from LA County CAMS."
    )

    # ---------------------------------------------------------------
    # SIDEBAR: SCHOOL + RADIUS PRESET
    # ---------------------------------------------------------------
    try:
        schools_df = load_schools()
    except Exception as e:
        st.error(f"Error loading schools CSV from GitHub: {e}")
        return

    if schools_df.empty:
        st.error("Schools CSV loaded but is empty or missing coordinates.")
        return

    school_names = (
        schools_df[SCHOOL_NAME_COL].dropna().astype(str).sort_values().unique()
    )

    st.sidebar.header("Step 1 - Choose a school")
    selected_school = st.sidebar.selectbox("School", school_names, index=0)

    school_row = schools_df[schools_df[SCHOOL_NAME_COL] == selected_school].iloc[0]
    school_lat = float(school_row[LAT_COL])
    school_lon = float(school_row[LON_COL])
    school_short = str(school_row.get(SHORTNAME_COL, ""))

    st.sidebar.write(f"**Selected:** {selected_school}")
    st.sidebar.write(f"**Lat/Lon:** {school_lat:.6f}, {school_lon:.6f}")

    st.sidebar.header("Step 2 - Choose your area")
    area_mode = st.sidebar.radio(
        "How do you want to define the outreach area?",
        ["Buffer radius (quick)", "Draw on map (custom)"],
        index=0,
    )

    radius_ft = None
    if area_mode == "Buffer radius (quick)":
        preset = st.sidebar.selectbox(
            "Radius from school",
            list(RADIUS_PRESETS_FT.keys()),
            index=1,  # default to 1,000 ft
        )
        if preset == "Custom":
            radius_ft = st.sidebar.number_input(
                "Custom radius (feet)",
                min_value=50, max_value=10000, value=750, step=50,
            )
        else:
            radius_ft = RADIUS_PRESETS_FT[preset]

    st.sidebar.header("Step 3 - Mailing options")
    expand_multiunit = st.sidebar.checkbox(
        "Expand multi-unit buildings",
        value=False,
        help=(
            "Duplicates rows for buildings flagged as multi-unit so each unit "
            "gets its own 'Resident' label. Uses the low end of the CAMS range "
            "to stay conservative."
        ),
    )
    dedupe = st.sidebar.checkbox(
        "Remove duplicate addresses",
        value=True,
        help="Drops rows where the Street + ZipCode combo is identical.",
    )

    # ---------------------------------------------------------------
    # MAP
    # ---------------------------------------------------------------
    m = folium.Map(location=[school_lat, school_lon], zoom_start=16, tiles=None)

    folium.TileLayer("OpenStreetMap", name="Regular View", control=True).add_to(m)
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        attr="Esri",
        name="Satellite View",
        overlay=False,
        control=True,
    ).add_to(m)

    popup_text = selected_school + (f" ({school_short})" if school_short else "")
    folium.Marker([school_lat, school_lon], popup=popup_text, tooltip=popup_text).add_to(m)

    # Show buffer circle if radius mode
    if area_mode == "Buffer radius (quick)" and radius_ft:
        folium.Circle(
            location=[school_lat, school_lon],
            radius=radius_ft * 0.3048,  # feet to meters
            color="#1f77b4",
            weight=2,
            fill=True,
            fill_opacity=0.10,
            popup=f"{radius_ft} ft buffer",
        ).add_to(m)

    # Drawing tools only matter in custom mode but always available
    if area_mode == "Draw on map (custom)":
        Draw(
            export=False,
            position="topleft",
            draw_options={
                "polyline": False,
                "rectangle": True,
                "circle": False,
                "circlemarker": False,
                "marker": False,
                "polygon": True,
            },
            edit_options={"edit": True, "remove": True},
        ).add_to(m)

    folium.LayerControl().add_to(m)

    st.markdown("### Map preview")
    if area_mode == "Buffer radius (quick)":
        st.write(f"Showing a **{radius_ft} ft** buffer around {selected_school}.")
    else:
        st.write("Use the toolbar in the top-left of the map to draw a rectangle or polygon.")

    map_data = st_folium(
        m, width=900, height=600,
        returned_objects=["last_active_drawing"],
    )

    # ---------------------------------------------------------------
    # RUN QUERY
    # ---------------------------------------------------------------
    col1, col2 = st.columns([1, 2])

    with col1:
        if area_mode == "Buffer radius (quick)":
            st.success(f"Buffer ready: {radius_ft} ft around school.")
        else:
            last = map_data.get("last_active_drawing")
            if last:
                st.success("Shape detected. Ready to get addresses.")
            else:
                st.info("Draw a rectangle or polygon to enable the address lookup.")

        run_query = st.button("Get addresses in this area", type="primary")

    df_final = pd.DataFrame()

    if run_query:
        try:
            if area_mode == "Buffer radius (quick)":
                esri_polygon = build_esri_polygon_from_circle(school_lat, school_lon, radius_ft)
            else:
                last = map_data.get("last_active_drawing")
                if not last or "geometry" not in last:
                    st.error("No shape detected. Please draw a rectangle or polygon first.")
                    return
                esri_polygon = build_esri_polygon_from_geojson(last["geometry"])

            with st.spinner("Querying CAMS..."):
                df_cams = query_cams_addresses(esri_polygon)

            if df_cams.empty:
                st.warning("No CAMS address points found in that area.")
            else:
                df_final = prepare_address_output(df_cams, expand_multiunit=expand_multiunit)

                if dedupe and not df_final.empty:
                    before = len(df_final)
                    df_final = df_final.drop_duplicates(subset=["Street", "ZipCode"]).reset_index(drop=True)
                    removed = before - len(df_final)
                    if removed > 0:
                        st.caption(f"Removed {removed} duplicate address rows.")

        except Exception as e:
            st.error(f"Error while processing: {e}")

    # ---------------------------------------------------------------
    # RESULTS
    # ---------------------------------------------------------------
    with col2:
        st.markdown("### Addresses in selected area")
        if not df_final.empty:
            total = len(df_final)
            flagged = (df_final["address_note"] != "").sum() if "address_note" in df_final.columns else 0
            unique_streets = df_final["Street"].nunique() if "Street" in df_final.columns else 0

            mc1, mc2, mc3 = st.columns(3)
            mc1.metric("Total mail pieces", f"{total:,}")
            mc2.metric("Unique street addresses", f"{unique_streets:,}")
            mc3.metric("Multi-unit flagged", f"{flagged:,}")

            st.dataframe(df_final, use_container_width=True)

            # Mail-vendor export: just the 4 columns the team wants
            vendor_cols = [c for c in ["Street", "LegalComm", "State", "ZipCode"] if c in df_final.columns]
            vendor_df = df_final[vendor_cols]
            vendor_csv = vendor_df.to_csv(index=False, sep="\t").encode("utf-8")

            st.download_button(
                "Download mailing list (team format, tab-separated)",
                data=vendor_csv,
                file_name=f"mailing_list_{school_short or 'school'}.tsv",
                mime="text/tab-separated-values",
            )

            full_csv = df_final.to_csv(index=False).encode("utf-8")
            st.download_button(
                "Download full data with QA notes (CSV)",
                data=full_csv,
                file_name=f"mailing_list_{school_short or 'school'}_full.csv",
                mime="text/csv",
            )
        else:
            st.write("No results yet. Set your area and click **Get addresses in this area**.")


if __name__ == "__main__":
    main()
