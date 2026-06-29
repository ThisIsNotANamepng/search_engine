"""
Serves blocklist.db for download by scrapers
"""

import os
from flask import Flask, send_file, abort

BLOCKLIST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "blocklist.db")

app = Flask(__name__)


@app.route("/blocklist.db")
def download_blocklist():
    if not os.path.exists(BLOCKLIST_PATH):
        abort(404)
    return send_file(
        BLOCKLIST_PATH,
        mimetype="application/octet-stream",
        as_attachment=True,
        download_name="blocklist.db",
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8002)
