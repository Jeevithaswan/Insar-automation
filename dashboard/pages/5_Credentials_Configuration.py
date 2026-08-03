import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import streamlit as st

from orchestrator import env as env_checks

st.set_page_config(page_title="Credentials Configuration", layout="wide")
st.title("Credentials Configuration")
st.caption("Entered once here, used for any subsequent run regardless of region.")

st.subheader("ASF / Earthdata credentials (~/.netrc)")
st.caption("Required for downloading Sentinel-1 scenes from ASF.")
netrc_path = Path.home() / ".netrc"
if netrc_path.exists():
    st.success("~/.netrc is present.")
else:
    st.warning("~/.netrc is missing.")

with st.form("netrc_form"):
    username = st.text_input("Earthdata username")
    password = st.text_input("Earthdata password", type="password")
    if st.form_submit_button("Save"):
        if username and password:
            content = (
                f"machine urs.earthdata.nasa.gov\n"
                f"login {username}\n"
                f"password {password}\n"
            )
            netrc_path.write_text(content)
            os.chmod(netrc_path, 0o600)
            st.success("Saved ~/.netrc. Note: stored as plaintext on disk, which is standard for "
                       "how curl/wget/requests-based tools authenticate to Earthdata -- no additional "
                       "OS-keychain hardening is applied.")
            st.rerun()
        else:
            st.error("Both fields are required")

st.divider()

st.subheader("CDS / ERA5 credentials (~/.cdsapirc)")
st.caption("Only needed if a run has tropospheric_delay.enabled = true.")
cdsapirc_path = Path.home() / ".cdsapirc"
if cdsapirc_path.exists():
    st.success("~/.cdsapirc is present.")
else:
    st.info("~/.cdsapirc is missing -- fine unless you enable tropospheric correction.")

with st.form("cdsapirc_form"):
    token = st.text_input("CDS personal access token", type="password",
                           help="From https://cds.climate.copernicus.eu/ profile page. "
                                "You must also accept the ERA5 dataset licenses on that site once.")
    if st.form_submit_button("Save"):
        if token:
            content = f"url: https://cds.climate.copernicus.eu/api\nkey: {token}\n"
            cdsapirc_path.write_text(content)
            os.chmod(cdsapirc_path, 0o600)
            st.success("Saved ~/.cdsapirc.")
            st.rerun()
        else:
            st.error("Token is required")

st.divider()

st.subheader("Environment diagnostics")
if st.button("Run diagnostics"):
    result = env_checks.run_all_checks(require_credentials=False)
    for c in result["checks"]:
        if c["ok"]:
            st.success(f"{c['check']}: {c['detail']}")
        else:
            st.error(f"{c['check']}: {c['detail']}")
