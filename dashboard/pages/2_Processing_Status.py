import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import streamlit as st

from orchestrator import status as status_mod
from orchestrator.paths import RunPaths
from dashboard.dashboard_lib import job_manager, log_tail, run_registry

st.set_page_config(page_title="Processing Status", layout="wide")
st.title("Processing Status")

runs = run_registry.list_runs()
if not runs:
    st.info("No runs yet -- start one from the Configure Analysis page.")
    st.stop()

run_ids = [r["run_id"] for r in runs]
selected = st.selectbox("Run", run_ids, key="monitor_run_select")
run_dir = Path(run_registry.DEFAULT_BASE_DIR) / selected
paths = RunPaths(run_dir)


@st.fragment(run_every="5s")
def monitor_fragment():
    s = status_mod.read_status(run_dir)
    alive = job_manager.is_alive(s.get("pid"))

    col1, col2, col3 = st.columns(3)
    col1.metric("Current stage", s.get("current_stage") or "-")
    col2.metric("Process", "running" if alive else ("crashed?" if s.get("pid") and not s.get("error") else "not running"))
    col3.metric("Needs review", s.get("needs_review") or "no")

    if s.get("error"):
        st.error(f"Last error: {s['error']}")
    if s.get("pid") and not alive and s.get("needs_review") is None and not s.get("error"):
        st.warning("Process is not running but no review gate is pending and no error was recorded -- "
                   "it may have finished, or been killed unexpectedly. Check the log below.")
    if s.get("needs_review"):
        st.info(f"Waiting for approval at **{s['needs_review']}** -- go to the Quality Checkpoints page.")

    progress = s.get("progress")
    if progress and progress.get("total", 0) > 0:
        st.subheader("Current step progress")
        fraction = min(1.0, progress["current"] / progress["total"])
        st.progress(fraction, text=f"{progress['label']}  ({progress['current']} of {progress['total']})")

    st.subheader("Stage status")
    stage_rows = [
        {"Stage": status_mod.STAGE_LABELS.get(stage, stage), "Status": s["stage_status"].get(stage, "pending")}
        for stage in status_mod.STAGES
    ]
    st.table(stage_rows)

    st.subheader("Log (last ~20KB)")
    log_path = paths.logs / "orchestrator.log"
    st.code(log_tail.tail(log_path), language=None)

    if alive:
        if st.button("Stop run", type="secondary"):
            job_manager.stop_run(s.get("pid"), s.get("pgid"))
            st.warning("Stop signal sent. The run is resumable -- start it again from where it left off "
                       "via the Configure Analysis page's same run ID, or the Quality Checkpoints page if it was mid-gate.")


monitor_fragment()
