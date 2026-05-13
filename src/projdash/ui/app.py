"""Streamlit entrypoint for the new service-backed UI."""

from __future__ import annotations

import os

import streamlit as st


def main() -> None:
    """Render the first service-backed UI shell."""
    st.set_page_config(page_title="ProjectDashboard", layout="wide")
    st.title("ProjectDashboard")
    st.caption("Service-backed rewrite in progress.")

    db_path = os.environ.get("PROJDASH_DB_PATH", "projdash.lbug")
    st.sidebar.text_input("Service database", db_path, disabled=True)

    st.info(
        "The legacy Streamlit implementation is preserved under `old_code/`. "
        "The new UI will call the validated service API instead of mutating "
        "project state directly."
    )
