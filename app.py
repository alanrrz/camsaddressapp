import json
import requests
import pandas as pd
import streamlit as st

import folium
from folium.plugins import Draw
from streamlit_folium import st_folium

# -------------------------------------------------------------------
# CONFIGURATION: YOUR GITHUB CSV + COLUMN NAMES
# -------------------------------------------------------------------

SCHOOLS_CSV_URL = (
    "https://raw.githubusercontent.com/alanrrz/la_buffer_app_clean/"
    "ab73deb13c0a02107f43001161ab70891630a9c7/schools.csv"
)

# CSV headers: LABEL,LAT,LON,SHORTNAME
SCHOOL_NAME_COL = "LABEL"
LAT_COL = "LAT"
LON_COL = "LON"
SHORTNAME_COL = "SHORTNAME"

# LA County CAMS Address Points feature service
CAMS_URL = (
    "https://arcgis.gis.lacounty.gov/arcgis/rest/services/DRP/"
    "GISNET_Public/MapServer/402/query"
)


# -------------------------------------------------------------------
# DATA LOADING
# -------------------------------------------------------------------

@st.cache_data
def load_schools() -> pd.DataFrame:
    """Load schools from GitHub CSV."""
    df = pd.read_csv(SCHOOLS_CSV_URL)
    df = df.dropna(subset=[LAT_COL, LON_COL])
    return df


# -------------------------------------------------------------------
# CAMS QUERY HELPERS
# -------------------------------------------------------------------

def build_esri_polygon_from_geojson(geojson_geom: dict) -> dict:
    """
    Convert a GeoJSON Polygon geometry into ArcGIS polygon JSON.
    Expects geometry like:
      {"type": "Polygon", "coordinates": [[[lon, lat], [lon, lat], ...]]}
    """
    geom_type = geojson_geom.get("type")
    if geom_type != "Polygon":
        raise ValueError(f"Expected a Polygon geometry, got {geom_type}")

    coords = geojson_geom["coordinates"][0]  # first ring
    esri_polygon = {
        "rings": [coords],
        "spatialReference": {"wkid": 4326},
    }
    return esri_polygon


