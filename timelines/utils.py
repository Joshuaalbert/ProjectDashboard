import datetime
import json

import numpy as np

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


hash_map = {Cache: lambda c: c.cache_hash}


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


def fill_graph(G, data, date):
    G.graph['cache_hash'] = data['cache_hash']
    G.graph['start_date'] = datetime.datetime.fromisoformat(data['start_date'])

    for process in data['processes']:
        # all sorted dates where the process had an update
        dates = sorted(map(lambda date: datetime.datetime.fromisoformat(date), data['processes'][process]['history']))
        if date < dates[0]:  # if date of simulation before oldest date, don't add this process
            continue
        # we pick the infimum of dates wrt this date, largest lower bounding date.
        key_date = None
        for i in range(len(dates)):
            if dates[i] > date:
                break
            key_date = dates[i]
        process_data = data['processes'][process]['history'][key_date.isoformat()]

        _commitment = {key: com * hours_per_attention * process_data['duration'] / 5. for key, com in
                       process_data['commitment'].items()}
        G.add_node(f"{process}",
                   duration=datetime.timedelta(days=process_data['duration']),
                   pessimistic_duration=datetime.timedelta(days=process_data['pessimistic_duration']),
                   optimistic_duration=datetime.timedelta(days=process_data['optimistic_duration']),
                   started=process_data['started'],
                   started_date=next_business_day(datetime.datetime.fromisoformat(process_data['started_date'])),
                   roles=process_data['roles'],
                   resources=[resource for resource in data['resources'] if any([role in process_data['roles']
                                                                                 for role in process_data['roles']])],
                   commitment=_commitment,
                   attention=process_data['commitment'],
                   earliest_start=next_business_day(datetime.datetime.fromisoformat(process_data['earliest_start'])),
                   start_earliest_start=process_data['start_earliest_start'],
                   delay_start=datetime.timedelta(days=process_data['delay_start']),
                   name=process_data['name']
                   )
        for dep in process_data['dependencies']:
            G.add_edge(f"{dep}", f"{process}")
    # filter undefined nodes (because of history)
    for node in list(G.nodes):
        if G.nodes[node].get('duration', None) is None:
            G.remove_node(node)


def prod(x):
    if len(x) == 0:
        return 1.
    return np.prod(x)


def symbolify(text):
    nonchars = "!@#$%^&*()_-=+"
    for c in nonchars:
        text = text.replace(c, " ")

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


def strip_time(date: datetime.datetime):
    return datetime.datetime(year=date.year, month=date.month, day=date.day)


def add_business_days(date: datetime.datetime, days: datetime.timedelta) -> datetime.datetime:
    """
    Adds one business day to the date.

    :param date:
    :param days:
    :return:
    """
    output = prev_business_day(date)
    count = datetime.timedelta(days=0)
    lim = days
    oneday = datetime.timedelta(days=1)

    # start = friday, saturday
    # duration = 2 days
    # end = tuesday, tuesday

    while count < lim:
        output += oneday
        if output.weekday() < 5:
            count += oneday

    return output


def subtract_business_days(date: datetime.datetime, days: datetime.timedelta) -> datetime.datetime:
    """
    Subtracts one business day from the date.

    :param date:
    :param days:
    :return:
    """
    output = next_business_day(date)
    count = datetime.timedelta(days=0)
    lim = days
    oneday = datetime.timedelta(days=1)

    # start = friday, saturday
    # duration = 2 days
    # end = wednesday, wednesday

    while count < lim:
        output -= oneday
        if output.weekday() < 5:
            count += oneday
    return output


def test_add_subtract_business_days():
    for h in range(1, 8):
        date = datetime.datetime(year=2021, month=1, day=h)
        if date.weekday() >= 5:
            continue
        for d in range(0, 8):
            delta = datetime.timedelta(days=d)
            assert subtract_business_days(add_business_days(date, delta), delta) == date


def count_business_days(start: datetime.datetime, end: datetime.datetime) -> int:
    """
    Count business days (exclusive) of start date.
    That is from the SOB on `start` to SOB on `end`.
    E.g. Monday to Wednesday -> 2

    :param start: inclusive start-date
    :param end: inclusive end-date
    :return:
    """
    date = start
    count = 0
    while date < end:
        if date.weekday() < 5:
            count += 1
        date += datetime.timedelta(days=1)
    return count


def get_dates_of_prediction_change(cache: Cache):
    data = cache['data']
    if 'processes' not in data:
        return []
    dates = set()
    for process in data['processes']:
        # st.write(data['processes'][process])
        for date in data['processes'][process]['history']:
            dates.add(datetime.datetime.fromisoformat(date))
    return sorted(list(dates))
