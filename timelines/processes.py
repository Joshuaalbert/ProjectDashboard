import datetime
import networkx as nx
import streamlit as st

from .utils import flush_state, fill_graph, symbolify, next_business_day, strip_time, Cache, count_business_days, \
    add_business_days
from .critical_path import get_critical_path


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


def build_remaining_to_duration(remaining_key):
    def _f():
        if 'process_date_started' not in st.session_state:
            start_date = next_business_day(strip_time(datetime.datetime.now()))
        else:
            start_date = strip_time(st.session_state['process_date_started'])
        process_duration = count_business_days(start_date,
                                               add_business_days(datetime.datetime.now(),
                                                                 datetime.timedelta(
                                                                     days=st.session_state[remaining_key])))
        st.session_state[remaining_key.replace('remaining', 'duration')] = process_duration

    return _f


def build_duration_to_remaining(duration_key):
    def _f():
        if 'process_date_started' not in st.session_state:
            start_date = next_business_day(strip_time(datetime.datetime.now()))
        else:
            start_date = strip_time(st.session_state['process_date_started'])
        process_remaining = count_business_days(datetime.datetime.now(),
                                                add_business_days(start_date,
                                                                  datetime.timedelta(
                                                                      days=st.session_state[duration_key])))
        st.session_state[duration_key.replace('duration', 'remaining')] = process_remaining

    return _f


