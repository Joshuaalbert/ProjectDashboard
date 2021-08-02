import datetime
import re

import networkx as nx
import streamlit as st
from copy import deepcopy

from .probabilities import compute_event_probabilities
from .resource_usage_analysis import display_graph, display_usage

from .utils import add_business_days, subtract_business_days, count_business_days, next_business_day, strip_time, \
    fill_graph, prod, merge_nodes


class Cache(object):
    def __init__(self, **kwargs):
        self._kwargs = dict()
        for key in kwargs:
            self._kwargs[key] = kwargs[key]

    @property
    def cache_hash(self):
        return self._kwargs['data']['cache_hash']

    def __getitem__(self, item):
        return self._kwargs[item]

hash_map = {Cache:lambda c: c.cache_hash}

class CPM(nx.DiGraph):

    def __init__(self):
        super().__init__()
        self._dirty = True
        self._critical_path_length = -1
        self._criticalPath = None

    def add_node(self, *args, **kwargs):
        self._dirty = True
        super().add_node(*args, **kwargs)

    def add_nodes_from(self, *args, **kwargs):
        self._dirty = True
        super().add_nodes_from(*args, **kwargs)

    def add_edge(self, *args, **kwargs):  # , **kwargs):
        self._dirty = True
        super().add_edge(*args, **kwargs)  # , **kwargs)

    def add_edges_from(self, *args, **kwargs):
        self._dirty = True
        super().add_edges_from(*args, **kwargs)

    def remove_node(self, *args, **kwargs):
        self._dirty = True
        super().remove_node(*args, **kwargs)

    def remove_nodes_from(self, *args, **kwargs):
        self._dirty = True
        super().remove_nodes_from(*args, **kwargs)

    def remove_edge(self, *args):  # , **kwargs):
        self._dirty = True
        super().remove_edge(*args)  # , **kwargs)

    def remove_edges_from(self, *args, **kwargs):
        self._dirty = True
        super().remove_edges_from(*args, **kwargs)

    def _forward(self):
        start_time = next_business_day(strip_time(self.graph['start_date']))# min([self.nodes[j]['earliest_start'] for j in self.nodes], default=next_business_day(strip_time(datetime.datetime.now())))
        for n in nx.topological_sort(self):
            es = max([self.nodes[j]['EF'] for j in self.predecessors(n)], default=start_time)
            es = max([es, self.nodes[n]['earliest_start'], add_business_days(es, self.nodes[n]['delay_start'])])
            ef = add_business_days(es, self.nodes[n]['duration'])
            duration = self.nodes[n]['duration']
            if self.nodes[n]['done']:
                if ef > self.nodes[n]['done_date']:
                    duration = datetime.timedelta(days=count_business_days(es, self.nodes[n]['done_date']))
                    st.write(ef, self.nodes[n]['done_date'], duration)
                    ef = self.nodes[n]['done_date']
            self.add_node(n,
                          ES=es,
                          EF=ef,
                          duration=duration)

    def _backward(self):
        # import streamlit as st
        for n in reversed(list(nx.topological_sort(self))):
            lf = min([self.nodes[j]['LS'] for j in self.successors(n)], default=self._critical_path_length)
            if self.nodes[n]['done']:
                lf = self.nodes[n]['done_date']
            ls = subtract_business_days(lf,self.nodes[n]['duration'])
            self.add_node(n,
                          LS=ls,
                          LF=lf,
                          total_float=datetime.timedelta(days=count_business_days(self.nodes[n]['ES'], lf)) - self.nodes[n]['duration'])



            # st.write(datetime.timedelta(days=count_business_days(self.nodes[n]['ES'], lf)) - self.nodes[n]['duration'])

    def _compute_critical_path(self):
        graph = set()
        for n in self:
            # if (self.nodes[n]['EF'] == self.nodes[n]['LF']) and (self.nodes[n]['ES'] == self.nodes[n]['LS']):
            if (self.nodes[n]['total_float'] == datetime.timedelta(days=0)):
                graph.add(n)
        self._criticalPath = self.subgraph(graph)

    @property
    def critical_path_length(self):
        if self._dirty:
            self._update()
        return self._critical_path_length

    @property
    def critical_path(self):
        if self._dirty:
            self._update()
        return sorted(self._criticalPath, key=lambda x: self.nodes[x]['ES'])

    def _update(self):
        self._forward()
        self._critical_path_length = max(nx.get_node_attributes(self, 'EF').values(), default=datetime.timedelta(0))
        self._backward()
        self._compute_critical_path()
        self._dirty = False

