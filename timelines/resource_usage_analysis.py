import datetime

import networkx as nx
import numpy as np
import pylab as plt
from mpl_toolkits.axes_grid1 import make_axes_locatable
import streamlit as st
from graphviz import Digraph as graphviz_graph

from .utils import count_business_days, add_business_days

# make a color map of fixed colors
cmap = plt.cm.colors.ListedColormap(['tab:cyan','tab:blue','tab:green','tab:olive','orange','red','pink','lime'])
bounds = [0, 2., 4., 6., 8., 10., 15., 20., 100.]
norm = plt.cm.colors.BoundaryNorm(bounds, cmap.N)
hours_per_attention = 40.

def add_colorbar_to_axes(ax, label):
    """
    Add colorbar to axes easily.

    Args:
        ax: Axes
        cmap: str or cmap
        norm: Normalize or None
        vmin: lower limit of color if norm is None
        vmax: upper limit of color if norm is None
    """
    divider = make_axes_locatable(ax)
    cax = divider.append_axes('right', size='5%', pad=0.05)
    sm = plt.cm.ScalarMappable(norm, cmap=cmap)
    ax.figure.colorbar(sm,label=label ,cax=cax, orientation='vertical')

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

hash_map = {Cache:lambda c: c.cache_hash, np.ndarray: lambda x: np.sum(x)}

def hours_distibution(start_date, end_date, es, ls, duration):
    num_days = (end_date - start_date).days
    h = []
    # st.write(es, ls, duration)
    for start_day in range((ls-es).days+1):
        count = 0
        date = es + datetime.timedelta(days=start_day)
        # st.write(start_day, date)
        while count < duration.days:
            idx = (date - start_date).days
            h.append(idx)
            date = add_business_days(date, datetime.timedelta(days=1))
            count += 1

    if len(h) > 0:
        weights = np.bincount(np.asarray(h),minlength=num_days)
        # st.write(h, weights, np.sum(weights), np.min(h), np.max(h))
        weights = weights / np.sum(weights)
    else:
        weights = np.zeros(num_days)

    return weights

def compute_hours_per_role(G, data, use_weighted_hours):
    # construct per-role requirements over time
    roles = data['roles']
    num_per_role = {role: 0 for role in roles}
    for resource in data['resources']:
        for role in data['resources'][resource]['roles']:
            num_per_role[role] += 1

    num_roles = len(roles)
    start_date = min([G.nodes[node]['ES'] for node in G.nodes], default=datetime.datetime.now())
    end_date = max([G.nodes[node]['LF'] for node in G.nodes], default=datetime.datetime.now())
    diff_date = end_date - start_date
    num_days = diff_date.days
    hours = np.zeros((num_roles, num_days))
    reward = np.zeros(num_days)
    total_commitment = 0.
    order = []
    for bar_idx, process in enumerate(nx.topological_sort(G)):
        order.append(process)

        density = hours_distibution(start_date, end_date,
                                    G.nodes[process]['ES'], G.nodes[process]['LS'], G.nodes[process]['duration'])

        for role in G.nodes[process]['roles']:
            commitment = G.nodes[process]['commitment'][role]*hours_per_attention
            total_commitment += commitment
            reward = G.nodes[process]['reward']
            idx = roles.index(role)
            if use_weighted_hours:
                hours[idx, :] += G.nodes[process]['start_prob'] * density * commitment
            else:
                hours[idx, :] += density * commitment

        if use_weighted_hours:
            reward += G.nodes[process]['success'] * density * reward
        else:
            reward += density * reward
    return hours, reward

@st.cache(show_spinner=True, suppress_st_warning=True, ttl=3600., allow_output_mutation=True, hash_funcs=hash_map)
def get_hour_stats(c, use_weighted_hours, scenario):
    data = c['data']
    G = c['G']
    hours_per_role, reward = compute_hours_per_role(G, data, use_weighted_hours)
    hours_per_resource = compute_hours_per_resource(hours_per_role, data)
    cost_per_resource = compute_cost_per_resource(G, data, hours_per_resource)
    return hours_per_role, hours_per_resource, cost_per_resource, reward

@st.cache(show_spinner=True, suppress_st_warning=True, ttl=3600., allow_output_mutation=True, hash_funcs=hash_map)
def get_usage_figs(c, hours_per_role, hours_per_resource, display_resources, scenario):
    G = c['G']
    G_collapsed = c['G_collapsed']
    data = c['data']
    critical_path_collapsed = c['critical_path_collapsed']

    fig, axs = plt.subplots(3, 1, figsize=(12, 28), sharex=True)

    plot_gantt_chart(G_collapsed, critical_path_collapsed, display_resources, axs[0])

    plot_role_usage(G, data, hours_per_role, axs[1])

    plot_resource_usage(G, data, hours_per_resource, axs[2])

    add_colorbar_to_axes(axs[1], "hours / b.day / res.")
    add_colorbar_to_axes(axs[2], "hours / b.day")

    return fig


