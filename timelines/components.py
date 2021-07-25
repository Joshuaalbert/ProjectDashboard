import streamlit as st
from streamlit import caching
import json
import os
from .critical_path import render_critical_path

from .processes import render_processes
from .resources import render_resources
from .roles import render_roles
from .subgraphs import render_process_subgraph
from .utils import flush_state

import base64


def get_table_download_link(save_file):
    """Generates a link allowing the data in a given panda dataframe to be downloaded
    in:  dataframe
    out: href string
    """
    with open(save_file, 'r') as f:
        val = "\n".join(f.readlines())

    b64 = base64.b64encode(val.encode())  # .decode()
    # b64 = base64.b64encode(val)  # val looks like b'...'
    return f'<a href="data:application/octet-stream;base64,{b64.decode()}" download="{save_file}">Download State File</a>'  # decode b'abc' => abc


def render_components():
    save_file = st.sidebar.text_input("State file: ", 'project.json', help="JSON file to store information in.")

    file = st.sidebar.file_uploader("Upload data file", type=['json'], accept_multiple_files=False)
    if file is not None:
        data = json.load(file)
        flush_state(save_file, data)


    if os.path.isfile(save_file):
        with open(save_file, 'r') as f:
            data = json.load(f)
    else:
        data = dict(cache_hash=0,
                    roles=[],
                    resources=dict(),
                    processes=dict(),
                    subgraphs=dict())
        flush_state(save_file, data)

    st.sidebar.markdown(get_table_download_link(save_file), unsafe_allow_html=True)

    if st.sidebar.button("Refresh"):
        caching.clear_cache()

    render_roles(data, save_file)

    render_resources(data, save_file)

    render_processes(data, save_file)

    render_process_subgraph(data, save_file)

    render_critical_path(data)



