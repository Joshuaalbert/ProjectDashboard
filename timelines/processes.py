import datetime
import networkx as nx
import streamlit as st

from .utils import flush_state, fill_graph, symbolify, next_business_day, strip_time, Cache, count_business_days, add_business_days
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
    # s+d = t+r => d = count(s, t+r)
    def _f():
        process_duration = count_business_days(strip_time(st.session_state['process_date_started']),
                                               add_business_days(next_business_day(strip_time(datetime.datetime.now())),
                                                                 datetime.timedelta(days=st.session_state[remaining_key])))
        st.session_state[remaining_key.replace('remaining', 'duration')] = process_duration

    return _f


def build_duration_to_remaining(duration_key):
    #s+d = t+r => r = count(t, s + d)
    def _f():
        process_remaining = count_business_days(next_business_day(strip_time(datetime.datetime.now())),
                                                add_business_days(strip_time(st.session_state['process_date_started']),
                                                                  datetime.timedelta(days=st.session_state[duration_key])))
        st.session_state[duration_key.replace('duration', 'remaining')] = process_remaining

    return _f

def load_from_data(data, process):
    date = data['processes'][process]['last_date']
    process_data = data['processes'][process]['history'][date]
    session_state = dict(
        process=process,  # symbol
        process_name=process_data['name'],  # Name
        process_dependencies=process_data['dependencies'],
        process_date_started=datetime.datetime.fromisoformat(
            process_data['started_date']),
        process_started=process_data['started'],
        duration=process_data['duration'],
        pessimistic_duration=process_data['pessimistic_duration'],
        optimistic_duration=process_data['optimistic_duration'],
        process_roles=process_data['roles'],
        process_commitment=process_data['commitment'],
        process_earliest_start=datetime.datetime.fromisoformat(
            process_data['earliest_start']),
        process_start_earliest_start=process_data['start_earliest_start'],
        process_delay_start=process_data['delay_start'])
    for key in session_state:
        st.session_state[key] = session_state[key]

def set_default_values(data, weak=False):
        session_state = dict(
            process="",
            process_name="",
            process_dependencies=[],
            process_date_started=strip_time(datetime.datetime.now()),
            process_started=False,
            duration=0,
            pessimistic_duration=0,
            optimistic_duration=0,
            process_roles=[],
            process_commitment=dict(),
            process_earliest_start=strip_time(datetime.datetime.fromisoformat(data['start_date'])),
            process_start_earliest_start=False,
            process_delay_start=0
        )

        for key in session_state:
            if weak:
                if key in st.session_state:
                    continue
            st.session_state[key] = session_state[key]


