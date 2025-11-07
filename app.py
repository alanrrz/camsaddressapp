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
# IMPORTANT: set USPS_USER_ID in Streamlit Secrets for this app:
# Settings -> Secrets:
# USPS_USER_ID = "YOUR_WEBTOOLS_USERID"
USPS_USER_ID = st.secrets["USPS_USER_ID"]
USPS_ENDPOINT = "https://secure.shippingapis.com/ShippingAPI.dll"


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
# USPS ADDRESS CLEANING HELPERS
# -------------------------------------------------------------------

def build_mailing_address_from_cams(row):
    """
    Build mailing address components from a CAMS row using the actual fields:
      Number, PreDirAbbr, StreetName, PostTypeAbbr, UnitName,
      PostComm1 (city), ZipCode.
    """

    # House number
    house = str(row.get("Number", "")).strip()

    # Directional prefix (E, W, N, S)
    predir = str(row.get("PreDirAbbr", "")).strip()

    # Street name and type
    name = str(row.get("StreetName", "")).strip()
    st_type = str(row.get("PostTypeAbbr", "")).strip()

    # Optional unit (apt, suite, etc.)
    unit = str(row.get("UnitName", "")).strip()

    # Assemble street line: "816 E 108Th St" or with unit if present
    street_parts = [house, predir, name, st_type, unit]
    street = " ".join(p for p in street_parts if p)

    # City: prefer PostComm1, fall back to LegalComm
    city = str(row.get("PostComm1", "") or row.get("LegalComm", "")).strip()

    # State: all CAMS records are in LA County -> California
    state = "CA"

    # ZIP5 from ZipCode
    zip_raw = str(row.get("ZipCode", "")).strip()
    zip5 = zip_raw[:5] if zip_raw else ""

    return street, city, state, zip5


def usps_verify_one(street: str, city: str, state: str, zip5: str) -> dict:
    """
    Call USPS Verify API for a single address.
    Returns standardized fields, or an error description if USPS rejects it.
    """
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
    except requests.RequestException as e:
        return {"usps_error": f"HTTP error: {e}"}

    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError:
        return {"usps_error": "XML parse error"}

    # If USPS returns an <Error>, capture the message
    err = root.find(".//Error")
    if err is not None:
        number = (err.findtext("Number") or "").strip()
        desc = (err.findtext("Description") or "").strip()
        return {"usps_error": f"{number}: {desc}"}

    addr = root.find(".//Address")
    if addr is None:
        return {"usps_error": "No <Address> element in USPS response"}

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
        "usps_error": "",
    }


def detect_apartment_note(row) -> str:
    """
    If BldgTypePl or BldgType contains '1-4',
    treat it as an apartment/small multi-family and flag it.
    """
    btype = str(row.get("BldgTypePl", "") or row.get("BldgType", "")).strip()
    if "1-4" in btype:
        return "APARTMENT numbers"
    return ""


def usps_clean_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """
    Call USPS for each CAMS row and append:
      - street_sent_to_usps, city_sent_to_usps, etc.
      - usps_* standardized fields (if any)
      - usps_error (if USPS rejected or access failed)
      - usps_status: 'VERIFIED' or 'UNVERIFIED'
      - address_note: 'APARTMENT numbers' when applicable

    All rows are returned.
    """
    if df.empty:
        return df

    results = []
    total = len(df)
    progress = st.progress(0)

    for i, (_, row) in enumerate(df.iterrows(), start=1):
        street, city, state, zip5 = build_mailing_address_from_cams(row)

        if street and city and state:
            cleaned = usps_verify_one(street, city, state, zip5)
        else:
            cleaned = {"usps_error": "Missing street/city/state"}

        if cleaned.get("usps_error"):
            status = "UNVERIFIED"
        elif cleaned.get("usps_street"):
            status = "VERIFIED"
        else:
            status = "UNVERIFIED"

        note = detect_apartment_note(row)

        merged = row.to_dict()
        merged.update(
            {
                "street_sent_to_usps": street,
                "city_sent_to_usps": city,
                "state_sent_to_usps": state,
                "zip_sent_to_usps": zip5,
                "usps_status": status,
                "address_note": note,
            }
        )
        merged.update(cleaned)
        results.append(merged)

        progress.progress(i / total)

    progress.empty()
    df_all = pd.DataFrame(results)

    # Optional: show a quick summary of USPS errors in the UI
    if "usps_error" in df_all.columns:
        errors = (
            df_all["usps_error"]
            .fillna("")
            .loc[df_all["usps_error"] != ""]
            .value_counts()
            .head(5)
        )
        if not errors.empty:
            st.warning("Sample USPS errors (top 5):")
            st.write(errors)

    return df_all


# -------------------------------------------------------------------
# STREAMLIT APP
# -------------------------------------------------------------------

def main():
    st.set_page_config(page_title="CAMS → USPS Address Export", layout="wide")
    st.title("CAMS Address Selector (LA County) with USPS-cleaned export")

    st.markdown(
        """
        **Workflow (single step):**

        1. Pick a school from the dropdown in the left sidebar.  
        2. The map will zoom to that school and drop a marker.  
        3. Draw a polygon or rectangle around the area you care about.  
        4. Click **Run CAMS + USPS**.  

        The app will:

        - Query LA County CAMS for all address points inside your shape.  
        - Run USPS address cleaning on each result.  
        - Mark each row as VERIFIED or UNVERIFIED.  
        - Flag likely apartment buildings as "APARTMENT numbers".  
        - Show you the table and a CSV for download.
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
                f"Addresses returned: **{len(df_final)}** rows "
                "(CAMS data + USPS fields + status/note)."
            )
            st.dataframe(df_final)

            csv_bytes = df_final.to_csv(index=False).encode("utf-8")
            st.download_button(
                "Download address CSV",
                data=csv_bytes,
                file_name="cams_usps_addresses.csv",
                mime="text/csv",
            )
        else:
            st.write("No results yet. Draw an area and click **Run CAMS + USPS**.")


if __name__ == "__main__":
    main()
