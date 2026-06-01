#!/usr/bin/env bash
set -euo pipefail

export PYTHONPATH="${PWD}/src:${PYTHONPATH:-}"
export PROJDASH_STORAGE="${PROJDASH_STORAGE:-sqlite}"
export PROJDASH_DB_PATH="${PROJDASH_DB_PATH:-projdash.sqlite}"

if command -v python >/dev/null 2>&1; then
  PYTHON_CMD=(python)
  STREAMLIT_CMD=(streamlit)
else
  PYTHON_CMD=(conda run -n projdash_py python)
  STREAMLIT_CMD=(conda run -n projdash_py streamlit)
fi

"${PYTHON_CMD[@]}" -m projdash.service.bootstrap \
  --storage "${PROJDASH_STORAGE}" \
  --db "${PROJDASH_DB_PATH}"
"${STREAMLIT_CMD[@]}" run app.py
