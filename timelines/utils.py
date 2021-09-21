import datetime
import json

import numpy as np
import streamlit as st
hours_per_attention = 40.

class Cache(object):
    def __init__(self, data, **kwargs):
        self._kwargs = dict()
        self._kwargs['data'] = data
        for key in kwargs:
            self._kwargs[key] = kwargs[key]

    @property
    def cache_hash(self):
        return self._kwargs['data']['cache_hash']

    def __getitem__(self, item):
        return self._kwargs[item]

hash_map = {Cache:lambda c: c.cache_hash}

def flush_state(save_file, data):
    with open(save_file, 'w') as f:
        data['cache_hash'] += 1
        json.dump(data, f, indent=2)


def merge_nodes(G, nodes, new_node, attr_dict=None, **attr):
    """
    Merges the selected `nodes` of the graph G into one `new_node`,
    meaning that all the edges that pointed to or from one of these
    `nodes` will point to or from the `new_node`.
    attr_dict and **attr are defined as in `G.add_node`.
    """
    G.add_node(new_node, **attr)  # Add the 'merged' node

    for n1, n2, data in list(G.edges(data=True)):
        # For all edges related to one of the nodes to merge,
        # make an edge going to or coming from the `new gene`.
        if n1 in nodes:
            G.add_edge(new_node, n2, **data)
        elif n2 in nodes:
            G.add_edge(n1, new_node, **data)
    for n in nodes:  # remove the merged nodes
        G.remove_node(n)

def fill_graph(G, data, scenario="Normal"):
    G.graph['cache_hash'] = data['cache_hash']
    G.graph['start_date'] = datetime.datetime.fromisoformat(data['start_date'])

    for process in data['processes']:
        if scenario == 'Pessimistic':
            _mod = data['processes'][process]['pessimistic_modifier']
        elif scenario == 'Optimistic':
            _mod = data['processes'][process]['optimistic_modifier']
        elif scenario == 'Normal':
            _mod = 1.
        else:
            raise ValueError(f"Invalid scenario, {scenario}")
        _duration = int(_mod * data['processes'][process]['duration']) # days
        _commitment = {key: com * hours_per_attention * _duration / 5. for key, com in data['processes'][process]['commitment'].items()}
        G.add_node(f"{process}",
                   duration=datetime.timedelta(days=_duration),
                   roles=data['processes'][process]['roles'],
                   resources=[resource for resource in data['resources'] if any([role in data['resources'][resource]['roles']
                                                                                 for role in data['processes'][process]['roles']])],
                   reward=data['processes'][process]['reward'],
                   success_prob=data['processes'][process]['success_prob'],
                   commitment=_commitment,
                   attention=data['processes'][process]['commitment'],
                   earliest_start=next_business_day(datetime.datetime.fromisoformat(data['processes'][process]['earliest_start'])),
                   delay_start=datetime.timedelta(data['processes'][process]['delay_start']),
                   done=data['processes'][process]['done'],
                   done_date=next_business_day(datetime.datetime.fromisoformat(data['processes'][process]['done_date']))
                   )
        for dep in data['processes'][process]['dependencies']:
            G.add_edge(f"{dep}", f"{process}")

def prod(x):
    if len(x) == 0:
        return 1.
    return np.prod(x)

def symbolify(text):
    nonchars = "!@#$%^&*()_-=+"
    for c in nonchars:
        text = text.replace(c," ")
    def _first_char(t):
        if t.isnumeric():
            return t
        elif t.upper() == t:
            return t
        else:
            return t.upper()[0]
    s = text.split(" ")
    s = [_first_char(t.strip()) for t in s if len(t.strip()) > 0]
    s = "".join(s)
    return s


def next_business_day(date):
    weekday = date.weekday()
    if weekday < 5:
        return date
    return date + datetime.timedelta(days=weekday % 4)

def prev_business_day(date):
    weekday = date.weekday()
    if weekday < 5:
        return date
    return date - datetime.timedelta(days=weekday % 4)


