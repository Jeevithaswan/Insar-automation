# InSAR SBAS automation pipeline, packaged so it runs on any machine without
# repeating the environment setup this project needed the hard way: two
# conda installs conflicting with each other, ISCE2 not on PATH/PYTHONPATH,
# a DEM compression bug that stalled processing for hours, and five separate
# MintPy edge-case bugs -- all found, fixed, and now baked into this image
# instead of needing rediscovery on every new machine.
#
# Build:  docker build -t insar-pipeline .
# Run:    docker run --rm \
#           -v ~/.netrc:/root/.netrc:ro \
#           -v ~/.cdsapirc:/root/.cdsapirc:ro \
#           -v $(pwd)/runs:/root/insar-automation/runs \
#           -v $(pwd)/configs:/root/insar-automation/configs \
#           insar-pipeline 0
#
# (or see docker-compose.yml for the same thing with less typing)

FROM condaforge/miniforge3:latest

# Configs reference "~/insar-automation/runs" as their output base_dir --
# matching that exact path here (running as root -> ~ = /root) means the
# config files don't need to change between host and container.
ENV INSAR_HOME=/root/insar-automation
WORKDIR ${INSAR_HOME}

# ---- 1. Build the exact conda environment verified working (2026-08-04) ----
COPY environment.yml /tmp/environment.yml
RUN mamba env create -f /tmp/environment.yml && mamba clean -afy

# ---- 2. Environment variables ISCE2/topsStack need on PATH/PYTHONPATH ----
# pipeline.py's own _extend_path_for_isce2() does this too at import time,
# but setting it at the image level as well means every tool (not just
# pipeline.py's own subprocess calls) sees a correctly configured PATH.
ENV CONDA_DEFAULT_ENV=insar
ENV CONDA_PREFIX=/opt/conda/envs/insar
ENV PATH=${CONDA_PREFIX}/share/isce2/topsStack:${CONDA_PREFIX}/lib/python3.12/site-packages/isce/applications:${CONDA_PREFIX}/bin:${PATH}
ENV PYTHONPATH=${CONDA_PREFIX}/share/isce2

# PROJ_DATA/GDAL_DATA normally get set by conda's own activate.d hooks (run
# automatically by `conda activate`) -- but the ENTRYPOINT below calls the
# env's python binary directly, without ever activating, so those hooks
# never run. Without PROJ_DATA, GDAL prints "PROJ: proj_create_from_database:
# Open of .../share/proj failed" during ISCE2's topo step -- confirmed by
# direct testing inside a running container (2026-08-04).
#
# Deliberately NOT setting GDAL_DRIVER_PATH, unlike conda's own
# gdal-activate.sh: that directory only holds a handful of optional format
# plugins (netCDF, HDF5, GRIB, ...), not GDAL's core drivers (GTiff, MEM,
# etc, which are compiled into libgdal itself). Setting it anyway broke
# rasterio's MemoryFile.open() with "TypeError: 'NoneType' object is not
# callable" -- also confirmed by direct testing, isolated by toggling this
# one variable. Core driver resolution needs GDAL_DRIVER_PATH left unset.
ENV GDAL_DATA=${CONDA_PREFIX}/share/gdal
ENV PROJ_DATA=${CONDA_PREFIX}/share/proj

# ---- 3. Copy the pipeline itself ----
COPY pipeline.py ${INSAR_HOME}/
COPY configs/ ${INSAR_HOME}/configs/

# ---- 4. Apply the known ISCE2/MintPy library patches at BUILD time ----
# Patches are idempotent and also self-apply the first time stage 0 runs,
# but baking them in now means the image is correct from the first run,
# not just after remembering to run `pipeline.py 0` first.
RUN ${CONDA_PREFIX}/bin/python -c "import sys; sys.path.insert(0, '${INSAR_HOME}'); import pipeline; pipeline._check_and_apply_library_patches()"

# ---- 5. Run data lives in a mounted volume, not baked into the image ----
# (per-run satellite data is many GB -- doesn't belong inside the image)
RUN mkdir -p ${INSAR_HOME}/runs

ENTRYPOINT ["/opt/conda/envs/insar/bin/python", "pipeline.py"]
CMD ["0"]