def display_usage(G, critical_path, data, G_collapsed=None, critical_path_collapsed=None, scenario=None):
    if st.checkbox("Compute resource requirements"):
        use_weighted_hours = st.checkbox("Display probability weighted resource usage.", False,
                                         help="Whether to compute the expected resource usage based on probability of being able to perform process.")
        display_resources = st.multiselect("Gantt chart only some resources? ", list(data['resources']), [],
                                       help="Whether to GANTT chart certain resources.")

        if len(display_resources)==0:
            display_resources = list(data['resources'])


        hours_per_role, hours_per_resource, cost_per_resource, reward = get_hour_stats(Cache( data=data, G=G),use_weighted_hours, scenario)

        if G_collapsed is None:
            G_collapsed = G

        if critical_path_collapsed is None:
            critical_path_collapsed = critical_path

        fig = get_usage_figs(Cache(G_collapsed=G_collapsed, G=G, data=data,
                                   critical_path_collapsed=critical_path_collapsed),
                             hours_per_role,
                             hours_per_resource,
                             display_resources,
                             scenario
                             )

        # fig, axs = plt.subplots(3, 1, figsize=(12, 28), sharex=True)
        #
        # plot_gantt_chart(G_collapsed, critical_path_collapsed, axs[0])
        #
        # plot_role_usage(G, data, hours_per_role, axs[1])
        #
        # plot_resource_usage(G, data, hours_per_resource, axs[2])

        st.write(fig)

        plot_costs_per_resource(G, data, cost_per_resource, reward, scenario)

def compute_cost_per_resource(G, data, hours_per_resource):
    start_date = min([G.nodes[node]['ES'] for node in G.nodes], default=datetime.datetime.now())
    end_date = max([G.nodes[node]['LF'] for node in G.nodes], default=datetime.datetime.now())
    diff_date = end_date - start_date
    num_days = diff_date.days
    num_weeks = num_days/7.
    cost_per_resource = dict()
    for resource in data['resources']:
        if not data['resources'][resource]['cost_per_week']:
            _cost = hours_per_resource[resource] * data['resources'][resource]['cost']
        else:
            total_cost = num_weeks * data['resources'][resource]['cost']
            _cost = np.ones_like(hours_per_resource[resource]) * total_cost / num_days
        cost_per_resource[resource] = _cost
    return cost_per_resource



def compute_hours_per_resource(hours_per_role, data):
    num_roles, num_days = hours_per_role.shape
    roles = data['roles']
    num_per_role = {role: 0 for role in roles}
    for resource in data['resources']:
        for role in data['resources'][resource]['roles']:
            num_per_role[role] += 1
    hours_per_resource = dict()
    for bar_idx, resource in enumerate(data['resources']):
        _bar = sum([hours_per_role[roles.index(role), :] / num_per_role[role]
                    for role in data['resources'][resource]['roles']],
                   np.zeros(num_days))
        hours_per_resource[resource] = _bar
    return hours_per_resource

def plot_resource_usage(G, data, hours_per_resource, ax):


    resources = sorted(list(data['resources']))
    # st.write(np.stack([hours_per_resource[res] for res in resources], axis=0), resources)

    start_date = min([G.nodes[node]['ES'] for node in G.nodes], default=datetime.datetime.now())
    for bar_idx, resource in enumerate(resources):
        _bar = hours_per_resource[resource]
        xranges = []
        facecolors = []
        annotations = []
        for _start_range, _end_range in get_breaks(_bar):
            xranges.append((start_date + datetime.timedelta(days=_start_range), datetime.timedelta(days=_end_range - _start_range)))
            hour_req = _bar[_start_range]
            if np.isnan(hour_req):
                hour_req = 0
            annotations.append(f"{round(hour_req, 1)}")  # hr / b.day / res.
            facecolors.append(colour_hours(hour_req))

        yrange = (bar_idx, 1)
        ax.broken_barh(xranges,
                       yrange,
                       facecolors=facecolors,
                       edgecolor=None,
                       alpha=0.75)
        # for (x, w), annotation in zip(xranges, annotations):
        #     ax.text(x=x + w / 2,
        #             y=bar_idx + 0.5,
        #             s=annotation,
        #             ha='center',
        #             va='center',
        #             color='black',
        #             )
    ax.grid()
    ax.axvline(datetime.datetime.now(), c='black', lw=3.,alpha=0.75, label='Now')
    ax.legend(loc='lower right')
    ax.set_yticks(np.arange(len(resources)) + 0.5)
    ax.set_yticklabels(resources, rotation=0)
    ax.set_title("Hours per resource")
    plt.tight_layout()

def colour_hours(t):
    return cmap(norm(np.clip(t,0., 100)))