def render_processes(data, save_file, advanced, date_of_change):
    ###
    # processes
    with st.sidebar.expander("Processes"):
        def _lookup_process_cb():
            process_lookup = st.session_state['process_lookup']
            if len(process_lookup) == 1:  # found
                process = process_lookup[0]
                load_from_data(data, process) # data closure
            else:  # none looked up, defaults are set
                set_default_values(data)

        process_lookup = st.multiselect("Process Lookup: ",
                                        data['processes'],
                                        help='Look-up a process via symbol and modify.',
                                        # on_change=_lookup_process_cb,
                                        key='process_lookup')


        found_process = False
        if len(process_lookup) == 0:
            if st.button("Reset process data"):
                set_default_values(data)
            process_name = st.text_input("Process name: ",
                                         help="Add a new process. Makes a new symbol for that name.",
                                         key='process_name')
            process = symbolify_process_name(data, process_name)
            st.session_state['process'] = process
        elif len(process_lookup) == 1:
            process = process_lookup[0]
            if st.button("Load process data"):
                load_from_data(data, process)  # data closure
            date = data['processes'][process]['last_date']
            process_data = data['processes'][process]['history'][date]
            st.session_state['process_name'] = process_data['name'] # Name
            found_process = True
        else:
            raise ValueError("Too many symbols selected.")

        set_default_values(data, weak=True)

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
                       default=st.session_state['process_dependencies'],
                       help="What are the dependencies of this process.",
                       key='process_dependencies')


        if found_process:
            st.checkbox("Process Started",
                        value=st.session_state['process_started'],
                                          help="Is this process underway?",
                                          key='process_started')
            if st.session_state['process_started']:#st.session_state['process_started']:
                def _clean_date():
                    st.session_state['process_date_started'] = next_business_day(
                        strip_time(st.session_state['process_date_started']))
                st.date_input("Date started",
                              min_value=None,
                              max_value=datetime.datetime.now(),
                              help="When was the process sarted?",
                              key='process_date_started',
                              on_change=_clean_date)

                # process_date_done = add_business_days(st.session_state['process_date_started'],
                #                                       datetime.timedelta(days=st.session_state['process_duration']))
                # process_done = datetime.datetime.now() >= process_date_done
                # if process_done:
                #     st.sidebar.info(f"Process completed on {process_date_done.isoformat()}.")

        # st.write(st.session_state)

        if 'process_started' not in st.session_state:
            st.session_state['process_started'] = False

        # Durations
        duration_text = st.text_input("Conservative duration (days)",
            max_chars=3,
            value=st.session_state['duration'],
            help='Conservative estimate days duration total.')
        if duration_text != "":
            try:
                if str(int(duration_text)) != duration_text:
                    st.warning("Duration much be an integer, e.g. 10")
            except:
                st.warning("Duration much be an integer, e.g. 10")

        st.slider("Conservative duration (days)",
                  min_value=0,
                  max_value=90,
                  value=int(duration_text),
                  step=1,
                  help='Conservative estimate days duration total.',
                  key='duration')





        if 'pessimistic_duration' in st.session_state:
            st.session_state['pessimistic_duration'] = min(max(st.session_state['pessimistic_duration'],
                          st.session_state['duration']),
                          st.session_state['duration'] + 30)

        st.slider("Pessimistic duration (days)",
                  min_value=st.session_state['duration'],
                  max_value=st.session_state['duration'] + 30,
                  value=st.session_state['pessimistic_duration'],
                  step=1,
                  help='Pessimisic estimate of days to do.',
                  key='pessimistic_duration')

        if 'optimistic_duration' in st.session_state:
            st.session_state['optimistic_duration'] = max(0,min(st.session_state['optimistic_duration'],
                          st.session_state['duration']))

        st.slider("Optimistic duration (days)",
                  min_value=0,
                  max_value=max(st.session_state['duration'], 1),
                  value=st.session_state['optimistic_duration'],
                  step=1,
                  help='Optimistic estimate of days to do.',
                  key='optimistic_duration')

        st.subheader("Starting constraints")

        # Earliest start
        st.date_input("Earliest start:",
                      value=st.session_state['process_earliest_start'],
                      help="What date is the earliest this process can start?",
                      key='process_earliest_start')

        st.checkbox("Start exactly at earliest date?",
                    value=st.session_state['process_start_earliest_start'],
                      help="Should this be planned to start exactly at the earliest date?",
                      key='process_start_earliest_start')

        # Delay start
        st.slider("Delay start:", min_value=0, max_value=30,
                  value=st.session_state['process_delay_start'],
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
            if (process not in display_process) and (len(display_process) > 0):
                continue
            last_date = data['processes'][process]['last_date']
            if data['processes'][process]['history'][last_date]['started']:
                _done_date = add_business_days(datetime.datetime.fromisoformat(data['processes'][process]['history'][last_date]['started_date']),
                                      datetime.timedelta(days=data['processes'][process]['history'][last_date]['duration']))
                _done = datetime.datetime.now() >= _done_date
            else:
                _done = False
                _done_date = None
            if _done:
                st.sidebar.info(f"Process completed on {_done_date.isoformat()}.")
                st.markdown(f" - [x] ({process}) {data['processes'][process]['history'][last_date]['name']} done on {date_label(_done_date)}")
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
    process_started = bool(st.session_state['process_started'])
    process_start_date = next_business_day(
        strip_time(st.session_state['process_date_started'])) if 'process_date_started' in st.session_state else today
    process_duration = int(st.session_state['duration'])
    pessimistic_duration = int(st.session_state['pessimistic_duration'])
    optimistic_duration = int(st.session_state['optimistic_duration'])
    process_earliest_start = next_business_day(strip_time(st.session_state['process_earliest_start']))
    process_start_earliest_start = st.session_state['process_start_earliest_start']
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
        started=process_started,
        started_date=process_start_date.isoformat(),
        duration=process_duration,
        pessimistic_duration=pessimistic_duration,
        optimistic_duration=optimistic_duration,
        earliest_start=process_earliest_start.isoformat(),
        start_earliest_start=process_start_earliest_start,
        delay_start=process_delay_start
    )
    data['processes'][process]['last_date'] = today.isoformat()
    flush_state(save_file, data)
