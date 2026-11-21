#!/usr/bin/env bash
# Point the head's proxy (db01:8888, what the crawlers fetch through) at the VPS proxy made by deploy/vps/install_proxy.sh,
# then rebuild and restart it.
#
# Run on db01 from the repo:
#   sudo ./deploy/db01/connect_vps_proxy.sh <vps_public_ip_or_name> <vps_cert.crt> [port]
#
# Keeps PROXY_PASSWORD from /etc/stultus/proxy.env. PROXY_PASSWORD=... sets a new one; it must then also be
# SCRAPER_PROXY_PASSWORD on every worker. The old env file is kept as /etc/stultus/proxy.env.bak.
set -euo pipefail

usage="usage: sudo $0 <vps_public_ip_or_name> <vps_cert.crt> [port]"
VPS=${1:?$usage}
CERT_IN=${2:?$usage}
PORT=${3:-8443}
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
ENV_FILE=/etc/stultus/proxy.env
CERT=/etc/stultus/vps-proxy.crt

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
command -v go >/dev/null || { echo "go is missing (dnf install golang)" >&2; exit 1; }
go_minor="$(go env GOVERSION | sed -E 's/^go1\.([0-9]+).*/\1/')"
(( go_minor >= 18 )) || { echo "proxy.go needs Go 1.18 or newer, found $(go env GOVERSION)" >&2; exit 1; }
openssl x509 -in "$CERT_IN" -noout 2>/dev/null || { echo "$CERT_IN is not a PEM certificate" >&2; exit 1; }

if [[ -z ${PROXY_PASSWORD:-} && -f $ENV_FILE ]]; then
    PROXY_PASSWORD="$(sed -n 's/^PROXY_PASSWORD=//p' "$ENV_FILE" | tail -1)"
fi
[[ -n ${PROXY_PASSWORD:-} && $PROXY_PASSWORD != CHANGE_ME ]] \
    || { echo "no PROXY_PASSWORD in $ENV_FILE: pass PROXY_PASSWORD=... (the VPS's password)" >&2; exit 1; }

host=$VPS; [[ $VPS == *:* ]] && host="[$VPS]"  # an IPv6 address needs brackets in a URL

echo "== checking the VPS proxy directly"
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 --cacert "$CERT_IN" \
    -H @<(printf 'X-Proxy-Auth: %s\n' "$PROXY_PASSWORD") "https://$host:$PORT/fetch?url=https://example.com/" || true)"
case $code in
    200) echo "ok" ;;
    401) echo "the VPS rejected the password: use the one in the VPS's /etc/stultus/proxy.env" >&2; exit 1 ;;
    000) echo "can't reach https://$host:$PORT (VPS firewall / cloud security group / wrong address or cert?)" >&2; exit 1 ;;
    *)   echo "the VPS answered HTTP $code: check journalctl -u stultus-proxy on the VPS" >&2; exit 1 ;;
esac
# Crawlers going through the proxy leave the "public address only" check to the VPS, so it must have it
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 --cacert "$CERT_IN" \
    -H @<(printf 'X-Proxy-Auth: %s\n' "$PROXY_PASSWORD") "https://$host:$PORT/fetch?url=http://127.0.0.1:22/" || true)"
[[ $code == 403 ]] || { echo "the VPS runs an old proxy.go (an internal address got HTTP $code, want 403): rerun install_proxy.sh on the VPS, it pulls the latest code" >&2; exit 1; }

echo "== testing and building proxy -> /usr/local/bin/stultus-proxy"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
(cd "$REPO/supporting_servers" && export GOCACHE="$tmp/cache" && go test -count=1 proxy.go proxy_test.go && go build -o "$tmp/stultus-proxy" proxy.go)
install -m 755 "$tmp/stultus-proxy" /usr/local/bin/stultus-proxy

mkdir -p /etc/stultus
install -m 644 "$CERT_IN" "$CERT"
old=""
if [[ -f $ENV_FILE ]]; then
    cp -p "$ENV_FILE" "$ENV_FILE.bak"
    old="$(grep -vE '^(PROXY_UPSTREAM|PROXY_PASSWORD|PROXY_TLS_INSECURE|SSL_CERT_FILE)=' "$ENV_FILE.bak" || true)"
fi
# SSL_CERT_FILE: trust only the VPS's own certificate (Go reads it). This proxy dials nothing but the VPS,
# so it needs no other roots, and no PROXY_TLS_INSECURE, which would let anyone in the middle read the password.
{
    [[ -n $old ]] && printf '%s\n' "$old"
    echo "PROXY_UPSTREAM=https://$host:$PORT"
    echo "PROXY_PASSWORD=$PROXY_PASSWORD"
    echo "SSL_CERT_FILE=$CERT"
} > "$ENV_FILE"
grep -q '^PROXY_ADDR=' "$ENV_FILE" || echo "PROXY_ADDR=:8888" >> "$ENV_FILE"
chmod 600 "$ENV_FILE"

install -m 644 "$HERE/stultus-proxy.service" /etc/systemd/system/stultus-proxy.service
systemctl daemon-reload
systemctl enable stultus-proxy
systemctl restart stultus-proxy

echo "== checking the whole chain: db01:8888 -> VPS -> https://example.com/"
port_a="$(sed -n 's/^PROXY_ADDR=.*://p' "$ENV_FILE" | tail -1)"
code=000
for _ in $(seq 20); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 \
        -H @<(printf 'X-Proxy-Auth: %s\n' "$PROXY_PASSWORD") "http://127.0.0.1:${port_a:-8888}/fetch?url=https://example.com/" || true)"
    [[ $code == 200 ]] && break
    sleep 0.5
done
if [[ $code != 200 ]]; then
    echo "chain check failed (HTTP $code). Logs: journalctl -u stultus-proxy -n 50" >&2
    exit 1
fi
echo "ok. Crawlers use SCRAPER_PROXY=http://db01:${port_a:-8888} with SCRAPER_PROXY_PASSWORD set to the same password."
