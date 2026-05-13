FROM continuumio/miniconda3

RUN apt-get update && apt-get install -y graphviz



RUN conda update -n base -c defaults conda
RUN conda create -n app_env python=3.8

SHELL ["/bin/bash", "--login", "-c"]
RUN conda init bash
RUN echo "conda activate app_env" > ~/.bashrc

RUN pip install -r requirements.txt



ADD . /Dashboard
WORKDIR /Dashboard
ENTRYPOINT ["conda", "run", "--no-capture-output", "-n", "app_env", "bash", "main.sh"]
