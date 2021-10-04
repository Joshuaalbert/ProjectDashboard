import datetime

import networkx as nx
import numpy as np
import pylab as plt
import streamlit as st

from .utils import Cache, hash_map

from .utils import add_business_days, subtract_business_days, count_business_days, next_business_day, strip_time, \
    fill_graph

class CPM(nx.DiGraph):

    def __init__(self, method='single', num_particles=100):
        super().__init__()
        self._dirty = True
        self._attention_contraint = False
        self._critical_path_length = -1
        self._criticalPath = None
        assert method in ['single', 'stochastic']
        self._method = method
        self._num_particles = num_particles

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
            # required information:
            # start date & time remaining
            # start date & duration
            # earliest finish is by adding duration to es
            # if ef is specified, then duration
            if self._method == 'single':
                duration = self.nodes[n]['duration']
            elif self._method == 'stochastic':
                duration = datetime.timedelta(days=np.random.uniform(self.nodes[n]['optimistic_duration'].days,
                                                                     self.nodes[n]['pessimistic_duration'].days))
            else:
                raise ValueError(f"{self._method} invalid.")

            if self.nodes[n]['started']:
                es = ls = self.nodes[n]['started_date']
                ef = lf = add_business_days(es, duration)
                self.add_node(n,
                              ES=es,
                              LS=ls,
                              EF=ef,
                              LF=lf,
                              total_float=datetime.timedelta(days=0),
                              _duration=duration)
            else:
                es = max([self.nodes[j]['EF'] for j in self.predecessors(n)], default=start_time)
                extra_constraints = []
                if self.nodes[n]['earliest_start'] is not None:
                    extra_constraints.append(self.nodes[n]['earliest_start'])
                if self.nodes[n]['delay_start'] is not None:
                    extra_constraints.append(add_business_days(es, self.nodes[n]['delay_start']))
                # extend earliest start to the maximum of the constraints
                es = max([es]+extra_constraints)
                ef = add_business_days(es, duration)
                self.add_node(n,
                              ES=es,
                              EF=ef,
                              _duration=duration)

    def _backward(self):
        for n in reversed(list(nx.topological_sort(self))):
            if not self.nodes[n]['started']:
                lf = min([self.nodes[j]['LS'] for j in self.successors(n)], default=self._critical_path_end)
                ls = subtract_business_days(lf,self.nodes[n]['_duration'])
                self.add_node(n,
                              LS=ls,
                              LF=lf,
                              total_float=datetime.timedelta(days=count_business_days(self.nodes[n]['ES'], lf)) - self.nodes[n]['_duration'])

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
        if self._method == 'single':
            return self._critical_path_length
        elif self._method == 'stochastic':
            return self._stochastic_critical_path_length

    @property
    def critical_path_end(self):
        if self._dirty:
            self._update()
        if self._method == 'single':
            return self._critical_path_end
        elif self._method == 'stochastic':
            return self._stochastic_critical_path_end


    @property
    def critical_path(self):
        if self._dirty:
            self._update()
        if self._method == 'single':
            return sorted(self._criticalPath, key=lambda x: self.nodes[x]['ES'])
        elif self._method == 'stochastic':
            return [(n, np.mean(d['total_float'])) for n, d in self._stochastic_results.items()]

    def _update(self):
        if self._method == 'single':
            self._forward()
            self._critical_path_end = max(nx.get_node_attributes(self, 'EF').values(), default=datetime.timedelta(0))
            self._critical_path_length = max(nx.get_node_attributes(self, 'EF').values(), default=datetime.timedelta(0)) - min(nx.get_node_attributes(self, 'ES').values(), default=datetime.timedelta(0))
            self._backward()
            self._compute_critical_path()
            self._dirty = False
        elif self._method == 'stochastic':
            stochastic_results = dict()
            stochastic_critical_path_end = []
            stochastic_critical_path_length = []
            for _ in range(self._num_particles):
                self._forward()
                self._critical_path_end = max(nx.get_node_attributes(self, 'EF').values(), default=datetime.timedelta(0))
                self._critical_path_length = max(nx.get_node_attributes(self, 'EF').values(), default=datetime.timedelta(0)) - min(nx.get_node_attributes(self, 'ES').values(), default=datetime.timedelta(0))
                self._backward()
                self._compute_critical_path()
                for n in self.nodes:
                    if n not in stochastic_results:
                        stochastic_results[n] = dict(ES=[],EF=[],LS=[],LF=[],total_float=[])
                    for key in stochastic_results[n]:
                        stochastic_results[n][key].append(self.nodes[n][key])
                stochastic_critical_path_end.append(self._critical_path_end)
                stochastic_critical_path_length.append(self._critical_path_length)
            self._stochastic_results = stochastic_results
            self._stochastic_critical_path_end = stochastic_critical_path_end
            self._stochastic_critical_path_length = stochastic_critical_path_length
            self._dirty = False

