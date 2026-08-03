import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import streamlit as st

from orchestrator.paths import RunPaths
from dashboard.dashboard_lib import run_registry

st.set_page_config(page_title="Results Export", layout="wide")
st.title("Results Export")

runs = run_registry.list_runs()
done_runs = [r for r in runs if r["current_stage"] == "deliverables"]

if not done_runs:
    st.info("No runs have reached the deliverables stage yet.")
    st.stop()

run_ids = [r["run_id"] for r in done_runs]
selected = st.selectbox("Run", run_ids)
run_dir = Path(run_registry.DEFAULT_BASE_DIR) / selected
paths = RunPaths(run_dir)

summary_path = paths.deliverables / "summary.json"
if not summary_path.exists():
    st.warning("This run reached the deliverables stage but summary.json wasn't found -- it may still be running.")
    st.stop()

with open(summary_path) as f:
    summary = json.load(f)

c1, c2, c3 = st.columns(3)
c1.metric("Reliable pixels", f"{summary['reliable_pixels']:,} ({summary['reliable_pct']}%)")
c2.metric("Velocity min (mm/yr)", f"{summary['velocity_min_mmyr']:.1f}" if summary["velocity_min_mmyr"] is not None else "-")
c3.metric("Velocity max (mm/yr)", f"{summary['velocity_max_mmyr']:.1f}" if summary["velocity_max_mmyr"] is not None else "-")

st.subheader("Figures")
for png_name in ["velocity_map.png", "timeseries_by_date.png", "aoi_center_timeseries.png"]:
    png_path = paths.deliverables / png_name
    if png_path.exists():
        st.image(str(png_path), caption=png_name)

st.subheader("Downloads")
for fname in summary.get("files", []):
    fpath = paths.deliverables / fname
    if fpath.exists():
        with open(fpath, "rb") as f:
            st.download_button(f"Download {fname}", f.read(), file_name=fname)

st.subheader("Run configuration used")
config_path = run_dir / "config_used.yaml"
if config_path.exists():
    st.code(config_path.read_text(), language="yaml")