def strip_time(date:datetime.datetime):
    return datetime.datetime(year=date.year,month=date.month, day=date.day)


def add_business_days(date, days):
    output = prev_business_day(date)
    count = datetime.timedelta(days=0)
    lim = days
    oneday = datetime.timedelta(days=1)

    #start = friday, saturday
    #duration = 2 days
    #end = tuesday, tuesday

    while count < lim:
        output += oneday
        if output.weekday() < 5:
            count += oneday

    return output


def subtract_business_days(date, days):
    output = next_business_day(date)
    count = datetime.timedelta(days=0)
    lim = days
    oneday = datetime.timedelta(days=1)

    #start = friday, saturday
    #duration = 2 days
    #end = wednesday, wednesday

    while count < lim:
        output -= oneday
        if output.weekday() < 5:
            count += oneday
    return output

def test_add_subtract_business_days():
    for h in range(1,8):
        date = datetime.datetime(year=2021,month=1,day=h)
        if date.weekday()>=5:
            continue
        for d in range(0, 8):
            delta = datetime.timedelta(days=d)
            assert subtract_business_days(add_business_days(date, delta), delta) == date


def count_business_days(start, end):
    date = start
    count = 0
    while date < end:
        if date.weekday()<5:
            count += 1
        date += datetime.timedelta(days=1)
    return count





def set_prediction_data(scenario, date, G, data):
    date = strip_time(date)
    for process in G.nodes:
        __date = datetime.datetime.fromisoformat(data['processes'][process]['earliest_start'])
        __duration = data['processes'][process]['duration']
        for _date_key in data['processes'][process]['duration_dict']:
            _date = datetime.datetime.fromisoformat(_date_key)
            if (_date >= __date) and (_date <= date):
                __date = _date
                __duration = data['processes'][process]['duration_dict'][_date_key]

        __date = datetime.datetime.fromisoformat(data['processes'][process]['earliest_start'])
        __pessimistic_modifier = data['processes'][process]['pessimistic_modifier']
        for _date_key in data['processes'][process]['pessimistic_modifier_dict']:
            _date = datetime.datetime.fromisoformat(_date_key)
            if (_date >= __date) and (_date <= date):
                __date = _date
                __pessimistic_modifier = data['processes'][process]['pessimistic_modifier_dict'][_date_key]

        __date = datetime.datetime.fromisoformat(data['processes'][process]['earliest_start'])
        __optimistic_modifier = data['processes'][process]['optimistic_modifier']
        for _date_key in data['processes'][process]['optimistic_modifier_dict']:
            _date = datetime.datetime.fromisoformat(_date_key)
            if (_date >= __date) and (_date <= date):
                __date = _date
                __optimistic_modifier = data['processes'][process]['optimistic_modifier_dict'][_date_key]

        if scenario == 'Normal':
            G.nodes[process]['duration'] = datetime.timedelta(days=__duration)
        if scenario == 'Pessimistic':
            G.nodes[process]['duration'] = datetime.timedelta(days=int(__duration*__pessimistic_modifier))
        if scenario == 'Optimistic':
            G.nodes[process]['duration'] = datetime.timedelta(days=int(__duration*__optimistic_modifier))




@st.cache(show_spinner=True, suppress_st_warning=True, ttl=3600., allow_output_mutation=True, hash_funcs=hash_map)
def get_dates_of_prediction_change(cache: Cache):
    data = cache['data']
    if 'processes' not in data:
        return []
    dates = set()
    for process in data['processes']:
        for date in data['processes'][process]['duration_dict']:
            dates.add(datetime.datetime.fromisoformat(date))
        for date in data['processes'][process]['pessimistic_modifier_dict']:
            dates.add(datetime.datetime.fromisoformat(date))
        for date in data['processes'][process]['optimistic_modifier_dict']:
            dates.add(datetime.datetime.fromisoformat(date))
    return sorted(list(dates))