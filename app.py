import json
import requests
import pandas as pd
import streamlit as st
import xml.etree.ElementTree as ET

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

# USPS Web Tools
USPS_USER_ID = st.secrets.get("USPS_USER_ID", "PUT_YOUR_USPS_USERID_HERE")
USPS_ENDPOINT = "https://secure.shippingapis.com/ShippingAPI.dll"


@st.cache_data
def load_schools() -> pd.DataFrame:
    """Load schools from GitHub CSV."""
    df = pd.read_csv(SCHOOLS_CSV_URL)
    df = df.dropna(subset=[LAT_COL, LON_COL])
    return df


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
# USPS ADDRESS CLEANING
# -------------------------------------------------------------------

def build_mailing_address_from_cams(row) -> tuple[str, str, str, str]:
    """
    Build mailing address components from a CAMS row.

    You may need to adjust field names here to match the actual CAMS columns
    you see in the DataFrame.
    """
    # Try multiple possible CAMS field names, fall back gracefully
    house = str(
        row.get("STREETNUM", "")
        or row.get("ADDRNUM", "")
        or row.get("HOUSENUMBER", "")
        or ""
    ).strip()

    predir = str(row.get("PREDIR", "") or row.get("PRE_DIR", "") or "").strip()
    name = str(row.get("STREETNAME", "") or row.get("STREET_NAME", "") or "").strip()
    st_type = str(
        row.get("STREETTYPE", "") or row.get("STREET_TYPE", "") or ""
    ).strip()
    unit = str(
        row.get("UNIT", "")
        or row.get("UNITNUM", "")
        or row.get("UNIT_NUMBER", "")
        or ""
    ).strip()

    street_parts = [house, predir, name, st_type, unit]
    street = " ".join(p for p in street_parts if p)

    city = str(row.get("CITY", "") or row.get("COMMUNITY", "") or "").strip()
    state = str(row.get("STATE", "") or "CA").strip()
    zip_raw = str(row.get("ZIPCODE", "") or row.get("ZIP", "") or "").strip()
    zip5 = zip_raw[:5] if zip_raw else ""

    return street, city, state, zip5


def usps_verify_one(street: str, city: str, state: str, zip5: str) -> dict:
    """
    Call USPS Verify API for a single address.
    Returns standardized fields, or {} on error.
    """
    if not USPS_USER_ID or USPS_USER_ID.startswith("PUT_"):
        # Not configured; skip cleaning
        return {}

    xml = f"""
    <AddressValidateRequest USERID="{USPS_USER_ID}">
      <Revision>1</Revision>
      <Address ID="0">
        <Address1></Address1>
        <Address2>{street}</Address2>
        <City>{city}</City>
        <State>{state}</State>
        <Zip5>{zip5}</Zip5>
        <Zip4></Zip4>
      </Address>
    </AddressValidateRequest>
    """.strip()

    params = {"API": "Verify", "XML": xml}

    try:
        resp = requests.get(USPS_ENDPOINT, params=params, timeout=10)
        resp.raise_for_status()
    except requests.RequestException:
        return {}

    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError:
        return {}

    err = root.find(".//Error")
    if err is not None:
        return {}

    addr = root.find(".//Address")
    if addr is None:
        return {}

    def gettext(tag: str) -> str:
        el = addr.find(tag)
        return el.text.strip() if el is not None and el.text is not None else ""

    dpv_conf = gettext("DPVConfirmation")
    dpv_cmra = gettext("DPVCMRA")
    dpv_vac = gettext("DPVVacant")

    return {
        "usps_street": gettext("Address2"),
        "usps_city": gettext("City"),
        "usps_state": gettext("State"),
        "usps_zip5": gettext("Zip5"),
        "usps_zip4": gettext("Zip4"),
        "usps_dpv_confirmation": dpv_conf,
        "usps_dpv_cmra": dpv_cmra,
        "usps_dpv_vacant": dpv_vac,
    }


def usps_clean_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """
    For each CAMS result, call USPS Verify and append standardized columns.
    If USPS_USER_ID is not set, returns df unchanged with a warning.
    """
    if df.empty:
        return df

    if not USPS_USER_ID or USPS_USER_ID.startswith("PUT_"):
        st.warning("USPS_USER_ID not configured. Returning raw CAMS data.")
        return df

    results = []
    total = len(df)
    progress = st.progress(0)

    for i, (_, row) in enumerate(df.iterrows(), start=1):
        street, city, state, zip5 = build_mailing_address_from_cams(row)
        cleaned = {}
        if street and city and state:
            cleaned = usps_verify_one(street, city, state, zip5)

        merged = row.to_dict()
        merged.update(cleaned)
        results.append(merged)

        progress.progress(i / total)

    progress.empty()
    return pd.DataFrame(results)


# -------------------------------------------------------------------
# STREAMLIT APP
# -------------------------------------------------------------------

def main():
    st.set_page_config(page_title="CAMS → USPS Address Export", layout="wide")
    st.title("CAMS Address Selector (LA County) with USPS-cleaned export")

    st.markdown(
        """
        Workflow (single step):
        1. Pick a school from the dropdown (left sidebar).
        2. The map will zoom to that school and drop a marker.
        3. Draw a polygon or rectangle around the area you care about.
        4. Click **Run CAMS + USPS**.
           The app will:
           • Query LA County CAMS for all address points inside your shape, and  
           • Immediately run USPS address cleaning,  
           • Then give you a CSV with standardized mailing fields.
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

    # Map
    m = folium.Map(location=[school_lat, school_lon], zoom_start=16)

    popup_text = selected_school
    if school_short:
        popup_text += f" ({school_short})"

    folium.Marker(
        [school_lat, school_lon],
        popup=popup_text,
        tooltip=popup_text,
    ).add_to(m)

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
            st.success("Polygon detected. Ready to run CAMS + USPS.")
        else:
            st.info("Draw a polygon or rectangle to enable the query.")

        run_query = st.button("Run CAMS + USPS on drawn area")

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
                    with st.spinner("Cleaning addresses with USPS..."):
                        df_final = usps_clean_dataframe(df_cams)

            except Exception as e:
                st.error(f"Error while processing: {e}")

    with col2:
        st.markdown("### Results")
        if not df_final.empty:
            st.write(
                f"Found **{len(df_final)}** CAMS address points. "
                "USPS fields added where available."
            )
            st.dataframe(df_final)

            csv_bytes = df_final.to_csv(index=False).encode("utf-8")
            st.download_button(
                "Download USPS-ready CSV",
                data=csv_bytes,
                file_name="cams_usps_cleaned_addresses.csv",
                mime="text/csv",
            )
        else:
            st.write("No results yet. Draw an area and click **Run CAMS + USPS**.")


if __name__ == "__main__":
    main()