@st.cache(show_spinner=True, suppress_st_warning=True, ttl=3600., allow_output_mutation=True, hash_funcs=hash_map)
def get_critical_path(c, scenario):
    data = c['data']
    G = CPM()

    fill_graph(G, data, scenario)

    compute_event_probabilities(G, 1000)

    critical_path = G.critical_path
    return G, critical_path


def render_critical_path(data):
    st.header("Critical Path")
    scenario = st.radio("Scenario: ", ['Pessimistic', 'Normal', 'Optimistic'], index=1,help="Which scenario to show")

    G, critical_path = get_critical_path(Cache(data=data), scenario)
    G_collapsed, critical_path_collapsed = collapse_rollouts(G, critical_path, data, scenario)

    st.subheader("Graph")
    display_graph(G_collapsed, critical_path_collapsed, data, scenario)

    st.subheader("Timeline")

    display_usage(G, critical_path, data, G_collapsed, critical_path_collapsed, scenario)

@st.cache(show_spinner=True, suppress_st_warning=True, ttl=3600., allow_output_mutation=True, hash_funcs=hash_map)
def get_collapsed_rollout(c, scenario):
    data = c['data']
    G = c['G']
    critical_path = c['critical_path']
    rollout_subgraphs = dict()
    G = deepcopy(G)
    for subgraph in data['subgraphs']:
        if re.match("SG-(.+?)-RO", subgraph) is not None:
            rollout_subgraphs[subgraph] = data['subgraphs'][subgraph]['processes']

    _new_critical_path_additions = set()
    attrs = dict()
    for subgraph in rollout_subgraphs:
        for process in rollout_subgraphs[subgraph]:
            if process in critical_path:
                _new_critical_path_additions.add(subgraph)
                idx = critical_path.index(process)
                # st.warning(f"Removing {critical_path[idx]}")
                del critical_path[idx]

        G_rollout = nx.subgraph(G, rollout_subgraphs[subgraph])

        es = min([G_rollout.nodes[n]['ES'] for n in G_rollout.nodes],
                 default=next_business_day(strip_time(datetime.datetime.now())))
        ls = min([G_rollout.nodes[n]['LS'] for n in G_rollout.nodes],
                 default=next_business_day(strip_time(datetime.datetime.now())))
        ef = max([G_rollout.nodes[n]['EF'] for n in G_rollout.nodes],
                 default=next_business_day(strip_time(datetime.datetime.now())))
        lf = max([G_rollout.nodes[n]['LF'] for n in G_rollout.nodes],
                 default=next_business_day(strip_time(datetime.datetime.now())))
        reward = sum([G_rollout.nodes[n]['reward'] for n in G_rollout.nodes])
        in_nodes = filter(lambda n: G_rollout.in_degree(n) == 0, G_rollout.nodes)
        out_nodes = filter(lambda n: G_rollout.out_degree(n) == 0, G_rollout.nodes)
        start_prob = prod([G_rollout.nodes[n]['start_prob'] for n in in_nodes])
        success_prob = prod([G_rollout.nodes[n]['success'] for n in out_nodes])
        attrs[subgraph] = dict(ES=es, EF=ef, LF=lf, LS=ls,
                               reward=reward, start_prob=start_prob,
                               success=success_prob,
                               duration=lf - es)
        # st.write(es, lf, lf-es)

    for subgraph in rollout_subgraphs:
        merge_nodes(G, rollout_subgraphs[subgraph], subgraph)
        for key in attrs[subgraph]:
            G.nodes[subgraph][key] = attrs[subgraph][key]

    for process in list(_new_critical_path_additions):
        critical_path.append(process)
        # st.warning(f"Adding {process}")
    return G, critical_path


def collapse_rollouts(G, critical_path, data, scenario):
    if st.checkbox("Collapse Roll-outs", True, help="Whether to collapse rolled-out subgraphs into single processes."):
        G, critical_path = get_collapsed_rollout(Cache(data=data, G=G, critical_path=critical_path), scenario)

    return G, critical_path
