#!/usr/bin/env bash
# One-off cleanup after the url_queue widening (2026-11-21). Run on db01, from the repo, as matt (not sudo):
#   git pull && ./deploy/db01/fixup.sh
# 1. gives the dashboard the proxy password so its proxy check fetches through the chain
# 2. restarts stultus-head so it picks that and the new dashboard up
# 3. scales in05, in07 and in08 up to 8 crawlers like the other nodes
# 4. prints why crawlers on in09, in10 and in12 recently exited
set -uo pipefail

if [[ $EUID -eq 0 ]]; then
    echo "Run this as matt, without sudo (ssh to the workers needs your keys)." >&2
    exit 1
fi

echo "== proxy password for the dashboard"
sudo sh -c 'grep -q "^SCRAPER_PROXY_PASSWORD=" /etc/stultus/head.env || sed -n "s/^PROXY_PASSWORD=/SCRAPER_PROXY_PASSWORD=/p" /etc/stultus/proxy.env >> /etc/stultus/head.env'
sudo grep -q "^SCRAPER_PROXY_PASSWORD=." /etc/stultus/head.env && echo "   set" || echo "   NOT set (no PROXY_PASSWORD in /etc/stultus/proxy.env?)"

echo "== restarting stultus-head"
sudo systemctl restart stultus-head
for _ in $(seq 20); do curl -fs -o /dev/null http://127.0.0.1:8000/dashboard && break; sleep 1; done
systemctl is-active stultus-head

echo "== scaling in05 in07 in08 to 8 crawlers"
for n in in05 in07 in08; do
    printf '   %s: ' "$n"
    curl -s -X POST -H 'Content-Type: application/json' -d '{"desired": 8}' "http://127.0.0.1:8000/nodes/$n/scale"
    echo
done

echo "== recent crawler exits on in09 in10 in12"
for n in in09 in10 in12; do
    echo "-- $n"
    ssh -o ConnectTimeout=5 -o BatchMode=yes "$n" \
        'journalctl -u crawler-agent --since "-15min" --no-pager | grep -E "exited code|Error|Traceback" | tail -5' \
        || echo "   unreachable"
done
echo
echo "Done. Paste the last section if it shows errors."
