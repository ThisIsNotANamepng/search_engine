# Proxy server

The crawlers never fetch pages themselves. Every request (pages and robots.txt) goes through two copies of the same small Go program, `supporting_servers/proxy.go`:

``` mermaid
graph LR
    C[Crawlers on in01-in12] -->|"http, GET /fetch?url=..."| A["Head proxy, db01:8888"]
    A -->|"https, same request"| B["VPS proxy, :8443"]
    B -->|"the real request"| W[The website]
```

- The head proxy has `PROXY_UPSTREAM` set, so it only forwards the request to the VPS.
- The VPS proxy has no upstream: it makes the real request. It only connects to public internet addresses, checked on the IP it actually dials, so a link or redirect can't reach our own network or the cloud metadata service (169.254.169.254).
- Redirects aren't followed by the proxy. The scraper follows them itself, one hop at a time, so each hop gets its robots.txt and host checks.
- Every request needs the shared password in the `X-Proxy-Auth` header. The crawlers send it as `SCRAPER_PROXY_PASSWORD`; the head passes it on to the VPS. The head, the VPS and every crawler use the same password.
- The proxy only serves `GET /fetch`. It is not a general proxy: no CONNECT, no `curl -x`.

## When something is down

Errors that are the proxy's own fault (wrong password, the VPS unreachable) carry an `X-Proxy-Error` header. The scraper treats those, and not being able to reach the head proxy at all, as "our server is down": the url stays claimed and is retried after the claim times out (30 minutes). Errors without that header are the site's fault, and the url is dropped like any other failed page.

If the VPS loses power, the head notices within about 3 seconds per connection, before the crawler's own 10 second timeout, so urls are kept, not dropped.

## Setting it up

On a new VPS (Debian, Ubuntu, Rocky or anything with apt or dnf), copy the setup script and the proxy source, then run the script:

```bash
scp deploy/vps/install_proxy.sh supporting_servers/proxy.go <you>@<vps>:
ssh <you>@<vps>
sudo PROXY_PASSWORD=<PROXY_PASSWORD from db01:/etc/stultus/proxy.env> ./install_proxy.sh <vps_public_ip_or_name> <head_public_ip>
```

It installs Go, builds the proxy, makes a self-signed certificate, runs the proxy as the `stultus-proxy` systemd service on port 8443, and sets the firewall so only the head can reach that port (SSH stays open). The cloud provider's own firewall must allow 8443 from the head too. At the end it prints the next commands.

Then on db01, from the repo:

```bash
scp <you>@<vps>:/etc/stultus/proxy.crt /tmp/vps-proxy.crt
sudo ./deploy/db01/connect_vps_proxy.sh <vps_public_ip_or_name> /tmp/vps-proxy.crt
```

It checks the VPS answers, rebuilds and restarts the head proxy pointed at it, and fetches a test page through the whole chain. The head trusts only the VPS's own certificate (`SSL_CERT_FILE`), so nobody in between can read the password.

Both scripts are safe to run again, for example after changing `proxy.go`.

**Order when updating:** update the VPS proxy before the crawler image. Crawlers going through a proxy leave the "is this a public address" check to the VPS.

## Settings

Set in `/etc/stultus/proxy.env`. The scripts write it; the full list is at the top of `proxy.go`.

| Variable | Meaning |
|---|---|
| `PROXY_ADDR` | Listen address, `:8888` on the head, `:8443` on the VPS |
| `PROXY_PASSWORD` | The shared password |
| `PROXY_UPSTREAM` | Head only: `https://<vps>:8443` |
| `SSL_CERT_FILE` | Head only: the VPS's certificate, the only one it trusts |
| `PROXY_TLS_CERT`, `PROXY_TLS_KEY` | VPS only: its certificate and key, so it serves https |
| `DEBUG` | Log every request and how long it took |

## Tests

```bash
cd supporting_servers && go test proxy.go proxy_test.go   # the proxy
python -m pytest tests/test_proxy_errors.py               # the scraper's side
```
