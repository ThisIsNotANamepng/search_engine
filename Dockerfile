# Crawler image. Built from the checked-out repo (not a git clone), so the image is
# exactly the code you build it from, and db01 can build it without reaching github.com:
#   sudo podman build --network=host -t db01:5000/stultus:$(git rev-parse --short HEAD) .
# (--network=host: the build's own container network can't get out on db01.)
FROM debian:stable-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv ca-certificates curl nano \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first, so code-only changes reuse this (slow) layer.
COPY requirements.txt /search_engine/requirements.txt
RUN python3 -m venv /env && /env/bin/pip install --no-cache-dir -r /search_engine/requirements.txt

RUN /env/bin/python3 -m nltk.downloader -d /usr/share/nltk_data punkt_tab stopwords
ENV NLTK_DATA=/usr/share/nltk_data

# Apparently tldextract gets its data from publicsuffix.org, so we need to have it download its cache when building the container
RUN /env/bin/python3 -c "import tldextract; tldextract.extract('http://example.com')"

COPY . /search_engine
WORKDIR /search_engine

CMD ["/env/bin/python3", "scrape.py"]
