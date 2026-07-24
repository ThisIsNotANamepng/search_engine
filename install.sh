# Installs the Stultus crawler code
# Really just does pip install a bunch of times

python3 -m venv .env
source .env/bin/activate
pip install langdetect psycopg2-binary nltk tldextract bs4 trafilatura attrs requests redis lxml gunicorn trafilatura

