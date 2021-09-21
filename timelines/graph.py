import streamlit as st
from graphviz import Digraph as graphviz_graph
from .utils import Cache
from .critical_path import get_critical_path


def display_graph(data, scenario, date_of_change):
    if st.checkbox("Display graph", False):

        G, critical_path = get_critical_path(Cache(data=data), scenario, date_of_change)

        H = graphviz_graph(engine='dot')

        for process in G.nodes:
            if process in critical_path:
                label = "{}\nDur={} days\nPs={:.3f}, Pf={:.3f}".format(process, G.nodes[process]['duration'].days,
                                                                       round(G.nodes[process]['start_prob'], 3),
                                                                       round(G.nodes[process]['success'], 3))
            else:
                label = "{}\nSlack={} days\nPs={:.3f}, Pf={:.3f}".format(process, G.nodes[process]['total_float'].days,
                                                                         round(G.nodes[process]['start_prob'], 3),
                                                                         round(G.nodes[process]['success'], 3))
            H.node(process, penwidth="3" if process in critical_path else "1",
                   label=label)
            for dep in G.pred[process]:
                H.edge(dep, process, penwidth="3" if (dep in critical_path) and (process in critical_path) else "1")

        st.graphviz_chart(H)