def query_cams_addresses(esri_polygon: dict) -> pd.DataFrame:
    """
    Query CAMS address points that intersect the given polygon.
    Returns a Pandas DataFrame of attributes plus geometry coordinates.
    """
    params = {
        "f": "geojson",
        "where": "1=1",
        "geometry": json.dumps(esri_polygon),
        "geometryType": "esriGeometryPolygon",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "*",
        "outSR": 4326,
    }

    resp = requests.get(CAMS_URL, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    features = data.get("features", [])
    if not features:
        return pd.DataFrame()

    rows = []
    for feat in features:
        props = feat.get("properties", {})
        geom = feat.get("geometry", {})
        coords = geom.get("coordinates", [None, None])

        lon, lat = coords[0], coords[1]
        row = {
            **props,
            "longitude": lon,
            "latitude": lat,
        }
        rows.append(row)

    return pd.DataFrame(rows)


# -------------------------------------------------------------------
# ADDRESS POST-PROCESSING (NO USPS)
# -------------------------------------------------------------------

def detect_apartment_note(row) -> str:
    """
    Mark likely apartment / condo addresses.

    Heuristics:
      - If BldgTypePl or BldgType contains '1-4'
      - Or if UnitName is not empty

    Adjust this logic if CAMS uses different codes for multi-family.
    """
    btype = str(row.get("BldgTypePl", "") or row.get("BldgType", "")).upper()
    unit = str(row.get("UnitName", "")).strip()

    if "1-4" in btype or "APT" in btype or "APART" in btype or "CONDO" in btype:
        return "APARTMENT / CONDO (check unit numbers)"
    if unit:
        return "UNIT PRESENT (check apartment/condo number)"
    return ""


def prepare_address_output(df: pd.DataFrame) -> pd.DataFrame:
    """
    Reduce CAMS output to the requested columns and add apartment/condo note.

    Columns returned:
      - FullAddress
      - NumPrefix
      - Number
      - StreetName
      - PostType
      - address_note
    """
    if df.empty:
        return df

    df = df.copy()

    # Add note column
    df["address_note"] = df.apply(detect_apartment_note, axis=1)

    desired_cols = [
        "FullAddress",
        "NumPrefix",
        "Number",
        "StreetName",
        "PostType",
        "address_note",
    ]

    # Keep only the columns that actually exist in the DataFrame
    existing_cols = [c for c in desired_cols if c in df.columns]
    return df[existing_cols]


# -------------------------------------------------------------------
# STREAMLIT APP
# -------------------------------------------------------------------

def main():
    st.set_page_config(page_title="CAMS Address Export", layout="wide")
    st.title("CAMS Address Selector (LA County)")

    st.markdown(
        """
        **Workflow:**

        1. Pick a school from the dropdown in the left sidebar.  
        2. The map will zoom to that school and drop a marker.  
        3. Draw a polygon or rectangle around the area you care about.  
        4. Click **Run CAMS query**.  

        The app will:

        - Query LA County CAMS for all address points inside your shape.  
        - Mark likely apartment/condo addresses.  
        - Return only: **FullAddress, NumPrefix, Number, StreetName, PostType, address_note**.  
        """
    )

    # Load schools
    try:
        schools_df = load_schools()
    except Exception as e:
        st.error(f"Error loading schools CSV from GitHub: {e}")
        return

    if schools_df.empty:
        st.error("Schools CSV loaded but is empty or missing coordinates.")
        return

    # Sidebar dropdown
    school_names = (
        schools_df[SCHOOL_NAME_COL].dropna().astype(str).sort_values().unique()
    )

    st.sidebar.header("School selection")
    selected_school = st.sidebar.selectbox(
        "Select a school",
        school_names,
        index=0 if len(school_names) > 0 else None,
    )

    school_row = schools_df[schools_df[SCHOOL_NAME_COL] == selected_school].iloc[0]
    school_lat = float(school_row[LAT_COL])
    school_lon = float(school_row[LON_COL])
    school_short = str(school_row.get(SHORTNAME_COL, ""))

    st.sidebar.write(f"**Full name (LABEL):** {selected_school}")
    if school_short:
        st.sidebar.write(f"**Short name:** {school_short}")
    st.sidebar.write(f"**Lat/Lon:** {school_lat:.6f}, {school_lon:.6f}")

    # Map centered on selected school
    m = folium.Map(location=[school_lat, school_lon], zoom_start=16)

    popup_text = selected_school
    if school_short:
        popup_text += f" ({school_short})"

    folium.Marker(
        [school_lat, school_lon],
        popup=popup_text,
        tooltip=popup_text,
    ).add_to(m)

    # Drawing controls (polygon + rectangle)
    draw = Draw(
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
    )
    draw.add_to(m)

    st.markdown("### Map")
    st.write(
        "Choose a school in the sidebar, then draw a polygon or rectangle around it."
    )

    map_data = st_folium(
        m,
        width=900,
        height=600,
        returned_objects=["last_active_drawing"],
    )

    col1, col2 = st.columns([1, 2])

    with col1:
        last = map_data.get("last_active_drawing")
        if last:
            st.success("Polygon detected. Ready to run CAMS query.")
        else:
            st.info("Draw a polygon or rectangle to enable the query.")

        run_query = st.button("Run CAMS query on drawn area")

    df_final = pd.DataFrame()

    if run_query:
        last = map_data.get("last_active_drawing")
        if not last or "geometry" not in last:
            st.error("No polygon detected. Please draw a polygon or rectangle first.")
        else:
            try:
                geojson_geom = last["geometry"]
                esri_polygon = build_esri_polygon_from_geojson(geojson_geom)

                with st.spinner("Querying CAMS service..."):
                    df_cams = query_cams_addresses(esri_polygon)

                if df_cams.empty:
                    st.warning("No CAMS address points found in that area.")
                    df_final = df_cams
                else:
                    df_final = prepare_address_output(df_cams)

            except Exception as e:
                st.error(f"Error while processing: {e}")

    with col2:
        st.markdown("### Results")
        if not df_final.empty:
            st.write(
                f"Addresses returned: **{len(df_final)}** rows "
                "(FullAddress + basic components + note)."
            )
            st.dataframe(df_final)

            csv_bytes = df_final.to_csv(index=False).encode("utf-8")
            st.download_button(
                "Download address CSV",
                data=csv_bytes,
                file_name="cams_addresses_basic.csv",
                mime="text/csv",
            )
        else:
            st.write("No results yet. Draw an area and click **Run CAMS query**.")


if __name__ == "__main__":
    main()