@st.cache(show_spinner=True, suppress_st_warning=True, ttl=3600., allow_output_mutation=True, hash_funcs=hash_map)
def get_critical_path(cache: Cache, date_of_change, termination_nodes=None):
    data = cache['data']
    G = CPM()
    fill_graph(G, data, date_of_change)
    # compute_event_probabilities(G)
    if termination_nodes is not None:
        if not isinstance(termination_nodes, (tuple, list)):
            termination_nodes = [termination_nodes]
        ancestors = set()
        for source in termination_nodes:
            _ancestors = nx.algorithms.ancestors(G, source)
            ancestors = ancestors.union(_ancestors)
        for node in list(G.nodes):
            if node not in ancestors:
                G.remove_node(node)
    critical_path = G.critical_path
    return G, critical_path


def render_critical_path(data, date_of_change):

    if st.checkbox("Display critical path", False):
        # display_resources = st.multiselect("Gantt chart only some resources? ", list(data['resources']), [],
        #                                    help="Whether to GANTT chart certain resources.")

        termination_nodes = st.multiselect("Termination points: ", data['processes'], [],
                                           help='What nodes to compute up until, else all.')

        if len(termination_nodes) == 0:
            termination_nodes = None

        G, critical_path = get_critical_path(Cache(data=data), date_of_change, termination_nodes)
        plot_gantt_chart(G, critical_path, [])


def plot_gantt_chart(G, critical_path, display_resources):
    fig, ax = plt.subplots(1, 1, figsize=(12, 28//3))

    if len(display_resources) > 0:
        # resource_nodes = [node for node in G.nodes if (any([resource in G.nodes[node]['resources'] for resource in display_resources]) or (len(G.nodes[node]['roles']) == 0))]
        resource_nodes = list(filter(lambda node: any([resource in G.nodes[node]['resources'] for resource in display_resources]), G.nodes))
    else:
        resource_nodes = list(G.nodes)
    order = []
    for bar_idx, process in enumerate(filter(lambda node: node in resource_nodes, nx.topological_sort(G))):
        order.append(process)

        if process in critical_path:
            start_day = G.nodes[process]['ES']
            end_day = G.nodes[process]['LF']
            xranges = [(start_day, end_day - start_day)]
            yrange = (bar_idx, 1)

            ax.broken_barh(xranges,
                           yrange,
                           facecolors='red',
                           edgecolor='black',
                           alpha=0.75)
        else:
            yrange = (bar_idx, 1)
            xranges = [(G.nodes[process]['ES'], G.nodes[process]['LS'] - G.nodes[process]['ES']),
                       (G.nodes[process]['EF'], G.nodes[process]['LF'] - G.nodes[process]['EF']),
                       (G.nodes[process]['LS'], G.nodes[process]['EF'] - G.nodes[process]['LS'])]

            ax.broken_barh(xranges,
                           yrange,
                           facecolors=('green', 'blue', 'yellow'),
                           edgecolor='black',
                           alpha=0.5)
    ax.grid()
    ax.axvline(datetime.datetime.now(), c='black', lw=3.,alpha=0.75, label='Now')
    ax.legend(loc='lower right')
    ax.set_yticks(np.arange(len(order)) + 0.5)
    ax.set_yticklabels(order, rotation=0)
    plt.tight_layout()

    st.write(fig)