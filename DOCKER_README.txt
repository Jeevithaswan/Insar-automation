====================================================================
RUNNING THE INSAR PIPELINE VIA DOCKER
====================================================================
This packages the entire environment (conda, ISCE2, MintPy, the two
NumPy patches, all Python dependencies) plus the pipeline code and
dashboard into one image -- so someone else can run this without
the multi-day manual setup this project originally needed.

ONE-TIME SETUP
--------------------------------------------------------------------
1. Install Docker Desktop (Windows/Mac) or Docker Engine (Linux).
2. From this folder, create empty credential files (or fill them in
   with real values before first run):
     mkdir -p docker_runs docker_credentials
     touch docker_credentials/.netrc docker_credentials/.cdsapirc

BUILD AND RUN
--------------------------------------------------------------------
   docker compose up --build

Then open a browser to:
   http://localhost:8501

Same dashboard, same pipeline, same behavior as the WSL version --
just running inside a container instead.

ENTERING CREDENTIALS
--------------------------------------------------------------------
Same as the WSL version: use the "Credentials Configuration" page in
the dashboard to enter your ASF/Earthdata login and (optionally) your
CDS/ERA5 token. Because docker_credentials/ is mounted into the
container at the exact spot ISCE2/asf_search/cdsapi expect
(/root/.netrc, /root/.cdsapirc), these persist even if the container
is rebuilt or removed.

WHERE RESULTS GO
--------------------------------------------------------------------
Everything under docker_runs/ on your host machine -- one folder per
run, same structure as the WSL version's runs/ folder. Safe to browse,
back up, or delete individual runs directly from the host.

RUNNING THE CLI DIRECTLY INSTEAD OF THE DASHBOARD
--------------------------------------------------------------------
   docker compose run insar python -m orchestrator.cli run --config configs/runs/<name>.yaml
   docker compose run insar python -m orchestrator.cli status --run-id <name>

STOPPING
--------------------------------------------------------------------
   docker compose down
(run data in docker_runs/ and credentials in docker_credentials/ are
untouched -- only the running container stops)

KNOWN LIMITATION (as of this build)
--------------------------------------------------------------------
ISCE2's per-scene topography step has been observed to take a long
time on the development machine regardless of AOI size (tens of
minutes per burst) -- this is a property of ISCE2 itself / the host
machine's hardware, not something Docker changes. Expect real runs to
take a similar amount of wall-clock time inside Docker as they did
running directly in WSL.
