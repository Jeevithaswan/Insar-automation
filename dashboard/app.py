"""Dashboard entrypoint / landing page. Actual pages live in pages/ and
appear automatically in Streamlit's sidebar nav."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as st

from dashboard.dashboard_lib.run_registry import list_runs

st.set_page_config(page_title="InSAR Pipeline", layout="wide")

st.title("Generic InSAR Processing Pipeline")
st.caption("ASF download -> ISCE2 (topsStack) -> MintPy -> deliverables, for any AOI.")

st.markdown(
    """
Use the pages in the sidebar:

1. **Configure Analysis** -- define an AOI, date range, search available tracks, and start a run.
2. **Processing Status** -- watch progress and logs for an in-progress run.
3. **Quality Checkpoints** -- approve network design parameters and the reference point
   before the pipeline continues past each checkpoint. Every run stops here twice --
   nothing is silently auto-decided.
4. **Results Export** -- view and download figures/exports/KMZ once a run finishes.
5. **Credentials Configuration** -- set up ASF/Earthdata and CDS/ERA5 credentials, run environment diagnostics.
"""
)

runs = list_runs()
if runs:
    st.subheader("Existing runs")
    needing_review = [r for r in runs if r["needs_review"]]
    if needing_review:
        st.warning(f"{len(needing_review)} run(s) waiting for review -- see the Quality Checkpoints page.")
    st.dataframe(runs, use_container_width=True)
else:
    st.info("No runs yet -- start one from the Configure Analysis page.")
