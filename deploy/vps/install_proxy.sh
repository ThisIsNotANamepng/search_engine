#!/usr/bin/env bash
# Set up the cloud end of the proxy chain on a fresh VPS: the proxy that makes the real outbound requests.
# The head's proxy (db01:8888) forwards every crawler fetch here over HTTPS; see deploy/db01/connect_vps_proxy.sh.
#
# Run on the VPS (it fetches the proxy code itself, from the ai-supported branch on GitHub):
#   curl -fsSL https://raw.githubusercontent.com/ThisIsNotANamepng/search_engine/ai-supported/deploy/vps/install_proxy.sh \
#     | sudo PROXY_PASSWORD=<PROXY_PASSWORD from db01:/etc/stultus/proxy.env> bash -s -- <vps_public_ip_or_name> [port]
#
# Rerun it to update: it pulls the branch's latest commit and rebuilds. BRANCH=... builds another branch.
# Without PROXY_PASSWORD it reuses the one from an earlier run, or makes a new one.
# The port is open to anyone; the password guards it. A cloud provider firewall (security group) must allow it.
# Safe to run again; delete /etc/stultus/proxy.crt first to get a new certificate (then rerun the head script too).
set -euo pipefail

usage="usage: sudo PROXY_PASSWORD=... $0 <vps_public_ip_or_name> [port]"
NAME=${1:?$usage}
PORT=${2:-8443}
REPO_URL=https://github.com/ThisIsNotANamepng/search_engine.git
BRANCH=${BRANCH:-ai-supported}
SRC=/opt/stultus/search_engine
ENV_FILE=/etc/stultus/proxy.env
CRT=/etc/stultus/proxy.crt
KEY=/etc/stultus/proxy.key

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }

echo "== packages"
if command -v apt-get >/dev/null; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -q
    apt-get install -yq golang-go git openssl curl
elif command -v dnf >/dev/null; then
    dnf install -y golang git openssl curl
else
    echo "unsupported distro: needs apt-get or dnf" >&2; exit 1
fi

go_minor="$(go env GOVERSION | sed -E 's/^go1\.([0-9]+).*/\1/')"
(( ${go_minor:-0} >= 18 )) || { echo "proxy.go needs Go 1.18 or newer, this distro has $(go version): install one from https://go.dev/dl/" >&2; exit 1; }

echo "== getting the code: $BRANCH of $REPO_URL -> $SRC"
if [[ ! -d $SRC/.git ]]; then
    # Shallow, and only supporting_servers/ checked out: the rest of the repo (docs images, test texts) isn't needed here
    git clone -q --depth 1 --filter=blob:none --sparse --branch "$BRANCH" "$REPO_URL" "$SRC"
    git -C "$SRC" sparse-checkout set supporting_servers
else
    git -C "$SRC" fetch -q --depth 1 origin "$BRANCH"
    git -C "$SRC" reset -q --hard FETCH_HEAD  # this checkout is only ever written by this script
fi
echo "building $(git -C "$SRC" log -1 --format='%h %s')"

echo "== testing and building proxy -> /usr/local/bin/stultus-proxy"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
(cd "$SRC/supporting_servers" && export GOCACHE="$tmp/cache" && go test -count=1 proxy.go proxy_test.go && go build -o "$tmp/stultus-proxy" proxy.go)
install -m 755 "$tmp/stultus-proxy" /usr/local/bin/stultus-proxy

id stultus-proxy >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin stultus-proxy
mkdir -p /etc/stultus

if [[ ! -f $CRT ]]; then
    echo "== making a self-signed certificate for $NAME"
    if [[ $NAME =~ ^[0-9.]+$ || $NAME == *:* ]]; then san="IP:$NAME"; else san="DNS:$NAME"; fi
    openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 3650 \
        -subj "/CN=$NAME" -addext "subjectAltName=$san" -keyout "$KEY" -out "$CRT" 2>/dev/null