def get_breaks(a):
    changes = np.concatenate([np.asarray([0]), np.diff(a)])
    breaks = np.where(changes != 0)[0]
    breaks = np.concatenate([breaks, np.asarray([a.size])])
    _start_range = 0
    for i in breaks:
        i = i.item()
        yield (_start_range, i)
        _start_range = i

def plot_role_usage(G, data, hours_per_role, ax):
    # construct per-role requirements over time
    roles = data['roles']
    num_per_role = {role: 0 for role in roles}
    for resource in data['resources']:
        for role in data['resources'][resource]['roles']:
            num_per_role[role] += 1
    start_date = min([G.nodes[node]['ES'] for node in G.nodes], default=datetime.datetime.fromisoformat(data['start_date']))
    for bar_idx, role in enumerate(roles):
        xranges = []
        facecolors = []
        annotations = []
        for _start_range, _end_range in get_breaks(hours_per_role[bar_idx, :]):
            xranges.append(
                (start_date + datetime.timedelta(days=_start_range), datetime.timedelta(days=_end_range - _start_range)))
            hour_req = hours_per_role[bar_idx, _start_range] / num_per_role[role]
            if np.isnan(hour_req):
                hour_req = 0.
            annotations.append(f"{round(hour_req, 1)}")  # hr / b.day / res.
            facecolors.append(colour_hours(hour_req))
        yrange = (bar_idx, 1)
        ax.broken_barh(xranges,
                       yrange,
                       facecolors=facecolors,
                       edgecolor=None,
                       alpha=0.75)
        # for (x, w), annotation in zip(xranges, annotations):
        #     ax.text(x=x + w / 2,
        #             y=bar_idx + 0.5,
        #             s=annotation,
        #             ha='center',
        #             va='center',
        #             color='black',
        #             )
    ax.grid()
    ax.axvline(datetime.datetime.now(), c='black', lw=3.,alpha=0.75, label='Now')
    ax.legend(loc='lower right')
    ax.set_yticks(np.arange(len(roles)) + 0.5)
    ax.set_yticklabels(roles, rotation=0)
    ax.set_title("Hours per role")
    plt.tight_layout()


def plot_gantt_chart(G, critical_path, display_resources, ax):
    if len(display_resources) > 0:
        resource_nodes = [node for node in G.nodes if (any([resource in G.nodes[node]['resources'] for resource in display_resources]) or (len(G.nodes[node]['roles']) == 0))]
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
                       (G.nodes[process]['LS'], G.nodes[process]['EF'] - G.nodes[process]['LS']),
                       (G.nodes[process]['EF'], G.nodes[process]['LF'] - G.nodes[process]['EF'])]

            ax.broken_barh(xranges,
                           yrange,
                           facecolors=('green', 'blue', 'green'),
                           edgecolor='black',
                           alpha=0.5)
    ax.grid()
    ax.axvline(datetime.datetime.now(), c='black', lw=3.,alpha=0.75, label='Now')
    ax.legend(loc='lower right')
    ax.set_yticks(np.arange(len(order)) + 0.5)
    ax.set_yticklabels(order, rotation=0)
    plt.tight_layout()


def display_graph(G, critical_path, data, scenario):
    if st.checkbox("Display graph", False):

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

def plot_costs_per_resource(G, data, cost_per_resource, rewards, scenario):
    start_date = min([G.nodes[node]['ES'] for node in G.nodes], default=datetime.datetime.fromisoformat(data['start_date']))
    end_date = max([G.nodes[node]['LF'] for node in G.nodes], default=datetime.datetime.fromisoformat(data['start_date']))
    num_days = (end_date - start_date).days
    time = [start_date + datetime.timedelta(days=i) for i in range(num_days)]
    fig, ax = plt.subplots(1,1, figsize=(8,6))
    total_cost = np.zeros(num_days)-rewards
    cum_cost_per_resource = {resource:np.cumsum(cost_per_resource[resource]) for resource in data['resources']}
    vmin = min([cum_cost_per_resource[resource][-1] for resource in data['resources']], default=0)
    vmax = max([cum_cost_per_resource[resource][-1] for resource in data['resources']], default=0)
    for resource in sorted(data['resources'],
                           key=lambda res:cum_cost_per_resource[res][-1],
                           reverse=True):
        ax.plot(time, cum_cost_per_resource[resource],
                c=plt.cm.jet(plt.Normalize(vmin, vmax)(cum_cost_per_resource[resource][-1])),
                label=f"{resource}")
        total_cost += cost_per_resource[resource]

    # ax.plot(time, np.cumsum(total_cost), label=f"Total", lw=3., c='black')

    ax.legend()
    ax.set_xlabel("Time")
    ax.set_ylabel("Cumulative cost ($)")
    plt.tight_layout()
    st.write(fig)
