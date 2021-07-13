import datetime
import os.path

import streamlit as st
from streamlit import caching
from github import Github, Repository, Issue, NamedUser, Label
import re
import numpy as np
import pylab as plt
import base64
import networkx as nx
import json


def hash(x):
    if isinstance(x, int):
        return x
    return x.__hash__()



hash_map = {Repository.Repository: lambda x: hash(x.name),
            Issue.Issue: lambda x: hash(x.number),
            NamedUser.NamedUser: lambda x: hash(x.login),
            Label.Label: lambda x: hash(x.name),
            datetime.datetime: lambda x: hash(x.timestamp()),
            nx.Graph: lambda g: g['id']}


@st.cache(show_spinner=True, suppress_st_warning=True, ttl=3600., allow_output_mutation=True, hash_funcs=hash_map)
def get_issues(repo, label, assignee="*"):
    if not isinstance(label, (list, tuple)):
        label = [label]
    return repo.get_issues(state='all', labels=label, assignee=assignee)

@st.cache(show_spinner=True, suppress_st_warning=True, ttl=3600., allow_output_mutation=True, hash_funcs=hash_map)
def search_issues(repo, labels, assignees):
    issues = {}
    for assignee in assignees:
        for label in labels:
            _issues = get_issues(repo, label, assignee)
            for issue in _issues:
                if issue.number not in issues:
                    issues[issue.number] = issue
    return issues


@st.cache(show_spinner=True, suppress_st_warning=True, ttl=3600., allow_output_mutation=True, hash_funcs=hash_map)
def get_events(issue: Issue.Issue):
    return issue.get_events()


@st.cache(show_spinner=True, suppress_st_warning=True, ttl=3600., allow_output_mutation=True, hash_funcs=hash_map)
def get_teams(repo):
    teams = {team.name: team.get_members() for team in repo.get_teams()}
    return teams


@st.cache(show_spinner=True, suppress_st_warning=True, ttl=3600., allow_output_mutation=True, hash_funcs=hash_map)
def get_labels(repo: Repository.Repository):
    return repo.get_labels()


def is_issue_match(issue, regex):
    for label in issue['labels']:
        if re.match(regex, label.name) is not None:
            return True
    return False


# @st.cache(show_spinner=True, suppress_st_warning=True, ttl=3600., allow_output_mutation=True, hash_funcs=hash_map)
def get_story_points(issue, storypoint_regex, container=None):
    for label in issue.labels:
        match = re.match(storypoint_regex, label.name)
        if match is not None:
            return float(match.group(1))
    if container is not None:
        with container:
            st.warning(f"[#{issue.number}]({issue.html_url}) {issue.title} has no story points!")
    return None


def label_in_labels(label, labels):
    return label in [label.name for label in labels]


def get_table_download_link(save_file):
    """Generates a link allowing the data in a given panda dataframe to be downloaded
    in:  dataframe
    out: href string
    """
    with open(save_file, 'r') as f:
        val = "\n".join(f.readlines())

    b64 = base64.b64encode(val.encode())  # .decode()
    # b64 = base64.b64encode(val)  # val looks like b'...'
    return f'<a href="data:application/octet-stream;base64,{b64.decode()}" download="{save_file}.json">Download Data File</a>'  # decode b'abc' => abc



def user_assigned_for_interval(intervals, start, end):
    """
    Determine if user was assigned at `end`.

    :param intervals:
    :param start:
    :param end:
    :return:
    """
    for assigned, interval_start in intervals:
        if end <= interval_start:
            return assigned



class EventLog(object):
    def __init__(self):
        self.intervals = []
        self.b = None
        self.a = None

    def __call__(self, v):
        if self.b is None:
            self.b = v
            return
        self.intervals.append((v, self.b))
        self.b = None

def serialise_graph(G:nx.DiGraph):
    data = dict(id=G['id'],
                nodes=list(G.nodes),
                edges=list(G.edges),
                subgraphs=list(G['subgraphs']))
    return data

