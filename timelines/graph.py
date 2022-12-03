import streamlit as st
from graphviz import Digraph as graphviz_graph

from .critical_path import get_critical_path
from .utils import Cache


def display_graph(data, date_of_change):
    def fix_node_name(node):
        return node.replace(":", ".")

    if st.checkbox("Display graph", False):

        G, critical_path = get_critical_path(Cache(data=data), date_of_change)

        H = graphviz_graph(engine='dot')

        for process in G.nodes:
            if process in critical_path:
                label = "{}\nDur={} days".format(process, G.nodes[process]['duration'].days)
            else:
                label = "{}\nSlack={} days".format(process, G.nodes[process]['total_float'].days)
            tooltip = f"{G.nodes[process]['name']}"
            H.node(fix_node_name(process), penwidth="3" if process in critical_path else "1",
                   label=label, tooltip=tooltip)
            for dep in G.pred[process]:
                H.edge(fix_node_name(dep), fix_node_name(process),
                       penwidth="3" if (dep in critical_path) and (process in critical_path) else "1")

        st.graphviz_chart(H)
