#!/bin/bash
# Single source of truth for environment activation, sourced by every
# stage script and by the dashboard launcher. Must be sourced (not just
# `conda activate`) for topsStack scripts to be on PATH.
source "$HOME/miniconda3/etc/profile.d/conda.sh"
conda activate insar

export ISCE_HOME="$CONDA_PREFIX/lib/python3.12/site-packages/isce"
export PATH="$ISCE_HOME/applications:$ISCE_HOME/bin:$CONDA_PREFIX/share/isce2/topsStack:$CONDA_PREFIX/share/isce2/stripmapStack:$PATH"
export PYTHONPATH="$ISCE_HOME/applications:$ISCE_HOME/bin:$CONDA_PREFIX/share/isce2:$PYTHONPATH"

export INSAR_AUTOMATION_HOME="$HOME/insar-automation"
export PYTHONPATH="$INSAR_AUTOMATION_HOME:$PYTHONPATH"