def deserialise_graph(data):
    G = nx.DiGraph(id=data['id'], subgraphs=data['subgraphs'])
    G.add_nodes_from(data['nodes'])
    G.add_edges_from(data['nodes'])
    return G

def save_data(data, save_file):
    with open(save_file, 'w') as f:
        json.dump(data, f)

def load_data(data_file):
    if os.path.isfile(data_file):
        with open(data_file, 'r') as f:
            data = json.load(f)
    else:
        data = dict(id=0, subgraphs=[],nodes=[],edges=[], token="")
        save_data(data, data_file)
    return data

def get_start_time(issue):
    start_time = None
    for event in get_events(issue):
        if event.event == 'labeled':
            if event.label.name == 'in_progress':
                if start_time is None:
                    start_time = event.created_at
                else:
                    start_time = min(event.created_at, start_time)
    return start_time


def get_label_story_points(repo, issues, storypoint_regex, use_story_points=True):
    """
    Get the stats for an label.

    :param repo:
    :param label:
    :return:
        total_story_points, complete_story_points, start_time
    """
    total_story_points = 0
    complete_story_points = 0
    start_time = None
    for issue in issues:
        if use_story_points:
            story_points = get_story_points(issue, storypoint_regex)
            if story_points is None:
                st.warning("Falling back to open/closed definition of story points...")
                return get_label_story_points(repo, issues, storypoint_regex, use_story_points=False)
        else:
            story_points = 1
        total_story_points += story_points
        if start_time is None:
            start_time = get_start_time(issue)
        else:
            start_time = min(start_time, issue.created_at)
        if issue.state == 'closed':
            complete_story_points += story_points
    return total_story_points, complete_story_points, start_time

def plot_label_healthbar(repo, issues, storypoint_regex):
    total_story_points, complete_story_points, start_time = get_label_story_points(repo, issues, storypoint_regex)
    completeness = complete_story_points/total_story_points if total_story_points > 0 else 1.
    if completeness >= 0.:
        color = 'red'
    if completeness > 0.33:
        color = 'yellow'
    if completeness > 0.66:
        color = 'green'
    width = 30
    done_width = int(completeness * width)
    not_done_width = width - done_width
    st.markdown(f"Completeness: |{'#'*done_width}{'-'*not_done_width}| {round(completeness*100,1)}%")

    if start_time is not None:
        dt = datetime.datetime.now() - start_time
        rate = completeness / dt.total_seconds()
    else:
        rate = 0.
    if rate == 0:
        time_left = "NaN"
    else:
        time_left = (1. - completeness)/rate/86400./7
    st.markdown(f"Time remaining: {time_left} weeks")


def render_data():
    if st.sidebar.button("Refresh GitHub Data"):
        caching.clear_cache()

    data_file = st.text_input("Data file: ", "save_file.json", help="JSON file to store information in.")

    files = st.file_uploader("Upload data file", type=['json'], accept_multiple_files=True)
    if files is not None:
        data = dict()
        for file in files:
            _data = json.load(file)
            data.update(_data)
        save_data(data, data_file)

    data = load_data(data_file)

    st.markdown(get_table_download_link(data_file), unsafe_allow_html=True)

    token = st.sidebar.text_input("Github token: ", help="A github token giving you read access to the repo.")

    repo_name = st.sidebar.text_input("Repo :", 'Touch-Physio/Touch-Meta', help="Repo in format `owner/repo`")

    g = Github(token)
    repo = g.get_repo(repo_name)

    epic_regex = st.sidebar.text_input("Epic Regex (use <title> for title placeholder):", "EP - <title>",
                                       help="Pattern used to designate label labels.")
    epic_regex = epic_regex.replace("<title>", "(.+?)")

    storypoint_regex = st.sidebar.text_input("Story Point Regex (use <value> for value placeholder):", "<value>SPs")
    storypoint_regex = storypoint_regex.replace("<value>", "(.+?)")

    render_report(data, data_file, repo, epic_regex, storypoint_regex)

