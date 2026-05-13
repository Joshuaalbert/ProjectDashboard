#!/usr/bin/env bash
set -euo pipefail

export PYTHONPATH="${PWD}/src:${PYTHONPATH:-}"
export PROJDASH_DB_PATH="${PROJDASH_DB_PATH:-projdash.lbug}"

python -m projdash.service.bootstrap --db "${PROJDASH_DB_PATH}"
streamlit run app.py
