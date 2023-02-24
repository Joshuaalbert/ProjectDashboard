# ProjectDashboard
Dashboard for planning and tracking complex projects.

Deployed as an app at [streamlit](https://joshuaalbert-projectdashboard-app-dd3fbu.streamlit.app/). **Note** this app is public and not secure. Your project data is stored behind a specific project state file name that you choose. If you want a secure instance, see below how to run your own instance.

## Why you should consider using this
1. Allows you to plan out a project ahead of time.
2. Allows you to quantitatively track slippage and delivery dates.
3. Allows you to understand at a glance what you _should_ be doing.
4. Provides a source of truth for understanding changes in projects over their life.

## Main Functionality
1. Enables creation of complex process diagrams with advanced dependencies options
2. Performs critical path analysis
3. Tracks changes in your expectations of deliverables
4. Shows you how your expectations and planned deliverables changed over the course of the project

## Advanced functionality
1. Create and assign roles and people resources to each process step
2. Reveals resource contention and bottle necks

## To run your own instance

```
sudo docker build . -t project_dashboard
sudo docker run -dp 8502:8501 project_dashboard

# observe the app running at localhost:8502
```


## Screenshots

### Easily define complex processes, with advanced dependency options, and isolate critical paths.
![Screenshot from 2023-02-23 21-17-29](https://user-images.githubusercontent.com/14807032/221076007-1623b01c-dd78-4ace-9503-570c3db178a9.png)

### See at a glance what you should be doing, and how your completion timelines have changed over time
![Screenshot from 2023-02-23 21-18-42](https://user-images.githubusercontent.com/14807032/221076140-8f99f5c8-9129-4302-9a2c-c04b473bdb1c.png)