def safe_index(list, item):
    if item not in list:
        return 0
    return list.index(item)


def get_storypoints_on_date(issue:Issue.Issue, storypoint_regex, date):
    events = list(get_events(issue))
    labels = set()
    _date = issue.created_at
    for event in events:
        if event.created_at > date:
            break
        if event.event == 'labeled':
            labels.add(event.label.name)
        if event.event == 'unlabeled':
            labels = labels - {event.label.name}

    for label in labels:
        match = re.match(storypoint_regex, label)
        if match is not None:
            return float(match.group(1))
    st.warning(f"[#{issue.number}]({issue.html_url}) {issue.title} has no story points!")
    return None

def same_day(date1:datetime.datetime, date2:datetime.datetime):
    date1 = datetime.datetime(year=date1.year, month=date1.month, day=date1.day)
    date2 = datetime.datetime(year=date2.year, month=date2.month, day=date2.day)
    return date1 == date2

@st.cache(show_spinner=True, suppress_st_warning=True, ttl=3600., allow_output_mutation=True, hash_funcs=hash_map)
def was_labeled_on_date(issue:Issue.Issue, label, date):
    events = list(get_events(issue))
    was_labeled = False
    _date = issue.created_at
    for event in events:
        if not same_day(event.created_at, date):
            continue
        if event.event == 'labeled':
            if event.label.name == label:
                was_labeled = True
                break
    return was_labeled


@st.cache(show_spinner=True, suppress_st_warning=True, ttl=3600., allow_output_mutation=True, hash_funcs=hash_map)
def has_label_on_date(issue:Issue.Issue, label, date):
    events = list(get_events(issue))
    labels = set()
    _date = issue.created_at
    for event in events:
        if event.created_at > date:
            break
        if event.event == 'labeled':
            labels.add(event.label.name)
        if event.event == 'unlabeled':
            labels = labels - {event.label.name}
    if isinstance(label, list):
        return any([lab in labels for lab in label])
    return label in labels

@st.cache(show_spinner=True, suppress_st_warning=True, ttl=3600., allow_output_mutation=True, hash_funcs=hash_map)
def has_assignee_on_date(issue:Issue.Issue, assignee, date):
    events = list(get_events(issue))
    assignees = set()
    _date = issue.created_at
    for event in events:
        if event.created_at > date:
            break
        if event.event == 'assigned':
            assignees.add(event.assignee.login)
        if event.event == 'unassigned':
            assignees = assignees - {event.assignee.login}
    return assignee.login in assignees

@st.cache(show_spinner=True, suppress_st_warning=True, ttl=3600., allow_output_mutation=True, hash_funcs=hash_map)
def is_closed_on_date(issue:Issue.Issue, date):
    events = list(get_events(issue))
    closed = False
    _date = issue.created_at
    for event in events:
        if event.created_at > date:
            break
        if event.event == 'closed':
            closed = True
    return closed




