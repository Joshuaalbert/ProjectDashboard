import datetime

import streamlit as st

from .utils import flush_state


def render_resources(data, save_file, advanced):
    ###
    # resources
    with st.sidebar.expander("Resources"):
        new_resource = st.text_input("Resource name: ", help="Add a new resource.")

        if new_resource in data['resources']:
            _default_wage = data['resources'][new_resource]['cost']
            _default_roles = data['resources'][new_resource]['roles']
            _default_start_date = datetime.datetime.fromisoformat(data['resources'][new_resource]['start_date'])
            _default_cost_per_week = data['resources'][new_resource]['cost_per_week']
        else:
            _default_wage = 0.
            _default_roles = []
            _default_start_date = datetime.datetime.now()
            _default_cost_per_week = False

        cost_per_week = st.checkbox("Fixed resource cost ($/week)?", _default_cost_per_week)
        if cost_per_week:
            new_resource_wage = st.slider("Resource cost ($ / week): ", 0., 5000., _default_wage, 50.,
                                          help="What is the resource cost per week.")
        else:
            new_resource_wage = st.slider("Resource cost ($ / hour): ", 0., 200., _default_wage, 0.5,
                                          help="What is the resource cost in ($ / hour).")

        new_resource_roles = st.multiselect("Resource roles:", data['roles'], _default_roles,
                                            help="What roles this resource can fill.")

        new_resource_start_date = st.date_input("Start date: ", _default_start_date,
                                                help="From what date the resource can work.")

        if st.button("Add/Mod resource") and (new_resource != ""):
            data['resources'][new_resource] = dict(roles=new_resource_roles,
                                                   start_date=new_resource_start_date.isoformat(),
                                                   cost=new_resource_wage,
                                                   cost_per_week=cost_per_week)
            flush_state(save_file, data)

        delete_resources = st.multiselect("Delete resources: ", list(data['resources']))
        if st.button("Delete resources") and len(delete_resources) > 0:
            for resource in delete_resources:
                del data['resources'][resource]
            flush_state(save_file, data)

    with st.expander("Resources"):
        for resource in data['resources']:
            st.markdown(f" - {resource} -> {', '.join(data['resources'][resource]['roles'])}")
