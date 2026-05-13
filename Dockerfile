FROM continuumio/miniconda3

RUN apt-get update && apt-get install -y graphviz libgraphviz-dev pkg-config

ADD . /Dashboard
WORKDIR /Dashboard

RUN conda create -n app_env python=3.12 -y
RUN conda run -n app_env pip install -e .

ENTRYPOINT ["conda", "run", "--no-capture-output", "-n", "app_env", "bash", "main.sh"]
