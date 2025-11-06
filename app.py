import json
import requests
import pandas as pd
import streamlit as st

import folium
from folium.plugins import Draw
from streamlit_folium import st_folium

# LA County CAMS Address Points feature service
CAMS_URL = (
    "https://arcgis.gis.lacounty.gov/arcgis/rest/services/DRP/"
    "GISNET_Public/MapServer/402/query"
)


def build_esri_polygon_from_geojson(geojson_geom: dict) -> dict:
    """
    Convert a GeoJSON Polygon geometry into ArcGIS polygon JSON.
    Expects geometry like:
      {"type": "Polygon", "coordinates": [[[lon, lat], [lon, lat], ...]]}
    """
    if geojson_geom.get("type") != "Polygon":
        raise ValueError("Expected a Polygon geometry")

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


def main():
    st.set_page_config(page_title="CAMS Address Selector", layout="wide")
    st.title("CAMS Address Selector (LA County)")
    st.markdown(
        """
        This app lets you:
        1. Draw an area in Los Angeles County.
        2. Query the LA County CAMS service.
        3. Download all address points inside the drawn area as CSV.
        """
    )

    # Create base map centered on LA
    m = folium.Map(location=[34.0522, -118.2437], zoom_start=10)

    # Add drawing controls (polygon + rectangle)
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

    st.markdown("### Map (draw a polygon or rectangle)")
    map_data = st_folium(
        m,
        width=900,
        height=600,
        returned_objects=["last_active_drawing"],
    )

    col1, col2 = st.columns([1, 2])

    with col1:
        st.markdown("### Drawn geometry")
        last = map_data.get("last_active_drawing")
        st.json(last)

        run_query = st.button("Run query on drawn area")

    df = pd.DataFrame()

    if run_query:
        last = map_data.get("last_active_drawing")
        if not last or "geometry" not in last:
            st.error("No polygon detected. Please draw a polygon or rectangle first.")
        else:
            try:
                geojson_geom = last["geometry"]
                esri_polygon = build_esri_polygon_from_geojson(geojson_geom)
                with st.spinner("Querying CAMS service..."):
                    df = query_cams_addresses(esri_polygon)
            except Exception as e:
                st.error(f"Error while querying CAMS: {e}")

    with col2:
        st.markdown("### Results")
        if not df.empty:
            st.write(f"Found **{len(df)}** address points inside the drawn area.")
            st.dataframe(df)

            csv_bytes = df.to_csv(index=False).encode("utf-8")
            st.download_button(
                "Download results as CSV",
                data=csv_bytes,
                file_name="cams_addresses_in_polygon.csv",
                mime="text/csv",
            )
        else:
            st.write("No results yet. Draw an area and click **Run query**.")


if __name__ == "__main__":
    main()
