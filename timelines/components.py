import datetime

import streamlit as st
from streamlit import caching
import json
import os
from .critical_path import render_critical_path

from .processes import render_processes
from .resources import render_resources
from .roles import render_roles
from .resource_usage_analysis import render_resource_usage
from .utils import flush_state, Cache, get_dates_of_prediction_change, strip_time
from .graph import display_graph
from .time_changes import render_timeline_changes
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

    if not save_file.endswith('.json'):
        raise ValueError("Save file must end with '.json'")

    file = st.sidebar.file_uploader("Upload data file", type=['json'], accept_multiple_files=False)

    if st.sidebar.button("Load file") and (file is not None):
        data = json.load(file)
        flush_state(save_file, data)
        st.sidebar.info(f"Loaded {file} into {save_file}. Previous contents of {save_file} are deleted.")


    if os.path.isfile(save_file):
        with open(save_file, 'r') as f:
            data = json.load(f)
            data['start_date'] = data['start_date'] if 'start_date' in data else datetime.datetime.now().isoformat()
    else:
        data = dict(cache_hash=0,
                    start_date=datetime.datetime.now().isoformat(),
                    roles=[],
                    resources=dict(),
                    processes=dict())
        flush_state(save_file, data)

    st.sidebar.markdown(get_table_download_link(save_file), unsafe_allow_html=True)

    advanced = st.sidebar.checkbox("Advanced options", False, help="Whether to enable advanced options.")

    if st.sidebar.button("Refresh"):
        caching.clear_cache()


    st.session_state['start_date'] = datetime.datetime.fromisoformat(data['start_date'])
    def _on_change():
        data['start_date'] = strip_time(st.session_state['start_date']).isoformat()
        flush_state(save_file, data)

    start_date = st.sidebar.date_input("Start date: ",
                                       help='Date where process graph starts.',
                                       key='start_date',
                                       on_change=_on_change)

    if start_date is not None:
        data['start_date'] = start_date.isoformat()

    container = st.container()


    ### display sections

    st.header("Visualise")

    dates_of_change = get_dates_of_prediction_change(Cache(data))
    if len(dates_of_change) > 1:
        date_of_change = st.select_slider("Date of prediction: ", dates_of_change,
                                          format_func=lambda date: date.isoformat(),
                                          help="Choose the historical date to explore past predictions, from past updates.")
    else:
        date_of_change = datetime.datetime.fromisoformat(data['start_date'])

    display_graph(data, date_of_change)

    render_critical_path(data, date_of_change)

    render_timeline_changes(Cache(data), dates_of_change)

    render_resource_usage(data, date_of_change)

    ## data
    with container:
        if advanced:
            render_roles(data, save_file, advanced)
            render_resources(data, save_file, advanced)
        render_processes(data, save_file, advanced, date_of_change)



