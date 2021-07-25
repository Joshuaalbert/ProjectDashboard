import streamlit as st

from .utils import flush_state


def render_roles(data, save_file):
    ###
    # roles
    with st.sidebar.beta_expander("Roles"):
        new_role = st.text_input("New role: ", help="Add a new role type.")
        if st.button("Add role") and (new_role != ""):
            if new_role not in data['roles']:
                data['roles'].append(new_role)
                flush_state(save_file, data)
        delete_roles = st.multiselect("Delete roles: ", data['roles'])
        if st.button("Delete roles") and len(delete_roles) > 0:
            for role in delete_roles:
                idx = data['roles'].index(role)
                del data['roles'][idx]
                for resource in data['resources']:
                    if role in data['resources'][resource]['roles']:
                        idx = data['resources'][resource]['roles'].index(role)
                        del data['resources'][resource]['roles'][idx]
                for process in data['processes']:
                    if role in data['processes'][process]['roles']:
                        idx = data['processes'][process]['roles'].index(role)
                        del data['processes'][process]['roles'][idx]
                        del data['processes'][process]['commitment'][role]

            flush_state(save_file, data)
    with st.beta_expander("Roles"):
        for role in data['roles']:
            st.markdown(f" - {role}")