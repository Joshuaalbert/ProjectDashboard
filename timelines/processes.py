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
    with st.sidebar.expander("Processes"):
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
            st.info(f"Found ({new_process}) {new_process_name}")
            _default_done = data['processes'][new_process]['done']
            _default_done_date = datetime.datetime.fromisoformat(data['processes'][new_process]['done_date'])
        else:
            new_process = symbolify_process_name(data, new_process_name)
            _default_done = False
            _default_done_date = None

        process_done = st.checkbox("Done", _default_done, help="Is this process done?")
        if process_done:
            done_date = st.date_input("Done date",value=_default_done_date,
                                      min_value=None, max_value=datetime.datetime.now(),
                                      help="When was the process done?")
        else:
            done_date = None


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



        if new_process in data['processes']:
            _default_duration = data['processes'][new_process]['duration']
            _default_pessimistic_modifer = data['processes'][new_process]['pessimistic_modifier']
            _default_optimistic_modifer = data['processes'][new_process]['optimistic_modifier']
            if _default_duration % 5 == 0:
                _default_duration = _default_duration // 5
                _default_duration_in_weeks = True
            else:
                _default_duration_in_weeks = False

        else:
            _default_duration = 0
            _default_duration_in_weeks = True
            _default_pessimistic_modifer = 3.
            _default_optimistic_modifer = 0.3


        # Duration
        duration_in_weeks = st.checkbox("Duration in weeks", _default_duration_in_weeks,
                                        help="Whether duration of process is in business weeks.")

        if duration_in_weeks:
            new_process_duration = st.slider("Duration: ", 0, 52, _default_duration, step=1,
                                                     help="Duration of the process in weeks.")
            new_process_duration *= 5
        else:
            new_process_duration = st.slider("Duration: ", 0, 30, _default_duration, step=1,
                                             help="Duration of the process in business days.")


        pessimistic_modifier = st.slider("Pessimistic modifier", min_value=1., max_value=5., value=_default_pessimistic_modifer, step=0.1,
                  help="Pessimistic estimate of duration is duration times this.")

        optimistic_modifier = st.slider("Optimistic modifier", min_value=0., max_value=1.,
                                        value=_default_optimistic_modifer, step=0.1,
                                        help="Optimistic estimate of duration is duration times this.")

        # Roles
        if new_process in data['processes']:
            _default_roles = data['processes'][new_process]['roles']
        else:
            _default_roles = []
        new_process_roles = st.multiselect("Required roles:", data['roles'], _default_roles,
                                           help="Which roles are needed for process success.")

        # Commitment
        commitment = dict()
        for role in new_process_roles:
            if (new_process in data['processes']) and (role in data['processes'][new_process]['commitment']):
                _default_commitment = float(data['processes'][new_process]['commitment'][role])
            else:
                _default_commitment = 1.
            _commitment = st.slider(f"Attention {role}:", 0., 5., _default_commitment, step=1./3.,
                                    help="Attention of this role required for execution. > 1 means more than one resources with this role required.")
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
            _default_earliest_start = strip_time(datetime.datetime.fromisoformat(data['start_date']))
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
            set_process(save_file, data,
                        new_process_name=new_process_name,
                        new_process=new_process,
                        commitment=commitment,
                        new_process_dependencies=new_process_dependencies,
                        new_process_duration=new_process_duration,
                        new_process_earliest_start=new_process_earliest_start,
                        new_process_reward=new_process_reward,
                        new_process_roles=new_process_roles,
                        new_process_success_prob=new_process_success_prob,
                        new_process_delay_start=0,
                        pessimistic_modifier=pessimistic_modifier, optimistic_modifier=optimistic_modifier,
                        process_done=process_done,
                        done_date=done_date)

        # Delete
        delete_options = list(data['processes'])
        for subgraph in data['subgraphs']:
            if re.match("SG-(.+?)-RO",subgraph) is not None:
                delete_options = list(set(delete_options) - set(data['subgraphs'][subgraph]['processes']))
        delete_options = delete_options + list(data['subgraphs'])
        _delete_processes = st.multiselect("Delete processes: ", delete_options)
        if st.button("Delete processes") and len(_delete_processes) > 0:
            delete_processes(data, _delete_processes, save_file)

    # Display them
    with st.expander("Processes"):
        G = nx.DiGraph()
        fill_graph(G, data)
        for process in nx.topological_sort(G):
            if not any([process in data['subgraphs'][subgraph]['processes']
                    for subgraph in rollouts]):
                _done = data['processes'][process]['done']
                _done_date = data['processes'][process]['done_date']
                if _done:
                    st.markdown(f" - [x] ({process}) {data['processes'][process]['name']} done on {_done_date}")
                else:
                    st.markdown(f" - [ ] ({process}) {data['processes'][process]['name']}")


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
                new_process_delay_start=None,
                pessimistic_modifier=None,
                optimistic_modifier=None,
                process_done=None,
                done_date=None
                ):
    if new_process is None:
        new_process = symbolify_process_name(data, new_process_name)

    if new_process_dependencies is None:
        new_process_dependencies = []
    new_process_dependencies = list(new_process_dependencies)

    if new_process_duration is None:
        new_process_duration = 0
    new_process_duration = int(new_process_duration)


    if pessimistic_modifier is None:
        pessimistic_modifier = 1.
    pessimistic_modifier = float(pessimistic_modifier)

    if optimistic_modifier is None:
        optimistic_modifier = 1.
    optimistic_modifier = float(optimistic_modifier)

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

    if process_done is None:
        process_done = False
    if done_date is None:
        done_date = datetime.datetime.now()
    done_date = next_business_day(strip_time(done_date))

    data['processes'][new_process] = dict(roles=new_process_roles,
                                          dependencies=new_process_dependencies,
                                          reward=new_process_reward,
                                          success_prob=new_process_success_prob,
                                          commitment=commitment,
                                          duration=new_process_duration,
                                          pessimistic_modifier=pessimistic_modifier,
                                          optimistic_modifier=optimistic_modifier,
                                          earliest_start=new_process_earliest_start.isoformat(),
                                          delay_start=new_process_delay_start,
                                          name=new_process_name,
                                          done=process_done,
                                          done_date=done_date.isoformat())
    # update broadcasted variables
    new_process = new_process.split("-")[0]
    for other_process in data['processes']:
        if (re.match(f"{new_process}-(.+?)", other_process) is not None) or (new_process == other_process):
            data['processes'][other_process]['roles'] = new_process_roles
            data['processes'][other_process]['reward'] = new_process_reward
            data['processes'][other_process]['success_prob'] = new_process_success_prob
            data['processes'][other_process]['commitment'] = commitment
            data['processes'][other_process]['duration'] = new_process_duration
            data['processes'][other_process]['pessimistic_modifier'] = pessimistic_modifier
            data['processes'][other_process]['optimistic_modifier'] = optimistic_modifier
    flush_state(save_file, data)