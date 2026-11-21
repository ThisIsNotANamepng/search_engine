# Installs the Stultus crawler code
# Really just does pip install (requirements.txt is the one list of what the code needs)

python3 -m venv .env
source .env/bin/activate
pip install -r requirements.txt