def render_processes(data, save_file, advanced, date_of_change):
    ###
    # processes
    with st.sidebar.expander("Processes"):
        def _lookup_process_cb():
            process_lookup = st.session_state['process_lookup']
            if len(process_lookup) == 1:  # found
                process = process_lookup[0]
                date = data['processes'][process]['last_date']
                process_data = data['processes'][process]['history'][date]
                session_state = dict(
                    process=process_lookup[0],  # symbol
                    process_name=process_data['name'],  # Name
                    process_done=process_data['done'],
                    process_done_date=datetime.datetime.fromisoformat(process_data['done_date']),
                    process_dependencies=process_data['dependencies'],
                    process_date_started=datetime.datetime.fromisoformat(
                        process_data['started_date']),
                    process_started=process_data['started'],
                    process_duration=process_data['duration'],
                    pessimistic_duration=process_data['pessimistic_duration'],
                    optimistic_duration=process_data['optimistic_duration'],
                    process_roles=process_data['roles'],
                    process_commitment=process_data['commitment'],
                    process_earliest_start=datetime.datetime.fromisoformat(
                        process_data['earliest_start']),
                    process_delay_start=process_data['delay_start']
                )
            else:  # none looked up, defaults are set
                session_state = dict(
                    process="",
                    process_name="",
                    process_done=False,
                    process_done_date=strip_time(datetime.datetime.now()),
                    process_dependencies=[],
                    process_date_started=strip_time(datetime.datetime.now()),
                    process_started=False,
                    process_duration=0,
                    pessimistic_duration=0,
                    optimistic_duration=0,
                    process_roles=[],
                    process_commitment=dict(),
                    process_earliest_start=strip_time(datetime.datetime.fromisoformat(data['start_date'])),
                    process_delay_start=0
                )

            for key in session_state:
                st.session_state[key] = session_state[key]
            build_duration_to_remaining('process_duration')
            build_duration_to_remaining('pessimistic_duration')
            build_duration_to_remaining('optimistic_duration')

        process_lookup = st.multiselect("Process Lookup: ",
                                        data['processes'],
                                        help='Look-up a process via symbol and modify.',
                                        on_change=_lookup_process_cb,
                                        key='process_lookup')

        if len(process_lookup) == 0:
            process_name = st.text_input("Process name: ",
                                         help="Add a new process. Makes a new symbol for that name.",
                                         key='process_name')
            process = symbolify_process_name(data, process_name)
        elif len(process_lookup) == 1:
            process = process_lookup[0]
        else:
            raise ValueError("Too many symbols selected.")

        # store process name
        st.session_state['process'] = process

        # Dependencies
        dep_options = list(data['processes'])
        if process in data['processes']:
            G = nx.DiGraph()
            fill_graph(G, data, datetime.datetime.fromisoformat(data['processes'][process]['last_date']))
            descendants = nx.algorithms.dag.descendants(G, process)
            dep_options = sorted(list(set(dep_options) - descendants - {process}))

        st.multiselect("Dependencies:",
                       dep_options,
                       help="What are the dependencies of this process.",
                       key='process_dependencies')

        process_done = st.checkbox("Process Done",
                                   help="Is this process done?",
                                   key='process_done')

        if process_done:
            def _clean_date():
                st.session_state['process_done_date'] = next_business_day(
                    strip_time(st.session_state['process_done_date']))

            st.date_input("Date finished",
                          min_value=None,
                          max_value=datetime.datetime.now(),
                          help="When was the process done?",
                          key='process_done_date',
                          on_change=_clean_date)

        if process_done:
            st.session_state['process_started'] = True

        process_started = st.checkbox("Process Started",
                                      help="Is this process underway?",
                                      key='process_started')

        if process_started:
            def _clean_date():
                st.session_state['process_date_started'] = next_business_day(
                    strip_time(st.session_state['process_date_started']))

            st.date_input("Date started",
                          min_value=None,
                          max_value=st.session_state['process_done_date'] if process_done else datetime.datetime.now(),
                          help="When was the process done?",
                          key='process_date_started',
                          on_change=_clean_date)

        if process_started and not process_done:

            st.slider("Conservative remaining (days)",
                      min_value=0,
                      max_value=30,
                      step=1,
                      help='Days remaining until done.',
                      key='process_remaining',
                      on_change=build_remaining_to_duration('process_remaining'))

            st.slider("Pessimistic remaining (days)",
                      min_value=st.session_state['process_remaining'],
                      max_value=st.session_state['process_remaining'] + 30,
                      step=1,
                      help='Pessimistic estimate of days remaining until done.',
                      key='pessimistic_remaining',
                      on_change=build_remaining_to_duration('pessimistic_remaining'))

            st.slider("Optimistic remaining (days)",
                      min_value=0,
                      max_value=max(st.session_state['process_remaining'], 1),
                      step=1,
                      help='Optimistic estimate of days remaining until done.',
                      key='optimistic_remaining',
                      on_change=build_remaining_to_duration('optimistic_remaining'))


        elif not process_started and not process_done:
            st.slider("Conservative duration (days)",
                      min_value=0,
                      max_value=30,
                      step=1,
                      help='Days duration total.',
                      key='process_duration',
                      on_change=build_duration_to_remaining('process_duration'))

            st.slider("Pessimistic duration (days)",
                      min_value=st.session_state['process_duration'],
                      max_value=st.session_state['process_duration'] + 30,
                      step=1,
                      help='Conservative estimate of days to do.',
                      key='pessimistic_duration',
                      on_change=build_duration_to_remaining('pessimistic_duration'))

            st.slider("Optimistic duration (days)",
                      min_value=0,
                      max_value=max(st.session_state['process_duration'], 1),
                      step=1,
                      help='Optimistic estimate of days to do.',
                      key='optimistic_duration',
                      on_change=build_duration_to_remaining('optimistic_duration'))
        elif process_started and process_done:
            st.session_state['process_duration'] = count_business_days(
                strip_time(st.session_state['process_date_started']),
                strip_time(st.session_state['process_done_date']))
            st.session_state['pessimistic_duration'] = st.session_state['optimistic_duration'] = st.session_state[
                'process_duration']
            st.info(f"Duration {st.session_state['process_duration']} days.")
        elif process_done and not process_started:
            st.info("If process done then a start date must be chosen too.")

        st.subheader("Starting constraints")

        # Earliest start
        st.date_input("Earliest start:",
                      help="What date is the earliest this process can start?",
                      key='process_earliest_start')

        # Delay start
        st.slider("Delay start:", min_value=0, max_value=30,
                  help="Delay stary by this many days after all dependencies end.",
                  key='process_delay_start')

        if advanced:
            # Roles
            process_roles = st.multiselect("Required roles:", data['roles'],
                                           help="Which roles are needed for process success.",
                                           key='process_roles')

            # Commitment
            commitment = dict()
            for role in process_roles:
                last_date = data['processes'][process]['last_date']
                if (process in data['processes']) and (
                        role in data['processes'][process]['history'][last_date]['commitment']):
                    _default_commitment = float(data['processes'][process]['history'][last_date]['commitment'][role])
                else:
                    _default_commitment = 1.
                if advanced:
                    _commitment = st.slider(f"Attention {role}:", 0., 5., _default_commitment, step=1. / 3.,
                                            help="Attention of this role required for execution. > 1 means more than one resources with this role required.")
                else:
                    _commitment = _default_commitment
                commitment[role] = _commitment
        else:
            st.session_state['process_roles'] = []

        # Add it
        if st.button("Add/Mod process") and (process != ""):
            set_process(save_file, data)

        # Delete
        delete_options = list(data['processes'])
        delete_options = delete_options
        _delete_processes = st.multiselect("Delete processes: ", delete_options)
        if st.button("Delete processes") and len(_delete_processes) > 0:
            delete_processes(data, _delete_processes, save_file)

    # Display them
    with st.expander("Processes"):
        display_process = st.multiselect("Filter process", data['processes'])
        G, critical_path = get_critical_path(Cache(data), date_of_change)
        for process in nx.algorithms.topological_sort(G):
            if process not in display_process:
                continue
            last_date = data['processes'][process]['last_date']
            _done = data['processes'][process]['history'][last_date]['done']
            _done_date = datetime.datetime.fromisoformat(data['processes'][process]['history'][last_date]['done_date'])
            if _done:
                st.markdown(
                    f" - [x] ({process}) {data['processes'][process]['history'][last_date]['name']} done on {date_label(_done_date)}")
            else:
                st.markdown(f" - [ ] ({process}) {data['processes'][process]['history'][last_date]['name']}")
                st.markdown(f"Earliest Start: {date_label(G.nodes[process]['ES'])}")
                st.markdown(f"Latest Start: {date_label(G.nodes[process]['LS'])}")
                st.markdown(f"Earliest Finish: {date_label(G.nodes[process]['EF'])}")
                st.markdown(f"Latest Finish: {date_label(G.nodes[process]['LF'])}")


