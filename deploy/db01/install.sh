#!/usr/bin/env bash
# Install the db01 services as systemd units so they come back after a reboot.
# Run from anywhere on db01:  sudo ./deploy/db01/install.sh [repo_dir] [user]
# Uses the repo's virtualenv (<repo>/.env, made by the top-level install.sh) if it exists,
# otherwise /usr/bin/python3. Override with PYTHON=/path/to/python.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${1:-$(cd "$HERE/../.." && pwd)}"
RUN_USER="${2:-${SUDO_USER:-$(id -un)}}"

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
for f in /etc/stultus/head.env /etc/stultus/proxy.env; do
    [[ -f $f ]] || { echo "missing $f (see $HERE/$(basename "$f").example)" >&2; exit 1; }
    chmod 600 "$f"
done
if [[ -z ${PYTHON:-} ]]; then
    if [[ -x $REPO/.env/bin/python ]]; then PYTHON="$REPO/.env/bin/python"; else PYTHON=/usr/bin/python3; fi
fi
echo "using python: $PYTHON"

# SELinux won't let systemd exec (or follow a symlink to) a file labeled as home content,
# which is what a virtualenv under /home is. Label the venv's bin/ as executables.
venv_bin="$(dirname "$PYTHON")"
if [[ $venv_bin == /home/* ]] && command -v getenforce >/dev/null && [[ $(getenforce) != Disabled ]]; then
    command -v semanage >/dev/null || dnf install -y policycoreutils-python-utils
    semanage fcontext -a -t bin_t "$venv_bin(/.*)?" 2>/dev/null \
        || semanage fcontext -m -t bin_t "$venv_bin(/.*)?"
    restorecon -R "$venv_bin"
fi
(cd "$REPO" && sudo -u "$RUN_USER" "$PYTHON" -c "import gunicorn, flask, psycopg2, search") \
    || { echo "$PYTHON is missing packages (run as $RUN_USER: $PYTHON -m pip install gunicorn flask psycopg2-binary ...)" >&2; exit 1; }

echo "building proxy -> /usr/local/bin/stultus-proxy"
tmp="$(mktemp -d)"
chown "$RUN_USER" "$tmp"
(cd "$REPO/supporting_servers" && sudo -u "$RUN_USER" go build -o "$tmp/stultus-proxy" proxy.go)
install -m 755 "$tmp/stultus-proxy" /usr/local/bin/stultus-proxy
rm -rf "$tmp"

for unit in "$HERE"/*.service "$HERE"/*.timer; do
    sed -e "s|@REPO@|$REPO|g" -e "s|@USER@|$RUN_USER|g" -e "s|@PYTHON@|$PYTHON|g" "$unit" > "/etc/systemd/system/$(basename "$unit")"
done
systemctl daemon-reload
systemctl enable --now stultus-head stultus-proxy stultus-storage stultus-blocklist stultus-blocklist-update.timer
systemctl --no-pager --lines=0 status stultus-head stultus-proxy stultus-storage stultus-blocklist
