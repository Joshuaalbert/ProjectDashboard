import datetime
import json
import re
from copy import deepcopy

import numpy as np
import streamlit


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

def fill_graph(G, data, collapse_rollouts=False):
    G.graph['cache_hash'] = data['cache_hash']



    for process in data['processes']:
        G.add_node(f"{process}",
                   duration=datetime.timedelta(days=data['processes'][process]['duration']),
                   roles=data['processes'][process]['roles'],
                   resources=[resource for resource in data['resources'] if any([role in data['resources'][resource]['roles']
                                                                                 for role in data['processes'][process]['roles']])],
                   reward=data['processes'][process]['reward'],
                   success_prob=data['processes'][process]['success_prob'],
                   commitment=data['processes'][process]['commitment'],
                   earliest_start=next_business_day(datetime.datetime.fromisoformat(data['processes'][process]['earliest_start'])),
                   delay_start=datetime.timedelta(data['processes'][process]['delay_start'])
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

if __name__ == '__main__':
    test_add_subtract_business_days()