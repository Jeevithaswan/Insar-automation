import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import streamlit as st

from orchestrator import status as status_mod
from orchestrator.config import RunConfig
from orchestrator.paths import RunPaths
from dashboard.dashboard_lib import job_manager, run_registry

st.set_page_config(page_title="Quality Checkpoints", layout="wide")
st.title("Quality Checkpoints")

runs = run_registry.list_runs()
pending = [r for r in runs if r["needs_review"]]

if not pending:
    st.info("No runs are currently waiting for review.")
    st.stop()

run_ids = [r["run_id"] for r in pending]
selected = st.selectbox("Run waiting for review", run_ids)
run_dir = Path(run_registry.DEFAULT_BASE_DIR) / selected
paths = RunPaths(run_dir)
s = status_mod.read_status(run_dir)
gate = s["needs_review"]

st.subheader(f"Run: {selected} -- Gate: {gate}")

if gate == "gate_a":
    cost_path = paths.review / "network_design_summary.json"
    if not cost_path.exists():
        st.error("No network_design_summary.json found -- stage may not have completed correctly.")
        st.stop()
    with open(cost_path) as f:
        cost = json.load(f)

    st.write(f"**{cost['num_scenes']} scenes found**, ~**{cost['approx_interferogram_pairs']} interferogram pairs** "
             f"at the configured connections setting.")
    st.write(f"Estimated pixel count after multilooking: ~{cost['approx_pixels_after_multilook']:,}")
    st.write(f"Estimated SNAPHU memory: ~{cost['approx_snaphu_mb_per_instance']:,} MB per instance, "
             f"~{cost['approx_snaphu_peak_mb']:,} MB peak at the configured parallelism "
             f"({cost['configured_run16_parallelism']}-way).")
    st.caption(cost["note"])

    config = RunConfig.from_yaml(str(paths.run_dir / "config_used.yaml"))
    st.write("Current network settings (editable before approving):")
    c1, c2, c3, c4 = st.columns(4)
    connections = c1.number_input("Connections", min_value=1, value=config.network.connections)
    az_looks = c2.number_input("Azimuth looks", min_value=1, value=config.network.azimuth_looks)
    range_looks = c3.number_input("Range looks", min_value=1, value=config.network.range_looks)
    run16_parallelism = c4.number_input(
        "Unwrap (SNAPHU) parallelism", min_value=1, value=config.network.run16_parallelism,
        help="Keep at 1 (sequential) unless you're confident this AOI's memory footprint is small -- "
             "4-way parallelism OOM'd SNAPHU on a prior AOI in this pipeline's own history.",
    )

    if st.button("Approve & Continue", type="primary"):
        overrides = {}
        if connections != config.network.connections:
            overrides["connections"] = connections
        if az_looks != config.network.azimuth_looks:
            overrides["azimuth_looks"] = az_looks
        if range_looks != config.network.range_looks:
            overrides["range_looks"] = range_looks
        if run16_parallelism != config.network.run16_parallelism:
            overrides["run16_parallelism"] = run16_parallelism
        pid = job_manager.approve_gate_a(selected, overrides or None)
        st.success(f"Approved. Resuming (PID {pid}). Go to Monitor to watch progress.")

elif gate == "gate_b":
    cand_path = paths.review / "candidate_reference_points.json"
    if not cand_path.exists():
        st.error("No candidate_reference_points.json found -- stage may not have completed correctly.")
        st.stop()
    with open(cand_path) as f:
        candidates = json.load(f)

    if not candidates:
        st.error(
            "No reference point candidates were found at all, even after widening the search radius "
            "and relaxing the coherence threshold. This AOI may have very poor overall coherence -- "
            "consider a different date range or a larger scene stack before proceeding."
        )
        st.stop()

    st.write(f"**{len(candidates)} candidate(s) found**, ranked by (distance + elevation-difference) score:")

    low_coherence_warning = candidates[0]["coherence"] < 0.5
    if low_coherence_warning:
        st.warning(
            f"Best candidate's coherence is only {candidates[0]['coherence']:.3f} (below the usual 0.5 "
            "threshold) -- this AOI's overall coherence may be poor. Proceeding is possible but treat "
            "results as lower-confidence."
        )

    table_rows = [
        {
            "rank": i + 1,
            "lat": round(c["lat"], 5),
            "lon": round(c["lon"], 5),
            "coherence": round(c["coherence"], 3),
            "elevation_m": round(c["elevation_m"], 1),
            "distance_km": round(c["distance_km"], 2),
        }
        for i, c in enumerate(candidates)
    ]
    st.dataframe(table_rows, use_container_width=True)

    try:
        import pandas as pd
        st.map(pd.DataFrame([{"lat": c["lat"], "lon": c["lon"]} for c in candidates]))
    except Exception:
        pass

    choice_mode = st.radio("Choose reference point", ["Use top-ranked candidate", "Pick a candidate by rank", "Enter manually"])
    if choice_mode == "Use top-ranked candidate":
        lat, lon = candidates[0]["lat"], candidates[0]["lon"]
    elif choice_mode == "Pick a candidate by rank":
        rank = st.number_input("Rank", min_value=1, max_value=len(candidates), value=1)
        lat, lon = candidates[rank - 1]["lat"], candidates[rank - 1]["lon"]
    else:
        c1, c2 = st.columns(2)
        lat = c1.number_input("Latitude", value=candidates[0]["lat"], format="%.6f")
        lon = c2.number_input("Longitude", value=candidates[0]["lon"], format="%.6f")

    st.write(f"Selected: **{lat}, {lon}**")
    if st.button("Approve & Continue", type="primary"):
        pid = job_manager.approve_gate_b(selected, lat, lon)
        st.success(f"Approved. Resuming (PID {pid}). Go to Monitor to watch progress.")
