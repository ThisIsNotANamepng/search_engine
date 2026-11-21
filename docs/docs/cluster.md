# Cluster

One head machine (`db01`) and a set of air-gapped crawler workers (`in01`–`in12`), all Rocky Linux 9.7.

We used to run this on k3s. That's gone (removed November 2026). The workers now run plain containerd, and a small
node agent on each one starts and supervises the crawler containers. The head runs a handful of ordinary systemd
services.

```
             db01 (head, 137.28.211.66)
  ┌───────────────────────────────────────────────┐
  │ Postgres :5432   Redis :6379                   │
  │ registry :5000 / registry-cache :5001 (podman) │
  │ stultus-head :8000   (dashboard, search,       │
  │                       agent heartbeats)        │
  │ stultus-proxy :8888  stultus-storage :8003     │
  │ stultus-blocklist :8002 (+ 10 min refresh)     │
  └───────────────────────────────────────────────┘
          ▲ heartbeat (POST /agent/heartbeat, every 10 s)
          │ crawl traffic (proxy, Postgres, Redis, storage)
  ┌───────┴─────────┐
  │ in01 … in12     │  containerd + crawler-agent.service
  │ N crawler       │  (node_agent.py) → N × `ctr run` of
  │ containers each │  db01:5000/stultus:<version>
  └─────────────────┘
```

## Head (db01)

Everything except Postgres, Redis and the registry is installed from `deploy/db01/`:

```bash
sudo mkdir -p /etc/stultus
sudo cp deploy/db01/head.env.example  /etc/stultus/head.env    # DATABASE_URL
sudo cp deploy/db01/proxy.env.example /etc/stultus/proxy.env   # PROXY_UPSTREAM, PROXY_PASSWORD
sudo nano /etc/stultus/head.env /etc/stultus/proxy.env
sudo ./deploy/db01/install.sh /home/matt/search_engine matt
```

`install.sh` builds `supporting_servers/proxy.go` into `/usr/local/bin/stultus-proxy`, installs the units below
and starts them. It uses the repo's virtualenv (`<repo>/.env`) if there is one, and labels its `bin/` for SELinux
(systemd may not otherwise run programs stored under `/home`).

| Unit | Port | Runs |
|---|---|---|
| `stultus-head` | 8000 | `frontend/app.py` under gunicorn |
| `stultus-proxy` | 8888 | `/usr/local/bin/stultus-proxy` |
| `stultus-storage` | 8003 | `supporting_servers/web_storage_server.py` under gunicorn |
| `stultus-blocklist` | 8002 | `supporting_servers/serve_blocklist.py` under gunicorn |
| `stultus-blocklist-update.timer` | | `update_local_blocklist.py` every 10 minutes |

The registry is two podman containers set to `--restart=always`, brought back at boot by
`podman-restart.service`:

```bash
sudo podman ps                                   # registry, registry-cache
curl -s http://localhost:5000/v2/_catalog        # stultus, stultus-control, ...
```

Useful checks:

```bash
systemctl status stultus-head stultus-proxy stultus-storage stultus-blocklist
journalctl -u stultus-head -f
sudo ss -ltnp | grep -E ':(5000|8000|8002|8003|8888)\b'
```

## Workers (in01–in12)

- Air-gapped: no internet, no package repos. Anything they need is downloaded on db01 and copied over.
- Each runs `containerd.service` (the `containerd.io` RPM) and `crawler-agent.service`.
- The agent is `/usr/local/bin/node_agent.py` (stdlib only, run by the system python), configured by
  `/etc/stultus/crawler.env`. See the top of `crawler-agent.service` and `node_agent.py` for every option.

`/etc/stultus/crawler.env` (mode 600):

```bash
HEAD_URL=http://db01:8000
CRAWLER_IMAGE=db01:5000/stultus:<version>
CTR_PLAIN_HTTP=1
CTR_NAMESPACE=stultus
CTR_BIN=/usr/bin/ctr
DATABASE_URL=<postgres_url>
SCRAPER_PROXY=http://db01:8888
SCRAPER_PROXY_PASSWORD=<scraper_password>
WEB_TEXT_STORAGE_SERVER_ADDRESS=http://db01:8003
INITIAL_DESIRED=1
RUN_BATCHER=1
```

`RUN_BATCHER=1` makes the agent also run `supporting_servers/batcher.py` (from the same image) on the box. The
crawlers send their postgres writes to it on `127.0.0.1:8004`, and it sends them on together every 5 seconds
instead of every crawler sending its own small transactions. Leave it out to have the crawlers write directly.

The number of crawlers per worker is set from the dashboard; the agent picks it up on its next heartbeat. If the
head is down, workers keep running at the last count they were given.

### Setting up a new worker / moving one off k3s

On db01, download the RPMs once (the workers' `selinux-policy` is older than what the current `container-selinux`
needs, so the policy update comes along):

```bash
mkdir -p ~/containerd-rpms && cd ~/containerd-rpms
sudo dnf config-manager --add-repo https://download.docker.com/linux/rhel/docker-ce.repo   # once
dnf download containerd.io container-selinux selinux-policy selinux-policy-targeted
```

Then, for each worker (`$n`):

```bash
scp ~/containerd-rpms/*.rpm deploy/workers/migrate-to-containerd.sh $n:/tmp/
ssh -t $n "sudo bash /tmp/migrate-to-containerd.sh /tmp/selinux-policy-*.rpm /tmp/container-selinux-*.rpm /tmp/containerd.io-*.rpm"
```

The script removes k3s if present, installs and starts containerd, points `crawler-agent` at it and restarts the
agent. It is safe to re-run. A brand-new worker also needs the agent itself first:

```bash
scp node_agent.py crawler-agent.service $n:/tmp/
ssh -t $n "sudo install -m 755 /tmp/node_agent.py /usr/local/bin/node_agent.py &&
           sudo install -m 644 /tmp/crawler-agent.service /etc/systemd/system/ &&
           sudo systemctl daemon-reload && sudo systemctl enable crawler-agent"
# and create /etc/stultus/crawler.env as above
```

### Checking the workers

```bash
for n in in01 in02 in03 in04 in05 in06 in07 in08 in09 in10 in11 in12; do
  echo "$n: $(ssh $n 'systemctl is-active containerd crawler-agent | tr "\n" " "')"
done
ssh in01 journalctl -u crawler-agent -n 30 --no-pager
ssh -t in01 sudo ctr -n stultus tasks ls        # running crawler containers
```

### Deploying a new crawler image

On db01, from the repo, as your normal user (not with sudo):

```bash
git pull
./deploy/workers/update-crawler-image.sh                      # all workers
./deploy/workers/update-crawler-image.sh in03 in04            # or just some
./deploy/workers/update-crawler-image.sh --set RUN_BATCHER=1  # and set crawler.env options
```

It builds the image from the checkout (tagged with the commit, e.g. `db01:5000/stultus:e56c1b9`), pushes it to
the registry, then on each worker installs this checkout's `node_agent.py`, sets `CRAWLER_IMAGE` (and any
`--set KEY=VALUE`) in `crawler.env` and restarts the agent. The agent stops the old crawlers (they hand their URLs
back), pulls the new image and starts them again. Unreachable workers are skipped and listed at the end.
`--no-build` reuses the image already pushed for this commit. If the proxy changed in the same update, update it
first (see the proxy page).