def render_report(data, data_file, repo, epic_regex, storypoint_regex):
    repo_labels = get_labels(repo)
    repo_label_names = [lab.name for lab in repo_labels]

    user_story_label = st.sidebar.selectbox("User story label: ",
                                            repo_label_names,
                                            safe_index(repo_label_names, 'user_story'),
                                            help='What labels signifies the user story?')

    bug_label = st.sidebar.selectbox("Bug label: ",
                                            repo_label_names,
                                            safe_index(repo_label_names, 'bug'),
                                            help='What labels signifies the bug?')

    default_dev_tickets = ['frontend', 'backend']
    dev_ticket_labels = st.sidebar.multiselect("Dev ticket labels: ", repo_label_names,
                                         [lab for lab in default_dev_tickets if lab in repo_label_names],
                                         help="What labels signifies dev work?")



    epics_labels = list(filter(lambda label: re.match(epic_regex, label.name) is not None, repo_labels))
    epics_to_report = st.sidebar.multiselect("Which Epics to report on: ", epics_labels, [])
    if len(epics_to_report) == 0:
        epics_to_report = epics_labels

    default_tracking_labels = ['backlog', 'in_progress', 'blocked', 'Blocker', 'testing', 'awaiting_deploy']
    default_tracking_labels = list(filter(lambda x: x in repo_label_names, default_tracking_labels))
    tracking_labels = st.sidebar.multiselect("Tracking Labels: ", repo_label_names, default_tracking_labels)
    tracking_labels = list(filter(lambda x: x.name in tracking_labels, repo_labels))
    tracking_label_names = [lab.name for lab in tracking_labels]

    color_map = {label.name: label.color for label in repo_labels}
    teams = get_teams(repo)
    st.header("GitHub Teams")
    with st.beta_container():
        for team, members in teams.items():
            with st.beta_expander(f"{team}"):
                for member in members:
                    st.markdown(f" - {member.name} -> {member.login}")

    users = set()
    for team in teams:
        for member in teams[team]:
            users.add(member)
    users = list(users)
    user_logins = [user.login for user in users]
    users_to_report = st.sidebar.multiselect("Users to report on: ", user_logins, [])
    if len(users_to_report) == 0:
        users_to_report = user_logins
    users_to_report = list(filter(lambda x: x.login in users_to_report, users))

    report_start_date = st.sidebar.date_input("Report Start Date: ",
                                              datetime.datetime.now() - datetime.timedelta(days=7.),
                                              max_value=datetime.datetime.now(),
                                              help="Date to report from (inclusive).")
    report_start_date = datetime.datetime(year=report_start_date.year, month=report_start_date.month,
                                          day=report_start_date.day)
    report_end_date = st.sidebar.date_input("Report End Date: ", datetime.datetime.now(),
                                            min_value=report_start_date,
                                            max_value=datetime.datetime.now(),
                                            help="Date to report to (exclusive).")
    report_end_date = datetime.datetime(year=report_end_date.year, month=report_end_date.month, day=report_end_date.day)

    st.header("Missing story points (action needed!)")
    with st.beta_expander(f"User stories missing story points:"):
        user_story_missing_container = st.beta_container()

    with st.beta_expander(f"Bugs missing story points:"):
        bugs_missing_container = st.beta_container()

    with st.beta_expander(f"Dev tickets missing story points:"):
        dev_tickets_missing_container = st.beta_container()

    run_report = st.sidebar.button("Run Report")
    # for each epic print numbers of bugs, user stories, and dev tickets that have been created and closed during this period.
    st.header("Report per Epic")
    if run_report and (len(epics_to_report) > 0):
        all_user_stories = []
        all_bugs = []
        all_dev_tickets = []
        for epic in epics_to_report:
            with st.beta_expander(f"{epic.name}"):
                # over all health
                user_story_issues = list(get_issues(repo, [epic, user_story_label]))
                bug_issues = list(get_issues(repo, [epic, bug_label]))
                dev_issues = sum([list(get_issues(repo, [epic, dev_ticket_label])) for dev_ticket_label in dev_ticket_labels], [])
                all_user_stories += user_story_issues
                all_bugs += bug_issues
                all_dev_tickets += dev_issues

                for issues, name in zip([user_story_issues, bug_issues, dev_issues], ['user stories', 'bugs', 'dev tickets']):
                    opened_issues = list(filter(lambda issue: (issue.created_at >= report_start_date) and (issue.created_at < report_end_date), issues))
                    num_opened = len(opened_issues)
                    st.subheader(f"{num_opened} opened {name}")
                    for issue in opened_issues:
                        st.markdown(f" - [#{issue.number}]({issue.html_url}) {issue.title}")

                    closed_issues = list(filter(lambda issue: issue.state == 'closed', issues))
                    closed_issues = list(filter(
                        lambda issue: (issue.closed_at >= report_start_date) and (issue.closed_at < report_end_date),
                        closed_issues))
                    num_closed = len(closed_issues)
                    st.subheader(f"{num_closed} closed {name}")

                    devs = set()
                    for issue in closed_issues:
                        st.markdown(f" - [#{issue.number}]({issue.html_url}) {issue.title}")
                        devs = devs.union(set(issue.assignees))
                    story_points_per_dev = {assignee.login: sum(filter(lambda s: s is not None, map(lambda issue: get_story_points(issue, storypoint_regex),
                                                                    filter(lambda issue: assignee in issue.assignees, closed_issues))))
                                            for assignee in list(devs)}
                    for dev in story_points_per_dev.keys():
                        st.markdown(f" - [x] {dev} closed {story_points_per_dev[dev]} story points of {name}.")



                ### issues that are newly created and put on the backlog
                fig, ax = plt.subplots(1, 1, figsize=(8, 8))
                dates = []
                story_points = []
                _date = report_start_date
                while _date < report_end_date:
                    filtered_issues = list(filter(
                        lambda issue: was_labeled_on_date(issue, 'backlog', _date) and not is_closed_on_date(issue,
                                                                                                             _date),
                        user_story_issues))
                    _story_points = sum(filter(lambda x: x is not None, [
                        get_story_points(issue, storypoint_regex) for issue
                        in
                        filtered_issues]))
                    story_points.append(_story_points)
                    dates.append(_date)
                    _date += datetime.timedelta(days=1.)

                ax.plot(dates, story_points, c=f"#{color_map[user_story_label]}", label="User story curation", lw=2.)

                dates = []
                story_points = []
                _date = report_start_date
                while _date < report_end_date:
                    filtered_issues = list(filter(
                        lambda issue: was_labeled_on_date(issue, 'backlog', _date) and not is_closed_on_date(issue,
                                                                                                             _date),
                        dev_issues))
                    _story_points = sum(filter(lambda x: x is not None, [
                        get_story_points(issue, storypoint_regex) for issue
                        in
                        filtered_issues]))
                    story_points.append(_story_points)
                    dates.append(_date)
                    _date += datetime.timedelta(days=1.)

                ax.plot(dates, story_points, c=f"#{color_map[dev_ticket_labels[0]]}", label="Dev ticket curation", lw=2.)

                dates = []
                story_points = []
                _date = report_start_date
                while _date < report_end_date:
                    filtered_issues = list(filter(
                        lambda issue: was_labeled_on_date(issue, 'backlog', _date) and not is_closed_on_date(issue,
                                                                                                             _date),
                        bug_issues))
                    _story_points = sum(filter(lambda x: x is not None, [
                        get_story_points(issue, storypoint_regex) for issue
                        in
                        filtered_issues]))
                    story_points.append(_story_points)
                    dates.append(_date)
                    _date += datetime.timedelta(days=1.)

                ax.set_title(f"Curation of {epic.name} (Story Points added to backlog)")
                ax.plot(dates, story_points, c=f"#{color_map[bug_label]}", label="Bug curation", lw=2.)
                ax.legend()
                st.write(fig)


        st.header("Aggregated report")
        [get_story_points(issue, storypoint_regex,container=user_story_missing_container) for issue in all_user_stories]
        [get_story_points(issue, storypoint_regex,container=bugs_missing_container) for issue in all_bugs]
        [get_story_points(issue, storypoint_regex,container=dev_tickets_missing_container) for issue in all_dev_tickets]

        for issues, name in zip([all_user_stories, all_bugs, all_dev_tickets], ['user stories', 'bugs', 'dev tickets']):

            closed_issues = list(filter(lambda issue: issue.state == 'closed', issues))
            closed_issues = list(filter(
                lambda issue: (issue.closed_at >= report_start_date) and (issue.closed_at < report_end_date),
                closed_issues))
            num_closed = len(closed_issues)
            st.subheader(f"{num_closed} closed {name}")

            devs = set()
            for issue in closed_issues:
                st.markdown(f" - [#{issue.number}]({issue.html_url}) {issue.title}")
                devs = devs.union(set(issue.assignees))
            story_points_per_dev = {assignee.login: sum(
                filter(lambda s: s is not None, map(lambda issue: get_story_points(issue, storypoint_regex),
                                                    filter(lambda issue: assignee in issue.assignees, closed_issues))))
                                    for assignee in list(devs)}
            for dev in story_points_per_dev.keys():
                st.markdown(f" - [x] {dev} closed {story_points_per_dev[dev]} story points of {name}.")

        fig, ax = plt.subplots(1, 1,figsize=(8,8))
        totals = None
        for tracking_label in tracking_label_names:
            dates = []
            story_points = []
            _date = report_start_date
            while _date < report_end_date:
                filtered_issues = list(filter(
                    lambda issue: has_label_on_date(issue, tracking_label, _date) and not is_closed_on_date(issue, _date) , all_user_stories))
                _story_points = sum(filter(lambda x: x is not None, [get_story_points(issue, storypoint_regex) for issue in filtered_issues]))
                story_points.append(_story_points)
                dates.append(_date)
                _date += datetime.timedelta(days=1.)
            if totals is None:
                totals = story_points
            else:
                totals = [s + t for s,t in zip(story_points, totals)]
            if any([s > 0 for s in story_points]):
                ax.plot(dates, story_points, c=f"#{color_map[tracking_label]}", label=tracking_label)

        ax.set_title("User story burn-down")
        ax.set_ylabel("Story points in state")
        ax.plot(dates, totals, c=f"black", label="Total", lw=2.)
        ax.legend()
        st.write(fig)

        fig, ax = plt.subplots(1, 1, figsize=(8, 8))
        totals = None
        for tracking_label in tracking_label_names:
            dates = []
            story_points = []
            _date = report_start_date
            while _date < report_end_date:
                filtered_issues = list(filter(
                    lambda issue: has_label_on_date(issue, tracking_label, _date) and not is_closed_on_date(
                        issue, _date), all_bugs))
                _story_points = sum(filter(lambda x: x is not None,
                                           [get_story_points(issue, storypoint_regex) for issue in
                                            filtered_issues]))
                story_points.append(_story_points)
                dates.append(_date)
                _date += datetime.timedelta(days=1.)
            if totals is None:
                totals = story_points
            else:
                totals = [s + t for s,t in zip(story_points, totals)]
            if any([s > 0 for s in story_points]):
                ax.plot(dates, story_points, c=f"#{color_map[tracking_label]}", label=tracking_label)
        ax.set_title("Bug burn-down")
        ax.set_ylabel("Story points in state")

        ax.plot(dates, totals, c=f"black", label="Total", lw=2.)
        ax.legend()
        st.write(fig)

        fig, ax = plt.subplots(1, 1, figsize=(8, 8))
        totals = None
        for tracking_label in tracking_label_names:
            dates = []
            story_points = []
            _date = report_start_date
            while _date < report_end_date:
                filtered_issues = list(filter(
                    lambda issue: has_label_on_date(issue, tracking_label, _date) and not is_closed_on_date(
                        issue, _date), all_dev_tickets))
                _story_points = sum(filter(lambda x: x is not None,
                                           [get_story_points(issue, storypoint_regex) for issue in
                                            filtered_issues]))
                story_points.append(_story_points)
                dates.append(_date)
                _date += datetime.timedelta(days=1.)
            if totals is None:
                totals = story_points
            else:
                totals = [s + t for s, t in zip(story_points, totals)]
            if any([s > 0 for s in story_points]):
                ax.plot(dates, story_points, c=f"#{color_map[tracking_label]}", label=tracking_label)
        ax.set_title("Dev Tickets burn-down")
        ax.set_ylabel("Story points in state")

        ax.plot(dates, totals, c=f"black", label="Total", lw=2.)
        ax.legend()
        st.write(fig)

        ### burndowns
        all_issues = all_user_stories + all_bugs + all_dev_tickets

        st.header("Per user story boards")
        for user in users_to_report:
            fig, ax = plt.subplots(1, 1, figsize=(8, 8))
            user_issues = list(filter(lambda issue: user.login in [u.login for u in issue.assignees], all_issues))
            with st.beta_expander(f"Story board for {user.login}"):
                st.subheader("Closed during the report period")
                closed_issues = list(filter(lambda issue: issue.state == 'closed', user_issues))
                closed_issues = list(filter(
                    lambda issue: (issue.closed_at >= report_start_date) and (issue.closed_at < report_end_date),
                    closed_issues))
                num_closed = len(closed_issues)
                st.markdown(f"{num_closed} closed")

                for issue in closed_issues:
                    st.markdown(f" - [#{issue.number}]({issue.html_url}) {issue.title} ({get_story_points(issue, storypoint_regex)} SPs)")

                st.subheader("Tickets still open after report period")
                open_issues = list(filter(lambda issue: issue.state == 'open', user_issues))
                open_issues = list(filter(
                    lambda issue: has_assignee_on_date(issue, user, report_end_date),
                    open_issues))
                num_closed = len(open_issues)
                st.markdown(f"{num_closed} still open")

                for issue in open_issues:
                    st.markdown(
                        f" - [#{issue.number}]({issue.html_url}) {issue.title} ({get_story_points(issue, storypoint_regex)} SPs)")


                st.subheader("Story board")
                _story_board_issues = closed_issues + open_issues
                for bar_idx, issue in enumerate(_story_board_issues):
                    events = list(get_events(issue))
                    current_labels = dict()
                    xranges = []
                    colors = []
                    yrange = (bar_idx, 1)
                    for tracking_label in tracking_label_names:
                        for event in events:
                            if event.created_at > report_end_date:
                                break
                            if event.event == 'labeled':
                                if event.label.name == tracking_label:
                                    current_labels[event.label.name] = event.created_at
                            if event.event == 'unlabeled':
                                if event.label.name == tracking_label:
                                    xranges.append((current_labels[event.label.name], event.created_at - current_labels[event.label.name]))
                                    colors.append(f"#{color_map[tracking_label]}")
                                    del current_labels[event.label.name]
                    for label in current_labels.keys():
                        xranges.append((current_labels[label], report_end_date - current_labels[label]))
                        colors.append(f"#{color_map[label]}")
                    if issue.state is 'closed':
                        _xranges = []
                        _colors = []
                        for (x_start, x_len), color in zip(xranges, colors):
                            if x_start <= issue.closed_at:
                                _end = min(issue.closed_at, x_start + x_len)
                                _xranges.append((x_start, _end - x_start))
                                _colors.append(color)
                        xranges = _xranges
                        colors = _colors
                    ax.broken_barh(xranges, yrange, color=colors, edgecolor='black', alpha=1.)
                    if issue.state == 'closed':
                        ax.scatter(issue.closed_at, bar_idx+0.5, c='black', s=100, marker="o")
                ax.legend(loc='lower left')
                ax.set_xlim(report_start_date, report_end_date)
                ax.set_yticks(np.arange(len(_story_board_issues)) + 0.5)
                ax.set_yticklabels(
                    [f"#{issue.number}-{get_story_points(issue, storypoint_regex)}SPs" for issue in _story_board_issues],
                    rotation=0)
                plt.setp(ax.get_xticklabels(), Rotation=30, horizontalalignment='right')
                ax.set_title(
                    f"Story board for {', '.join([user.login for user in [user]])}\n({', '.join([lab.name for lab in epics_to_report])})")
                plt.tight_layout()
                st.write(fig)
