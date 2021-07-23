import datetime
import re
import networkx as nx
import streamlit as st

from .utils import flush_state, fill_graph, symbolify, next_business_day, strip_time


def symbolify_process_name(data, new_process_name):
    new_process = symbolify(new_process_name)
    # Get unique symbol
    if new_process in data['processes']:
        _new_process = new_process
        i = 1
        while _new_process in data['processes']:
            _new_process = new_process + str(i)
            i += 1
        new_process = _new_process
    return new_process


def render_processes(data, save_file):
    ###
    # processes
    with st.sidebar.beta_expander("Processes"):
        rollouts = []
        for subgraph in data['subgraphs']:
            if re.match("SG-(.+?)-RO", subgraph) is not None:
                rollouts.append(subgraph)

        new_process_name = st.text_input("Process name: ",
                                                 help="Add a new process, can be symbol or name. If symbol then modifies that process. If name then makes a new symbol for that name.")

        if new_process_name in data['processes']:
            # Name was symbol, so swap
            new_process = new_process_name
            new_process_name = data['processes'][new_process]['name']
        else:
            new_process = symbolify_process_name(data, new_process_name)


        # Dependencies
        dep_options = list(data['processes']) + rollouts
        if new_process in data['processes']:
            _default_dependencies = data['processes'][new_process]['dependencies']
            G = nx.DiGraph()
            fill_graph(G, data)
            descendants = nx.algorithms.dag.descendants(G, new_process)
            dep_options = sorted(list(set(dep_options) - descendants - {new_process}))
        else:
            _default_dependencies = []

        new_process_dependencies = st.multiselect("Dependencies:", dep_options, _default_dependencies,
                                                  help="What are the dependencies of this process.")

        # Duration
        duration_in_weeks = st.checkbox("Duration in weeks", True,
                                        help="Whether duration of process is in business weeks.")

        if new_process in data['processes']:
            _default_duration = data['processes'][new_process]['duration']
        else:
            _default_duration = 0
        if duration_in_weeks:
            _default_duration = _default_duration // 5
            new_process_duration = st.slider("Duration: ", 0, 52, _default_duration, step=1,
                                                     help="Duration of the process in weeks.")
        else:
            new_process_duration = st.slider("Duration: ", 0, 20, _default_duration, step=1,
                                             help="Duration of the process in business days.")

        if duration_in_weeks:
            new_process_duration *= 5

        # Roles
        if new_process in data['processes']:
            _default_roles = data['processes'][new_process]['roles']
        else:
            _default_roles = []
        new_process_roles = st.multiselect("Required roles:", data['roles'], _default_roles,
                                           help="Which roles are needed for process success.")

        if len(new_process_roles)>0:
            commitment_per_week = st.checkbox("Commitment per week",True, help="Whether commitment in hours per business week, or else total involvement.")
        # Commitment
        commitment = dict()
        for role in new_process_roles:
            if (new_process in data['processes']) and (role in data['processes'][new_process]['commitment']):
                _default_commitment = data['processes'][new_process]['commitment'][role]
            else:
                _default_commitment = 0
            if commitment_per_week:
                if new_process_duration == 0:
                    _default_commitment = 0
                else:
                    _default_commitment = (_default_commitment * 5) // new_process_duration
            _commitment = st.slider(f"Hours {role}:", 0, 80, _default_commitment, step=1,
                                    help="Hours of the resource performing this role, either total or per week.")
            if commitment_per_week:
                _commitment = _commitment * new_process_duration // 5
            commitment[role] = _commitment



        # Success Probability give correct resources
        if new_process in data['processes']:
            _default_success_prob = data['processes'][new_process]['success_prob']
        else:
            _default_success_prob = 100
        new_process_success_prob = st.slider("Success prob (%):", 0, 100, _default_success_prob, step=10,
                                             help="Probability of success with fully resourced process.")

        # Earliest start
        if new_process in data['processes']:
            _default_earliest_start = datetime.datetime.fromisoformat(data['processes'][new_process]['earliest_start'])
        else:
            _default_earliest_start = strip_time(datetime.datetime.now())
        new_process_earliest_start = st.date_input("Earliest start:", _default_earliest_start,
                                                           help="What date is the earliest this process can start?")

        # # Earliest start
        # if new_process in data['processes']:
        #     _default_earliest_start = datetime.datetime.fromisoformat(data['processes'][new_process]['earliest_start'])
        # else:
        #     _default_earliest_start = strip_time(datetime.datetime.now())
        # new_process_earliest_start = st.date_input("Earliest start:", _default_earliest_start,
        #                                            help="What date is the earliest this process can start?")


        # Reward
        if new_process in data['processes']:
            _default_reward = data['processes'][new_process]['reward']
        else:
            _default_reward = "0"
        new_process_reward = st.text_input("Reward ($):", _default_reward,
                                                   help="How much is earned by this process.")
        new_process_reward = float(new_process_reward)

        # Add it
        if st.button("Add/Mod process") and (new_process != ""):
            set_process(save_file, data, new_process_name, new_process, commitment, new_process_dependencies,
                        new_process_duration, new_process_earliest_start, new_process_reward, new_process_roles,
                        new_process_success_prob)

        # Delete
        delete_options = list(data['processes'])
        for subgraph in data['subgraphs']:
            if re.match("SG-(.+?)-RO",subgraph) is not None:
                delete_options = list(set(delete_options) - set(data['subgraphs'][subgraph]['processes']))
        delete_options = delete_options + list(data['subgraphs'])
        _delete_processes = st.multiselect("Delete processes: ", delete_options, key=0)
        if st.button("Delete processes", key=1) and len(_delete_processes) > 0:
            delete_processes(data, _delete_processes, save_file)

    # Display them
    with st.beta_expander("Processes"):
        for process in data['processes']:
            if not any([process in data['subgraphs'][subgraph]['processes']
                    for subgraph in rollouts]):
                st.markdown(f" - ({process}) {data['processes'][process]['name']}")


