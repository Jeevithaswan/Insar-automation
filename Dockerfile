# Generic InSAR pipeline, packaged so anyone can run it with one command
# instead of the multi-day manual environment setup this project originally
# needed (conda install, ISCE2 build, the two NumPy patches, etc).
#
# Build:  docker build -t insar-pipeline .
# Run:    docker compose up
#
FROM condaforge/miniforge3:latest

# The pipeline code hardcodes paths under ~/insar-automation (matches the
# WSL development setup) -- as root inside the container, that's
# /root/insar-automation. Keeping this path identical means none of the
# orchestrator/dashboard code needs to change to run in Docker.
ENV INSAR_HOME=/root/insar-automation
WORKDIR ${INSAR_HOME}

# ---- 1. Build the exact conda environment already verified working ----
COPY environment.yml /tmp/environment.yml
RUN mamba env create -f /tmp/environment.yml && mamba clean -afy

# ---- 2. Copy the pipeline code itself ----
COPY orchestrator/ ${INSAR_HOME}/orchestrator/
COPY dashboard/ ${INSAR_HOME}/dashboard/
COPY patches/ ${INSAR_HOME}/patches/
COPY configs/ ${INSAR_HOME}/configs/
COPY environment/ ${INSAR_HOME}/environment/
COPY AUTOMATION_STEPS_REFERENCE.txt ${INSAR_HOME}/

# ---- 3. Apply the two NumPy/ISCE2 compatibility patches at BUILD time ----
# In WSL these get re-verified on every run because the conda env could be
# rebuilt outside this project's control. In a Docker image, the env is
# baked in and immutable once built, so applying once here is sufficient --
# still re-verified by env_precheck at runtime too, as a safety net.
RUN /opt/conda/envs/insar/bin/python ${INSAR_HOME}/patches/apply_patches.py

# ---- 4. Environment variables ISCE2/topsStack need on PATH ----
ENV CONDA_DEFAULT_ENV=insar
ENV ISCE_HOME=/opt/conda/envs/insar/lib/python3.12/site-packages/isce
ENV PATH=${ISCE_HOME}/applications:${ISCE_HOME}/bin:/opt/conda/envs/insar/share/isce2/topsStack:/opt/conda/envs/insar/share/isce2/stripmapStack:/opt/conda/envs/insar/bin:${PATH}
ENV PYTHONPATH=${ISCE_HOME}/applications:${ISCE_HOME}/bin:/opt/conda/envs/insar/share/isce2:${INSAR_HOME}

# ---- 5. Run data lives in a mounted volume, not baked into the image ----
# (large per-run satellite data doesn't belong inside the image itself)
RUN mkdir -p ${INSAR_HOME}/runs

EXPOSE 8501

# Default: launch the dashboard. Override the command to run the CLI
# directly instead, e.g.:
#   docker compose run insar python -m orchestrator.cli run --config ...
CMD ["/opt/conda/envs/insar/bin/streamlit", "run", "/root/insar-automation/dashboard/app.py", \
     "--server.headless=true", "--server.port=8501", "--server.address=0.0.0.0"]
