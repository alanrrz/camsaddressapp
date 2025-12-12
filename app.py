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
    Flag possible multi-unit properties when:
      - BldgTypePl or BldgType contains a dash (e.g., '1-4', '5-9'),
      - or the street number includes a fraction (like '1/2', '3/4', etc.).
    """
    btype = str(row.get("BldgTypePl", "") or row.get("BldgType", "")).strip()
    number = str(row.get("Number", "")).strip()

    # Multi-family building type (range)
    if "-" in btype:
        return "MULTI-UNIT - PLEASE VERIFY"

    # Fractional address number (e.g., 1748 1/2, 1818 3/4)
    if "/" in number:
        return "MULTI-UNIT - PLEASE VERIFY"

    return ""


def build_mailing_address(row) -> str:
    """
    Build a clean mailing address from:
      Number, StreetName, PostType, LegalComm, ZipCode
    Example:
      816 108Th St Los Angeles 90059
    """
    number = str(row.get("Number", "")).strip()
    street = str(row.get("StreetName", "")).strip()
    post_type = str(row.get("PostType", "")).strip()
    city = str(row.get("LegalComm", "") or row.get("PostComm1", "")).strip()
    zip_raw = str(row.get("ZipCode", "")).strip()
    zip5 = zip_raw[:5] if zip_raw else ""

    parts = [number, street, post_type, city, zip5]
    return " ".join(p for p in parts if p)


def prepare_address_output(df: pd.DataFrame) -> pd.DataFrame:
    """
    Prepare final dataset for export:
      - Creates MailingAddress from basic fields
      - Flags multi-unit properties
      - Keeps selected columns
      - SORTS by StreetName then Number (numerically)
    """
    if df.empty:
        return df

    df = df.copy()
    df["MailingAddress"] = df.apply(build_mailing_address, axis=1)
    df["address_note"] = df.apply(detect_apartment_note, axis=1)

    # ---------------------------------------------------------
    # SORTING LOGIC ADDED HERE
    # ---------------------------------------------------------
    # 1. Create a temporary numeric column so "2" comes before "10"
    df["_sort_num"] = pd.to_numeric(df["Number"], errors="coerce")
    
    # 2. Sort by StreetName (A-Z), then by the numeric Number
    df = df.sort_values(by=["StreetName", "_sort_num"], ascending=[True, True])
    # ---------------------------------------------------------

    desired_cols = [
        "MailingAddress",
        "FullAddress_EnerGov",
        "NumPrefix",
        "Number",
        "StreetName",
        "PostType",
        "LegalComm",
        "ZipCode",
        "address_note",
    ]

    existing_cols = [c for c in desired_cols if c in df.columns]
    
    # Return only the desired columns (dropping the temp sort column)
    return df[existing_cols]

# -------------------------------------------------------------------
# STREAMLIT APP
# -------------------------------------------------------------------

def main():
    st.set_page_config(
        page_title="HOME - Household Outreach Mapping Engine",
        layout="wide",
    )

    st.title("HOME - Household Outreach Mapping Engine")
    st.caption("Draw an area around a school to download household addresses from LA County CAMS.")

    st.markdown(
        """
        **How to use this tool**

        1. On the left, choose a school.
        2. On the map, use the toolbar in the top-left to draw a box or outline
           (rectangle or polygon) around the area you want.
        3. Click **Get addresses in this area**.

        The app will:
        - Pull all CAMS address points inside your shape.
        - Flag possible multi-unit (apartment/condo) addresses.
        - Let you download a CSV of mailing addresses.
        """
    )

    st.info(
        "On the map, look at the small toolbar in the **top-left**. "
        "Click the square or shape icon to start drawing a rectangle or polygon."
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

    st.sidebar.header("Step 1 - Choose a school")
    selected_school = st.sidebar.selectbox(
        "School",
        school_names,
        index=0 if len(school_names) > 0 else None,
    )

    school_row = schools_df[schools_df[SCHOOL_NAME_COL] == selected_school].iloc[0]
    school_lat = float(school_row[LAT_COL])
    school_lon = float(school_row[LON_COL])
    school_short = str(school_row.get(SHORTNAME_COL, ""))

    st.sidebar.write(f"**Selected school:** {selected_school}")
    st.sidebar.write(f"**Lat/Lon:** {school_lat:.6f}, {school_lon:.6f}")

# ---------------------------------------------------------
    # MAP SETUP: REGULAR (DEFAULT) + SATELLITE TOGGLE
    # ---------------------------------------------------------
    
    # 1. Create the map container, but turn off the default tiles (tiles=None)
    #    so we can control exactly which layer is added first.
    m = folium.Map(location=[school_lat, school_lon], zoom_start=16, tiles=None)

    # 2. Add "Regular" view (OpenStreetMap) FIRST.
    #    Because this is added first, it will be the default view on load.
    folium.TileLayer(
        "OpenStreetMap",
        name="Regular View",  # Name that appears in the toggle
        control=True
    ).add_to(m)

    # 3. Add "Satellite" view SECOND.
    #    It will be available in the menu but hidden by default.
    folium.TileLayer(
        tiles='https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
        attr='Esri',
        name='Satellite View',
        overlay=False,
        control=True
    ).add_to(m)

    # 4. Add the toggle control (top-right layer icon)
    folium.LayerControl().add_to(m)

    # --------------------------------------------------------- m = folium.Map(location=[school_lat, school_lon], zoom_start=16)

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

    st.markdown("### Step 2 - Draw your area on the map")
    st.write(
        "Use the toolbar in the top-left of the map to draw a rectangle or polygon around the area you want."
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
            st.success("Shape detected. Ready to get addresses.")
        else:
            st.info("Draw a rectangle or polygon to enable the address lookup.")

        run_query = st.button("Step 3 - Get addresses in this area")

    df_final = pd.DataFrame()

    if run_query:
        last = map_data.get("last_active_drawing")
        if not last or "geometry" not in last:
            st.error("No shape detected. Please draw a rectangle or polygon first.")
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
        st.markdown("### Addresses in selected area")
        if not df_final.empty:
            st.write(
                f"Addresses returned: **{len(df_final)}** "
                "(MailingAddress + components + note)."
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
            st.write("No results yet. Draw an area and click **Get addresses in this area**.")


if __name__ == "__main__":
    main()