def delete_processes(data, processes, save_file):
    # unload subgraphs
    _processes = []
    _subgraphs = []
    for process in processes:
        if process in data['subgraphs']:
            _processes += data['subgraphs'][process]['processes']
            _subgraphs.append(process)
        else:
            _processes.append(process)
    processes = list(set(_processes))
    for process in processes:
        # delete process
        del data['processes'][process]
        # st.warning(f"Deleting process: {process}")
        # delete from subgraphs
        for subgraph in data['subgraphs']:
            if process in data['subgraphs'][subgraph]['processes']:
                idx = data['subgraphs'][subgraph]['processes'].index(process)
                del data['subgraphs'][subgraph]['processes'][idx]
        # delete from dependencies
        for other_process in data['processes']:
            if process in data['processes'][other_process]['dependencies']:
                idx = data['processes'][other_process]['dependencies'].index(process)
                del data['processes'][other_process]['dependencies'][idx]
    for subgraph in _subgraphs:
        del data['subgraphs'][subgraph]
        # st.warning(f"Deleting subgraph: {subgraph}")
    flush_state(save_file, data)


def set_process(save_file, data, new_process_name, new_process=None, commitment=None, new_process_dependencies=None,
                new_process_duration=None, new_process_earliest_start=None, new_process_reward=None,
                new_process_roles=None,
                new_process_success_prob=None,
                new_process_delay_start=None):
    if new_process is None:
        new_process = symbolify_process_name(data, new_process_name)

    if new_process_dependencies is None:
        new_process_dependencies = []
    new_process_dependencies = list(new_process_dependencies)

    if new_process_duration is None:
        new_process_duration = 0
    new_process_duration = int(new_process_duration)

    if new_process_earliest_start is None:
        new_process_earliest_start = datetime.datetime.now()
    new_process_earliest_start = next_business_day(strip_time(new_process_earliest_start))

    if new_process_reward is None:
        new_process_reward = 0.
    new_process_reward = float(new_process_reward)

    if new_process_delay_start is None:
        new_process_delay_start = 0
    new_process_delay_start = int(new_process_delay_start)

    if new_process_roles is None:
        new_process_roles = []
    new_process_roles = list(new_process_roles)

    if commitment is None:
        commitment = {role: 0. for role in new_process_roles}

    if new_process_success_prob is None:
        new_process_success_prob = 100
    new_process_success_prob = int(new_process_success_prob)

    data['processes'][new_process] = dict(roles=new_process_roles,
                                          dependencies=new_process_dependencies,
                                          reward=new_process_reward,
                                          success_prob=new_process_success_prob,
                                          commitment=commitment,
                                          duration=new_process_duration,
                                          earliest_start=new_process_earliest_start.isoformat(),
                                          delay_start=new_process_delay_start,
                                          name=new_process_name)
    # update broadcasted variables
    new_process = new_process.split("-")[0]
    for other_process in data['processes']:
        if (re.match(f"{new_process}-(.+?)", other_process) is not None) or (new_process == other_process):
            data['processes'][other_process]['roles'] = new_process_roles
            data['processes'][other_process]['reward'] = new_process_reward
            data['processes'][other_process]['success_prob'] = new_process_success_prob
            data['processes'][other_process]['commitment'] = commitment
            data['processes'][other_process]['duration'] = new_process_duration
    flush_state(save_file, data)