def date_label(date: datetime.datetime):
    return date.strftime("%a, %d %b, %Y")


def delete_processes(data, processes, save_file):
    # unload subgraphs
    _processes = []
    for process in processes:
        _processes.append(process)
    processes = list(set(_processes))
    for process in processes:
        # delete process
        del data['processes'][process]
        # delete from dependencies
        for other_process in data['processes']:
            for date in data['processes'][other_process]['history']:
                if process in data['processes'][other_process]['history'][date]['dependencies']:
                    idx = data['processes'][other_process]['history'][date]['dependencies'].index(process)
                    del data['processes'][other_process]['history'][date]['dependencies'][idx]
    flush_state(save_file, data)


def set_process(save_file, data):
    today = next_business_day(strip_time(datetime.datetime.today()))
    process = st.session_state['process']
    process_name = st.session_state['process_name']
    process_dependencies = list(st.session_state['process_dependencies'])
    process_done = bool(st.session_state['process_done'])
    process_done_date = next_business_day(
        strip_time(st.session_state['process_done_date'])) if 'process_done_date' in st.session_state else today
    process_started = bool(st.session_state['process_started'])
    process_start_date = next_business_day(
        strip_time(st.session_state['process_start_date'])) if 'process_start_date' in st.session_state else today
    process_duration = int(st.session_state['process_duration'])
    pessimistic_duration = int(st.session_state['pessimistic_duration'])
    optimistic_duration = int(st.session_state['optimistic_duration'])
    process_earliest_start = next_business_day(strip_time(st.session_state['process_earliest_start']))
    process_delay_start = int(st.session_state['process_delay_start'])
    process_roles = []# list(st.session_state['process_roles'])
    process_commitment = dict()#st.session_state['process_commitment']
    # for role in process_roles:
    #     assert role in data['roles']

    # Use this to make note of duration, and modifiers

    if process not in data['processes']:
        data['processes'][process] = dict(history=dict(),
                                          last_date=today.isoformat())
    data['processes'][process]['history'][today.isoformat()] = dict(
        name=process_name,
        roles=process_roles,
        dependencies=process_dependencies,
        commitment=process_commitment,
        done=process_done,
        done_date=process_done_date.isoformat(),
        started=process_started,
        started_date=process_start_date.isoformat(),
        duration=process_duration,
        pessimistic_duration=pessimistic_duration,
        optimistic_duration=optimistic_duration,
        earliest_start=process_earliest_start.isoformat(),
        delay_start=process_delay_start
    )
    data['processes'][process]['last_date'] = today.isoformat()
    flush_state(save_file, data)
