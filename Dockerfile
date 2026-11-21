# Crawler image. Built from the checked-out repo (not a git clone), so the image is
# exactly the code you build it from, and db01 can build it without reaching github.com:
#   sudo podman build --network=host -t db01:5000/stultus:$(git rev-parse --short HEAD) .
# (--network=host: the build's own container network can't get out on db01.)
#
# The python base already has python, pip, venv and CA certificates, so the build needs
# no apt: db01's network blocks outbound plain HTTP (port 80), which is all a bare
# Debian image can fetch packages over. Everything below downloads over HTTPS.
FROM docker.io/library/python:3.13-slim

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
