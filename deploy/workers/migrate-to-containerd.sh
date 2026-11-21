#!/usr/bin/env bash
# Move a crawler worker from k3s's bundled containerd to plain containerd.
# Safe to re-run. Run as root on the worker:
#   sudo bash migrate-to-containerd.sh /tmp/containerd.io-*.rpm [/tmp/container-selinux-*.rpm]
#
# The workers can't reach the internet, so copy the RPMs over from db01 first.
# containerd.io needs container-selinux. k3s pulled it in as a dependency, so the k3s
# uninstaller removes it as "unused" unless it is marked as wanted first (done below).
# If it's already gone, pass a container-selinux RPM that matches the worker's
# selinux-policy version (the latest one needs a newer policy than the workers have).
set -euo pipefail

[[ $# -ge 1 ]] || { echo "usage: $0 containerd.io-*.rpm [container-selinux-*.rpm]" >&2; exit 1; }
[[ $EUID -eq 0 ]] || { echo "run as root (sudo)" >&2; exit 1; }
NS="${CTR_NAMESPACE:-stultus}"
UNIT=/etc/systemd/system/crawler-agent.service
ENV_FILE=/etc/stultus/crawler.env

systemctl stop crawler-agent 2>/dev/null || true

# 1. Remove k3s (also stops its leftover containers and network interfaces).
#    Keep container-selinux: containerd.io needs it, and the k3s uninstaller would
#    otherwise remove it along with k3s-selinux.
rpm -q container-selinux >/dev/null 2>&1 && dnf mark install container-selinux >/dev/null
if [[ -x /usr/local/bin/k3s-agent-uninstall.sh ]]; then
    /usr/local/bin/k3s-agent-uninstall.sh
fi

# 2. A copy of the k3s binary was placed at /usr/bin/ctr; it talks to k3s's socket.
if [[ -e /usr/bin/ctr ]] && ! rpm -qf /usr/bin/ctr >/dev/null 2>&1; then
    mv /usr/bin/ctr /root/ctr.k3s-copy
fi

# 3. Install and start containerd.
rpm -q containerd.io >/dev/null 2>&1 || dnf install -y --disablerepo='*' "$@"
systemctl enable --now containerd
ctr version >/dev/null

# 4. Point the node agent at the new containerd.
sed -i 's/^After=k3s\.service k3s-agent\.service /After=containerd.service /' "$UNIT"
grep -q '^Wants=containerd\.service' "$UNIT" || sed -i '/^After=containerd\.service/a Wants=containerd.service' "$UNIT"
sed -i '/^CTR_BIN=/d' "$ENV_FILE"
echo 'CTR_BIN=/usr/bin/ctr' >> "$ENV_FILE"
systemctl daemon-reload

# 5. Clear crawler containers/snapshots left behind by earlier agent runs
#    (otherwise ctr run fails with 'snapshot "stultus-<node>-N": already exists').
for c in $(ctr -n "$NS" containers ls -q); do
    ctr -n "$NS" tasks kill -s KILL "$c" 2>/dev/null || true
    ctr -n "$NS" tasks delete --force "$c" 2>/dev/null || true
    ctr -n "$NS" containers rm "$c" 2>/dev/null || true
done
for s in $(ctr -n "$NS" snapshots ls | awk 'NR>1 && $1 ~ /^stultus-/ {print $1}'); do
    ctr -n "$NS" snapshots rm "$s" 2>/dev/null || true
done

systemctl start crawler-agent

echo "== $(hostname): done"
grep -E '^(After|Wants)=' "$UNIT"
echo "ctr: $(rpm -qf /usr/bin/ctr)"
