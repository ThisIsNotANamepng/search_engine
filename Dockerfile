FROM debian:stable-slim

RUN apt update && apt install -y \
    git python3 python3-pip python3-venv nano curl \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 https://github.com/ThisIsNotANamepng/search_engine.git

RUN python3 -m venv /env
RUN /env/bin/pip install -r search_engine/requirements.txt

RUN python -m nltk.downloader -d /usr/share/nltk_data punkt_tab stopwords
ENV NLTK_DATA=/usr/share/nltk_data

WORKDIR /search_engine

CMD ["/env/bin/python3", "scrape.py"]
