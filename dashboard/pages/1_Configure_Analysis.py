import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import streamlit as st

from orchestrator.config import RunConfig, AOI, DateRange, Sentinel1, NetworkConfig, MintPyConfig
from orchestrator.stages.s1_search_download import search_scenes, summarize_tracks
from dashboard.dashboard_lib import job_manager

st.set_page_config(page_title="Configure Analysis", layout="wide")
st.title("Configure Analysis")
st.caption("No defaults here are pre-filled toward any particular region -- fill in your own AOI.")

if "track_summary" not in st.session_state:
    st.session_state.track_summary = None
if "search_bbox" not in st.session_state:
    st.session_state.search_bbox = None

with st.form("aoi_form"):
    st.subheader("Area of interest")
    c1, c2, c3, c4 = st.columns(4)
    lon_min = c1.number_input("West (lon min)", value=0.0, format="%.4f")
    lat_min = c2.number_input("South (lat min)", value=0.0, format="%.4f")
    lon_max = c3.number_input("East (lon max)", value=0.0, format="%.4f")
    lat_max = c4.number_input("North (lat max)", value=0.0, format="%.4f")

    st.subheader("Date range")
    d1, d2 = st.columns(2)
    start_date = d1.text_input("Start (YYYY-MM-DD)", value="")
    end_date = d2.text_input("End (YYYY-MM-DD)", value="")

    polarization = st.selectbox("Polarization", ["VV", "VV+VH", "HH", "HH+HV"], index=0)

    search_submitted = st.form_submit_button("Search tracks")

if search_submitted:
    if lon_min >= lon_max or lat_min >= lat_max:
        st.error("bbox must have west < east and south < north")
    elif not start_date or not end_date:
        st.error("Start and end date are required")
    else:
        with st.spinner("Searching ASF (no download)..."):
            try:
                bbox = [lon_min, lat_min, lon_max, lat_max]
                scenes = search_scenes(bbox, start_date, end_date)
                st.session_state.track_summary = summarize_tracks(scenes)
                st.session_state.search_bbox = bbox
                st.session_state.search_dates = (start_date, end_date)
                st.session_state.search_polarization = polarization
            except Exception as e:
                st.error(f"Search failed: {e}")

if st.session_state.track_summary:
    st.subheader("Available tracks for this AOI")
    if not st.session_state.track_summary:
        st.warning("No Sentinel-1 scenes found for this AOI/date range.")
    else:
        for i, t in enumerate(st.session_state.track_summary):
            st.write(
                f"**Track {i+1}:** relative orbit {t['relative_orbit']}, "
                f"{t['flight_direction']}, {t['scene_count']} scenes, "
                f"{t['date_range'][0]} to {t['date_range'][1]}"
            )

        options = [f"orbit {t['relative_orbit']} ({t['flight_direction']}, {t['scene_count']} scenes)"
                   for t in st.session_state.track_summary]
        chosen_idx = st.selectbox("Choose a track for this run", range(len(options)), format_func=lambda i: options[i])
        chosen = st.session_state.track_summary[chosen_idx]

        st.subheader("Network design (defaults -- reviewable again at Gate A before the expensive stage runs)")
        n1, n2, n3 = st.columns(3)
        connections = n1.number_input("Connections", min_value=1, value=3)
        az_looks = n2.number_input("Azimuth looks", min_value=1, value=7)
        range_looks = n3.number_input("Range looks", min_value=1, value=19)

        run_id = st.text_input("Run ID (used as the folder name under runs/)", value="")

        if st.button("Start Run", type="primary"):
            if not run_id:
                st.error("Run ID is required")
            else:
                config = RunConfig(
                    run_id=run_id,
                    aoi=AOI(bbox=st.session_state.search_bbox),
                    date_range=DateRange(start=st.session_state.search_dates[0], end=st.session_state.search_dates[1]),
                    sentinel1=Sentinel1(
                        relative_orbit=chosen["relative_orbit"],
                        flight_direction=chosen["flight_direction"],
                        polarization=st.session_state.search_polarization,
                    ),
                    network=NetworkConfig(connections=connections, azimuth_looks=az_looks, range_looks=range_looks),
                    mintpy=MintPyConfig(),
                )
                config_path = Path.home() / "insar-automation" / "configs" / "runs" / f"{run_id}.yaml"
                config.to_yaml(str(config_path))
                pid = job_manager.launch_run(str(config_path))
                st.success(f"Run '{run_id}' started (PID {pid}). Go to the Processing Status page to watch progress.")