fi
chown root:stultus-proxy "$KEY"; chmod 640 "$KEY"; chmod 644 "$CRT"

new_password=""
if [[ -z ${PROXY_PASSWORD:-} && -f $ENV_FILE ]]; then
    PROXY_PASSWORD="$(sed -n 's/^PROXY_PASSWORD=//p' "$ENV_FILE" | tail -1)"
fi
if [[ -z ${PROXY_PASSWORD:-} ]]; then
    PROXY_PASSWORD="$(openssl rand -hex 32)"
    new_password=1
fi

# No PROXY_UPSTREAM: this is the last hop, it fetches the pages itself
cat > "$ENV_FILE" <<EOF
PROXY_ADDR=:$PORT
PROXY_PASSWORD=$PROXY_PASSWORD
PROXY_TLS_CERT=$CRT
PROXY_TLS_KEY=$KEY
EOF
chmod 600 "$ENV_FILE"

cat > /etc/systemd/system/stultus-proxy.service <<'EOF'
[Unit]
Description=Stultus proxy (cloud end)
After=network-online.target
Wants=network-online.target

[Service]
User=stultus-proxy
# Lets it listen on a port below 1024 (443) without running as root
AmbientCapabilities=CAP_NET_BIND_SERVICE
EnvironmentFile=/etc/stultus/proxy.env
ExecStart=/usr/local/bin/stultus-proxy
Restart=always
RestartSec=3
LimitNOFILE=65536
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable stultus-proxy
systemctl restart stultus-proxy

# We never turn a firewall on, but if the image came with one running (Rocky's firewalld), open the port in it
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
    ufw allow "$PORT/tcp"
fi
if systemctl is-active -q firewalld; then
    firewall-cmd -q --permanent --add-port="$PORT/tcp"
    firewall-cmd -q --reload
fi

echo "== self test: fetching https://example.com/ through the proxy"
code=000
for _ in $(seq 20); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --cacert "$CRT" --connect-to "$NAME:$PORT:127.0.0.1:$PORT" \
        -H @<(printf 'X-Proxy-Auth: %s\n' "$PROXY_PASSWORD") "https://$NAME:$PORT/fetch?url=https://example.com/" || true)"
    [[ $code == 200 ]] && break
    sleep 0.5
done
if [[ $code != 200 ]]; then
    echo "self test failed (HTTP $code). Logs: journalctl -u stultus-proxy -n 50" >&2
    exit 1
fi
code="$(curl -s -o /dev/null -w '%{http_code}' --cacert "$CRT" --connect-to "$NAME:$PORT:127.0.0.1:$PORT" \
    -H @<(printf 'X-Proxy-Auth: %s\n' "$PROXY_PASSWORD") "https://$NAME:$PORT/fetch?url=http://127.0.0.1:22/" || true)"
[[ $code == 403 ]] || { echo "self test failed: an internal address got HTTP $code, want 403 (old proxy.go?)" >&2; exit 1; }
echo "ok (and internal addresses are refused)"

cat <<EOF

Done. The proxy listens on https://$NAME:$PORT
Next, on the head node (db01), from the repo:
  scp <you>@$NAME:$CRT /tmp/vps-proxy.crt     # the certificate is public, it's printed below too
  sudo ./deploy/db01/connect_vps_proxy.sh $NAME /tmp/vps-proxy.crt $PORT

$(cat "$CRT")
EOF
if [[ -n $new_password ]]; then
    cat <<EOF

A new password was made. The head and every crawler must use the same one, so on db01 run instead:
  sudo PROXY_PASSWORD=$PROXY_PASSWORD ./deploy/db01/connect_vps_proxy.sh $NAME /tmp/vps-proxy.crt $PORT
and set SCRAPER_PROXY_PASSWORD=$PROXY_PASSWORD in /etc/stultus/crawler.env on every worker.
EOF
fi
