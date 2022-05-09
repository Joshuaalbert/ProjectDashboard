import streamlit as st
from .utils import hash_map, Cache
from .critical_path import get_critical_path
import networkx as nx
import datetime
import pylab as plt

def render_timeline_changes(cache: Cache, dates_of_change):
    if st.checkbox("Display timeline evolution", False):
        termination_nodes = st.multiselect("Termination point: ", cache['data']['processes'],[], help='What nodes to compute up until, else all.')
        if len(termination_nodes) == 0:
            termination_nodes = None
        fig, ax = plt.subplots(1,1,figsize=(12,28//3))
        _dates = []
        _total_lengths = []

        for date in dates_of_change:
            G, critical_path = get_critical_path(cache, date, termination_nodes=termination_nodes)
            _available_in_data = True
            if termination_nodes is not None:
                for _node in termination_nodes:
                    if _node not in G.nodes:
                        _available_in_data = False
                if not _available_in_data:
                    continue
            _dates.append(date)
            _total_lengths.append(G.critical_path_end)
            #
            # for process in nx.algorithms.topological_sort(G):
            #     if process in critical_path:
            #         print(process)
            #         start_day = G.nodes[process]['ES']
            #         end_day = G.nodes[process]['LF']
            #
            #         ax.bar(date,
            #                end_day - start_day,
            #                bottom=start_day,
            #                facecolor='red',
            #                edgecolor='none',
            #                alpha=0.5)
            #     else:
            #         print(process, G.nodes[process]['ES'])
            #         ax.bar(date,
            #                G.nodes[process]['LS'] - G.nodes[process]['ES'],
            #                bottom=G.nodes[process]['ES'],
            #                facecolor='green',
            #                edgecolor='none',
            #                alpha=0.15)
            #
            #         ax.bar(date,
            #                G.nodes[process]['LF'] - G.nodes[process]['EF'],
            #                bottom=G.nodes[process]['EF'],
            #                facecolor='blue',
            #                edgecolor='none',
            #                alpha=0.15)
            #
            #         ax.bar(date,
            #                (G.nodes[process]['EF'] - G.nodes[process]['LS']).days,
            #                bottom=G.nodes[process]['LS'],
            #                facecolor='yellow',
            #                edgecolor='none',
            #                alpha=0.15)

        G, critical_path = get_critical_path(cache, datetime.datetime.now(), termination_nodes=termination_nodes)
        _dates.append(datetime.datetime.now())
        _total_lengths.append(G.critical_path_end)

        ax.scatter(_dates, _total_lengths)
        ax.plot(_dates, _total_lengths)
        ax.set_ylim(min(_total_lengths) - datetime.timedelta(days=5), max(_total_lengths) + datetime.timedelta(days=5))
        ax.set_xlim(min(_dates) - datetime.timedelta(days=5), max(_dates) + datetime.timedelta(days=5))
        ax.grid()
        ax.axvline(datetime.datetime.now(), c='black', lw=3., alpha=0.75, label='Now')
        ax.set_xlabel("Date of estimation")
        ax.set_ylabel("Estimated date of completion")
        st.write(fig)

