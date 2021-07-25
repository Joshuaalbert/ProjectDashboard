import streamlit as st
from git_work.git_work import render_data
from timelines.components import render_components



def main():
    which = st.sidebar.radio("Choose view: ", ['Project Planner', 'Operations Report'], index=0,help='Choose which view to show.')

    if which == 'Project Planner':
        render_components()
    elif which == 'Operations Report':
        render_data()



if __name__ == '__main__':
    main()
