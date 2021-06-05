import datetime

import streamlit as st
from streamlit import caching
from github import Github, Repository, Issue, NamedUser, Label
import re
import numpy as np
import pylab as plt


def hash(x):
    if isinstance(x, int):
        return x
    return x.__hash__()


hash_map = {Repository.Repository: lambda x: hash(x.name),
            Issue.Issue: lambda x: hash(x.number),
            NamedUser.NamedUser: lambda x: hash(x.login),
            Label.Label: lambda x: hash(x.name)}


@st.cache(show_spinner=True, suppress_st_warning=True, ttl=3600., allow_output_mutation=True, hash_funcs=hash_map)
def get_issues(repo):
    issues = [dict(issue=issue, events=issue.get_events(), labels=issue.get_labels(), assignees=issue.assignees)
              for issue in repo.get_issues(state='all')]
    return issues


@st.cache(show_spinner=True, suppress_st_warning=True, ttl=3600., allow_output_mutation=True, hash_funcs=hash_map)
def search_issues(repo, labels, assignees):
    issues = {}
    for assignee in assignees:
        for label in labels:
            _issues = repo.get_issues(state='all', labels=[label], assignee=assignee)
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


def story_points(issue, storypoint_regex):
    for label in issue.labels:
        match = re.match(storypoint_regex, label.name)
        if match is not None:
            return float(match.group(1))
    st.warning(f"[#{issue.number}]({issue.html_url}) {issue.title} has no story points!")
    return None


def label_in_labels(label, labels):
    return label in [label.name for label in labels]


def render_data():
    _refresh = 0

    if st.sidebar.button("Refresh GitHub Data"):
        caching.clear_cache()

    token = st.sidebar.text_input("Github token: ", help="A github token giving you read access to the repo.")

    repo_name = st.sidebar.text_input("Repo :", 'Touch-Physio/Touch-Meta', help="Repo in format `owner/repo`")

    g = Github(token)
    repo = g.get_repo(repo_name)

    epic_regex = st.sidebar.text_input("Epic Regex (use <title> for title placeholder):", "EP - <title>")
    epic_regex = epic_regex.replace("<title>", "(.+?)")

    storypoint_regex = st.sidebar.text_input("Story Point Regex (use <value> for value placeholder):", "<value>SPs")
    storypoint_regex = storypoint_regex.replace("<value>", "(.+?)")

    render_report(repo, epic_regex, storypoint_regex)


def render_report(repo, epic_regex, storypoint_regex):
    repo_labels = get_labels(repo)
    repo_label_names = [lab.name for lab in repo_labels]
    epics_labels = list(filter(lambda label: re.match(epic_regex, label.name) is not None, repo_labels))
    epics_to_report = st.sidebar.multiselect("Which Epics to report on: ", epics_labels, [])
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
    users_to_report = list(filter(lambda x: x.login in users_to_report, users))
    report_start_date = st.sidebar.date_input("Report Start Date: ",
                                              datetime.datetime.now() - datetime.timedelta(days=7.),
                                              max_value=datetime.datetime.now())
    report_start_date = datetime.datetime(year=report_start_date.year, month=report_start_date.month,
                                          day=report_start_date.day)
    report_end_date = st.sidebar.date_input("Report End Date: ", datetime.datetime.now(),
                                            min_value=report_start_date,
                                            max_value=datetime.datetime.now())
    report_end_date = datetime.datetime(year=report_end_date.year, month=report_end_date.month, day=report_end_date.day)
    if st.sidebar.button("Report") and (len(users_to_report) > 0) and (len(epics_to_report) > 0):
        issues_to_report = search_issues(repo, epics_to_report, users_to_report)

        fig, ax = plt.subplots(1, 1, figsize=(8, 8))
        # nodes
        label_color_described = {label: False for label in tracking_label_names}
        label_color_described['closed'] = False
        for position, issue_number in enumerate(issues_to_report):
            closed = issues_to_report[issue_number].state == 'closed'
            if closed:
                closed_at = issues_to_report[issue_number].closed_at
            events = get_events(issues_to_report[issue_number])
            # for each tracking label get the sequence of label and unlabels
            event_log = {label: [] for label in tracking_label_names}
            for event in events:
                if (event.event == 'labeled') or (event.event == 'unlabeled'):
                    if event.label.name in tracking_label_names:
                        if closed:
                            # event_log[event.label.name].append(min([event.created_at, closed_at]))
                            event_log[event.label.name].append(event.created_at)
                        else:
                            event_log[event.label.name].append(event.created_at)
            for label in tracking_label_names:
                xranges = []
                if (len(event_log[label]) % 2 == 1):
                    # give end of block date (not real unlabeling)
                    event_log[label].append(report_end_date)
                for block_idx in range(0, len(event_log[label]), 2):
                    if event_log[label][block_idx + 1] > report_end_date:
                        continue
                    if event_log[label][block_idx + 1] < report_start_date:
                        continue
                    if closed:
                        event_log[label][block_idx + 1] = min([event_log[label][block_idx + 1], closed_at])
                        pass
                    xranges.append(
                        (event_log[label][block_idx], event_log[label][block_idx + 1] - event_log[label][block_idx]))
                yrange = (position, 1)
                if label_color_described[label]:
                    ax.broken_barh(xranges, yrange, color=f"#{color_map[label]}", edgecolor='black', alpha=1.)
                else:
                    label_color_described[label] = True
                    ax.broken_barh(xranges, yrange, color=f"#{color_map[label]}", edgecolor='black', alpha=1.,
                                   label=label)
                if closed:
                    if label_color_described['closed']:
                        ax.scatter(closed_at, position + 0.5, marker='o', c='black', s=50)
                    else:
                        label_color_described['closed'] = True
                        ax.scatter(closed_at, position + 0.5, marker='o', c='black', s=50, label='Closed')
        ax.legend(loc='lower left')
        ax.set_xlim(report_start_date, report_end_date)
        ax.set_yticks(np.arange(len(issues_to_report)) + 0.5)
        ax.set_yticklabels(
            [f"#{id}-{story_points(issues_to_report[id], storypoint_regex)}SP" for id in issues_to_report], rotation=0)
        ax.set_title(
            f"Story board for {', '.join([user.login for user in users_to_report])}\n({', '.join([lab.name for lab in epics_to_report])})")
        plt.tight_layout()
        st.write(fig)
