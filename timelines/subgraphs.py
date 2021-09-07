import datetime
from copy import deepcopy
import re
import streamlit as st
import networkx as nx
from .processes import delete_processes
from .utils import flush_state, symbolify, fill_graph, add_business_days


def render_process_subgraph(data, save_file):
    with st.sidebar.expander("Subgraphs"):
        new_subgraph_name = st.text_input("Subgraph Name: ")

        if new_subgraph_name in data['subgraphs']:
            # Name was symbol, so swap
            new_subgraph = new_subgraph_name
            new_subgraph_name = data['subgraphs'][new_subgraph]['name']
        else:
            new_subgraph = symbolify_subgraph_name(data, new_subgraph_name)

        if new_subgraph in data['subgraphs']:
            _default_processes = data['subgraphs'][new_subgraph]['processes']
        else:
            _default_processes = []
        processes = st.multiselect("Processes: ", list(data['processes']), _default_processes)

        # Add them
        if st.button("Add/Mod subgraph") and len(processes) > 0 and new_subgraph != "":
            add_subgraph(data, save_file, new_subgraph, new_subgraph_name, processes)

        if st.button("Replicate", help="Replicate the subgraph replacing dependencies with replicated ones.") and (len(processes) > 0):
            replicate_subgraph(data, save_file, processes)

        if st.checkbox("Roll-out subgraph", False,
                       help='Rolling out a subgraph allows you to make a chain of subgraphs with triggering events.'):
            num_rollout = st.slider("Number to roll-out: ",0,100,0,step=1, help="Number of subgraphs to roll-out.")
            rollout_delay = st.slider("Roll-out delay: ",0,30,1,
                                      help="Time to wait after dependencies trigger in business days.")
            rollout_dependencies = st.multiselect("Dependency triggers: ", processes, [],
                                                  help="Events of preceeding chain element which must have succeeded to initiate new process.")
            all_rollout = []
            if st.button("Roll-out", help="Add the roll-out") and (num_rollout > 0) and len(processes) > 0:
                #replicate from first subgraph then remove edges to initial subgraph
                G = nx.DiGraph()
                fill_graph(G, data)
                G = G.subgraph(processes)
                in_nodes = [v for v, d in G.in_degree() if d == 0]
                out_nodes = [v for v, d in G.out_degree() if d == 0]
                dep_nodes = rollout_dependencies
                rollout_processes = processes
                for rollout_idx in range(num_rollout):
                    symbol_map = replicate_subgraph(data, save_file, rollout_processes)
                    for new_process in symbol_map.values():
                        all_rollout.append(new_process)
                    for in_node_idx, old_root_node in enumerate(in_nodes):
                        new_root = symbol_map[old_root_node]

                        data['processes'][new_root]['delay_start'] = rollout_delay
                        for dep in dep_nodes:
                            #link to new root
                            data['processes'][new_root]['dependencies'].append(dep)

                        in_nodes[in_node_idx] = symbol_map[old_root_node]
                    for dep_idx, dep in enumerate(dep_nodes):
                        dep_nodes[dep_idx] = symbol_map[dep]
                    rollout_processes = list(symbol_map.values())
                flush_state(save_file, data)

                rollout_symbol = f"{new_subgraph}-RO"
                if rollout_symbol in data['subgraphs']:
                    _new_subgraph = rollout_symbol
                    i = 1
                    while _new_subgraph in data['subgraphs']:
                        _new_subgraph = rollout_symbol + str(i)
                        i += 1
                    rollout_symbol = _new_subgraph

                add_subgraph(data, save_file, rollout_symbol, f"{new_subgraph_name} (roll-out)", all_rollout)

        # Delete them
        delete_subgraphs = st.multiselect('Delete subgraphs:', list(data['subgraphs']), [])
        if st.button("Delete subgraph") and len(delete_subgraphs)>0:
            delete_subgraph(data, delete_subgraphs, save_file)

    # Display them
    rollouts = []
    for subgraph in data['subgraphs']:
        if re.match("SG-(.+?)-RO", subgraph) is not None:
            rollouts.append(subgraph)
    with st.expander("Subgraphs"):
        for subgraph in data['subgraphs']:
            if subgraph in rollouts:
                counts = set()
                rollout_processes = set()
                for process in data['subgraphs'][subgraph]['processes']:
                    _p,_c = process.split("-")
                    rollout_processes.add(_p)
                    counts.add(_c)
                st.markdown(
                    f" - ({subgraph}) {data['subgraphs'][subgraph]['name']}: (x{len(counts)}) {', '.join(data['subgraphs'][subgraph]['processes'])}")
            else:
                st.markdown(f" - ({subgraph}) {data['subgraphs'][subgraph]['name']}: {', '.join(data['subgraphs'][subgraph]['processes'])}")

def new_replicated_process_name(data, symbol):
    #Get root symbol
    symbol = symbol.split("-")[0]
    i = 1
    _symbol = symbol
    while _symbol in data['processes']:
        _symbol = f"{symbol}-{str(i)}"
        i += 1
    return _symbol

def replicate_subgraph(data, save_file, processes):
    symbol_map = dict()
    for process in processes:
        new_symbol = new_replicated_process_name(data, process)
        # st.warning(f"Duplicating {process} to {new_symbol}")
        symbol_map[process] = new_symbol
        data['processes'][new_symbol] = deepcopy(data['processes'][process])
    for new_process in symbol_map.values():
        for idx, dep in enumerate(data['processes'][new_process]['dependencies']):
            if dep in symbol_map:
                data['processes'][new_process]['dependencies'][idx] = symbol_map[dep]
                # st.warning(f"Replacing dep {dep} with {symbol_map[dep]}")
    flush_state(save_file, data)
    return symbol_map


def delete_subgraph(data, delete_subgraphs, save_file):
    for subgraph in delete_subgraphs:
        del data['subgraphs'][subgraph]
        st.warning(f"Deleted {subgraph}, Keeping processes associated with it.")
    flush_state(save_file, data)


def add_subgraph(data, save_file, new_subgraph, new_subgraph_name, processes):
    data['subgraphs'][new_subgraph] = dict(processes=processes,
                                           name=new_subgraph_name)
    flush_state(save_file, data)


def symbolify_subgraph_name(data, new_subgraph_name):
    new_subgraph = f"SG-{symbolify(new_subgraph_name)}"
    # Get unique symbol
    if new_subgraph in data['subgraphs']:
        _new_subgraph = new_subgraph
        i = 1
        while _new_subgraph in data['subgraphs']:
            _new_subgraph = new_subgraph + str(i)
            i += 1
        new_subgraph = _new_subgraph
    return new_subgraph