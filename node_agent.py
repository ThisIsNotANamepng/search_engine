"""
Node agent — runs on each crawler box, replaces the Kubernetes control plane.

Responsibilities:
  1. Spawn and supervise N crawler containers (via containerd's `ctr`; restart
     on crash, with exponential backoff so a broken crawler can't hot-loop).
  2. Reconcile the running count to a "desired count" it learns from the head
     machine, so you can scale a box up/down without touching it directly.
  3. Heartbeat to the head machine (the frontend dashboard) every few seconds
     with liveness + host CPU/RAM + per-container stats.

Each crawler is one containerd container. The agent supervises a *foreground*
`ctr run` per slot: run without `-d`, `ctr` stays attached for the container's
whole lifetime, so when the container exits `ctr` exits and the crash/restart
logic below reacts exactly as it would for a bare process. This keeps container
control (isolation, resource limits, image pinning) while the supervision engine
stays dead simple — and it reuses the containerd that was already installed with
your old Kubernetes, so there's nothing new to install.

Notes on ctr vs docker/podman (handled below):
  - No `--replace`, so we explicitly delete any leftover container of the same
    id before (re)spawning.
  - `--env` needs KEY=VALUE, so we resolve each CRAWLER_ENV var's value from the
    agent's own environment rather than passing the name through.
  - containerd has namespaces; we use our own (default "stultus") so we never
    touch the leftover "k8s.io" images/containers.
  - With the k8s CNI gone there's no pod networking, so containers run with
    --net-host (the host's network stack) by default — that's how the crawler
    reaches the proxy, Postgres and the storage server.

Design notes:
  - PULL model: the agent POSTs its state to the head and the head's response
    carries the new desired count. Workers never need an inbound port, so this
    is NAT/firewall friendly and the head never reaches into a box.
  - The head is NOT in the crawl data path. If the head is down, crawlers keep
    running at the last known desired count. Crawling never stops because the
    dashboard hiccuped (the whole point of leaving k8s behind).
  - systemd keeps exactly ONE thing alive per box: this agent. The agent keeps
    the crawler containers alive. See crawler-agent.service.

Configuration (environment variables):
  HEAD_URL              (required) base URL of the head/dashboard, e.g. http://db01:5000
  NODE_ID               stable id for this box            (default: hostname)
  CRAWLER_IMAGE         container image to run            (default: localhost/stultus:latest)
  CTR_BIN               ctr executable                    (default: /usr/local/bin/ctr)
  CTR_NAMESPACE         containerd namespace to use       (default: stultus)
  CTR_PLAIN_HTTP        "1" to pull over plain HTTP (insecure registry)  (default: 0)
  CRAWLER_PULL          "1" to auto-pull the image if missing (default: 1)
  CRAWLER_NET_HOST      "1" to run containers with --net-host (default: 1)
  CRAWLER_ENV           comma-separated env var NAMES to forward into each
                        container, resolved from the agent's own environment
                        (default: DATABASE_URL,SCRAPER_PROXY,SCRAPER_PROXY_PASSWORD,
                                  WEB_TEXT_STORAGE_SERVER_ADDRESS,DEBUG)
  CRAWLER_ENV_FILE      optional path to an env file passed as --env-file
  CRAWLER_RUN_ARGS      extra flags for `ctr run` (e.g. "--memory-limit 1073741824")
  CRAWLER_CMD           optional command to override the image's default entrypoint
  INITIAL_DESIRED       desired crawlers before the head answers (default: 1)
  HEARTBEAT_INTERVAL    seconds between heartbeats        (default: 10)
  RECONCILE_INTERVAL    seconds between supervise ticks   (default: 1)
  TERM_GRACE            seconds to wait for a container to stop before SIGKILL (default: 10)

The forwarded env vars (DATABASE_URL, SCRAPER_PROXY, etc.) only need to be set
once in the agent's environment (systemd unit / EnvironmentFile). Containers are
named "stultus-<node>-<slot>" within the CTR_NAMESPACE.
"""

import os
import re
import time
import json
import shlex
import signal
import socket
import subprocess
import urllib.request
import urllib.error

# Intentionally stdlib-only: workers may have no internet (no pip/PyPI), so the
# agent must run on a bare Python 3 with nothing installed. Host CPU/RAM come
# from /proc; the heartbeat uses urllib instead of requests.


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def _clean(raw):
    """
    Value up to any inline '#' comment, whitespace stripped. `ctr`/`docker`
    --env-file take the whole line after '=' literally (no inline comments), so
    a line like `INITIAL_DESIRED=1  # note` yields the value "1  # note". We
    tolerate that for the numeric/flag settings here. NOT applied to free-form
    strings (passwords, URLs) where '#' can be a legitimate character.
    """
    return raw.split("#", 1)[0].strip()


def _flag(name, default):
    return _clean(os.getenv(name, default)).lower() in ("1", "true", "yes", "on")


def _int_env(name, default):
    raw = os.getenv(name)
    if raw is None or not _clean(raw):
        return default
    try:
        return int(_clean(raw))
    except ValueError:
        print(f"[node_agent {NODE_ID}] invalid {name}={raw!r}, using {default}", flush=True)
        return default


def _float_env(name, default):
    raw = os.getenv(name)
    if raw is None or not _clean(raw):
        return default
    try:
        return float(_clean(raw))
    except ValueError:
        print(f"[node_agent {NODE_ID}] invalid {name}={raw!r}, using {default}", flush=True)
        return default


HEAD_URL = os.getenv("HEAD_URL", "").rstrip("/")
NODE_ID = os.getenv("NODE_ID") or socket.gethostname()

CTR_BIN = os.getenv("CTR_BIN", "/usr/local/bin/ctr")
CTR_NAMESPACE = os.getenv("CTR_NAMESPACE", "stultus")
CTR_PLAIN_HTTP = _flag("CTR_PLAIN_HTTP", "0")
CRAWLER_PULL = _flag("CRAWLER_PULL", "1")
CRAWLER_NET_HOST = _flag("CRAWLER_NET_HOST", "1")
CRAWLER_IMAGE = os.getenv("CRAWLER_IMAGE", "localhost/stultus:latest")
CRAWLER_ENV = [v.strip() for v in os.getenv(
    "CRAWLER_ENV",
    "DATABASE_URL,SCRAPER_PROXY,SCRAPER_PROXY_PASSWORD,"
    "WEB_TEXT_STORAGE_SERVER_ADDRESS,DEBUG",
).split(",") if v.strip()]
CRAWLER_ENV_FILE = os.getenv("CRAWLER_ENV_FILE")
CRAWLER_RUN_ARGS = shlex.split(os.getenv("CRAWLER_RUN_ARGS", ""))
CRAWLER_CMD = shlex.split(os.getenv("CRAWLER_CMD", ""))

# containerd container ids must be alphanumeric groups joined by single ._- ;
# collapse any run of other characters in the node id to a single dash.
_SAFE_NODE = re.sub(r"[^a-zA-Z0-9]+", "-", NODE_ID).strip("-") or "node"
CONTAINER_PREFIX = f"stultus-{_SAFE_NODE}"

INITIAL_DESIRED = _int_env("INITIAL_DESIRED", 1)
HEARTBEAT_INTERVAL = _float_env("HEARTBEAT_INTERVAL", 10.0)
RECONCILE_INTERVAL = _float_env("RECONCILE_INTERVAL", 1.0)
TERM_GRACE = _float_env("TERM_GRACE", 10.0)

# Crash backoff: a slot that dies waits BASE * 2**(consecutive restarts), capped.
# A slot that stayed up longer than HEALTHY_UPTIME has its restart count reset,
# so an occasional crash after hours of work doesn't accumulate backoff.
BACKOFF_BASE = 1.0
BACKOFF_MAX = 60.0
HEALTHY_UPTIME = 30.0


def log(msg):
    print(f"[node_agent {NODE_ID}] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# Host stats (stdlib only, from /proc — no psutil)
# --------------------------------------------------------------------------- #
_prev_cpu = None  # (total_jiffies, idle_jiffies) from the last cpu_percent() call


def cpu_percent():
    """
    Whole-host CPU utilisation since the previous call, from /proc/stat. Like
    psutil.cpu_percent(interval=None): the first call primes and returns 0.0.
    """
    global _prev_cpu
    try:
        with open("/proc/stat") as f:
            fields = [int(x) for x in f.readline().split()[1:]]
    except OSError:
        return 0.0
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0)  # idle + iowait
    total = sum(fields)
    prev = _prev_cpu
    _prev_cpu = (total, idle)
    if prev is None:
        return 0.0
    dtotal = total - prev[0]
    didle = idle - prev[1]
    if dtotal <= 0:
        return 0.0
    return round(100.0 * (dtotal - didle) / dtotal, 1)


def mem_percent():
    """Percent of RAM in use, from /proc/meminfo (MemTotal vs MemAvailable)."""
    info = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, rest = line.partition(":")
                info[key] = int(rest.split()[0])  # value is in kB
    except OSError:
        return 0.0
    total = info.get("MemTotal", 0)
    avail = info.get("MemAvailable", info.get("MemFree", 0))
    if total <= 0:
        return 0.0
    return round(100.0 * (total - avail) / total, 1)


# --------------------------------------------------------------------------- #
# containerd (ctr) helpers
# --------------------------------------------------------------------------- #
def _ctr(*args):
    """ctr argv with the namespace flag applied (must precede the subcommand)."""
    return [CTR_BIN, "--namespace", CTR_NAMESPACE, *args]


def _quiet(argv):
    return subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def container_name(slot_id):
    return f"{CONTAINER_PREFIX}-{slot_id}"


def run_command(slot_id):
    """Build the foreground `ctr run` argv for one crawler slot."""
    cmd = _ctr("run", "--rm")
    if CRAWLER_NET_HOST:
        cmd.append("--net-host")
    for var in CRAWLER_ENV:
        val = os.environ.get(var)
        if val is not None:
            cmd += ["--env", f"{var}={val}"]  # ctr needs KEY=VALUE, not just NAME
    if CRAWLER_ENV_FILE:
        cmd += ["--env-file", CRAWLER_ENV_FILE]
    cmd += CRAWLER_RUN_ARGS
    # `ctr run` positional args: IMAGE, container-id, then optional command.
    cmd += [CRAWLER_IMAGE, container_name(slot_id)]
    cmd += CRAWLER_CMD  # empty => use the image's own entrypoint/cmd
    return cmd


def ctr_rm(name):
    """Force-remove a container and its task by id (idempotent, quiet)."""
    _quiet(_ctr("task", "kill", "--signal", "SIGKILL", name))
    _quiet(_ctr("task", "delete", "--force", name))
    _quiet(_ctr("container", "delete", name))


def ensure_image():
    """Make sure CRAWLER_IMAGE exists in our namespace, pulling it if allowed."""
    res = subprocess.run(_ctr("image", "ls", "-q"),
                         capture_output=True, text=True)
    if CRAWLER_IMAGE in res.stdout.split():
        return
    if not CRAWLER_PULL:
        log(f"image {CRAWLER_IMAGE} not present and CRAWLER_PULL is off")
        return
    pull = _ctr("image", "pull")
    if CTR_PLAIN_HTTP:
        pull.append("--plain-http")
    pull.append(CRAWLER_IMAGE)
    log(f"pulling image {CRAWLER_IMAGE} into namespace {CTR_NAMESPACE}")
    subprocess.run(pull)  # inherit stdout/stderr so pull progress is visible


def cleanup_stale_containers():
    """Remove any leftover crawler containers from a previous unclean shutdown."""
    res = subprocess.run(_ctr("container", "ls", "-q"),
                         capture_output=True, text=True)
    stale = [n for n in res.stdout.split() if n.startswith(CONTAINER_PREFIX + "-")]
    if stale:
        log(f"cleaning up {len(stale)} stale container(s)")
        for name in stale:
            ctr_rm(name)


# --------------------------------------------------------------------------- #
# Agent
# --------------------------------------------------------------------------- #
class NodeAgent:
    def __init__(self):
        # slot_id -> {proc, started_at, restarts, next_start}
        # A slot is a logical crawler "slot"; slots 0..desired-1 should be running.
        self.slots = {}
        self.desired = INITIAL_DESIRED
        self.running = True
        # Prime cpu_percent() so later calls return a real delta, not 0.0.
        cpu_percent()

    # ---- container management ---------------------------------------------- #
    def _spawn(self, slot_id, restarts):
        # ctr has no --replace, so clear any leftover container of this id first
        # (e.g. from a hard crash where --rm didn't get to run).
        ctr_rm(container_name(slot_id))
        # Foreground `ctr run`: this process lives exactly as long as the
        # container, so poll()/terminate() below manage the container's lifecycle.
        proc = subprocess.Popen(
            run_command(slot_id),
            env=os.environ,          # ctr reads env values from here
            start_new_session=True,  # own process group, so we can signal cleanly
        )
        self.slots[slot_id] = {
            "proc": proc,
            "started_at": time.time(),
            "restarts": restarts,
            "next_start": 0.0,
        }
        log(f"spawned slot {slot_id} container={container_name(slot_id)} "
            f"pid={proc.pid} (restarts={restarts})")

    def _stop(self, slot_id):
        """Gracefully stop a slot's container: SIGTERM the task, then force-rm."""
        slot = self.slots.pop(slot_id, None)
        name = container_name(slot_id)
        if slot and slot["proc"] is not None and slot["proc"].poll() is None:
            proc = slot["proc"]
            log(f"stopping slot {slot_id} container={name} pid={proc.pid}")
            # Signal the container's process directly; the attached `ctr run`
            # exits once the task does.
            _quiet(_ctr("task", "kill", "--signal", "SIGTERM", name))
            try:
                proc.wait(timeout=TERM_GRACE)
            except subprocess.TimeoutExpired:
                log(f"slot {slot_id} didn't stop in time, killing")
                _quiet(_ctr("task", "kill", "--signal", "SIGKILL", name))
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        # Belt and suspenders: guarantee the container/task are gone even if the
        # `ctr run` client was killed and left the shim holding the task.
        ctr_rm(name)

    def reconcile(self):
        """Make the set of running crawlers match `self.desired`."""
        now = time.time()

        # 1. Reap crashed crawlers and schedule their respawn with backoff.
        for slot_id, slot in list(self.slots.items()):
            proc = slot["proc"]
            if proc is not None and proc.poll() is not None:
                uptime = now - slot["started_at"]
                # Reset the restart counter if it had been healthy for a while.
                restarts = 0 if uptime >= HEALTHY_UPTIME else slot["restarts"] + 1
                backoff = min(BACKOFF_MAX, BACKOFF_BASE * (2 ** restarts))
                slot["proc"] = None
                slot["restarts"] = restarts
                slot["next_start"] = now + backoff
                log(f"slot {slot_id} exited code={proc.returncode} "
                    f"uptime={uptime:.0f}s, respawning in {backoff:.0f}s")

        # 2. Scale down: stop slots at or above the desired count.
        for slot_id in [s for s in self.slots if s >= self.desired]:
            self._stop(slot_id)

        # 3. Scale up / restart: ensure slots 0..desired-1 are running.
        for slot_id in range(self.desired):
            slot = self.slots.get(slot_id)
            if slot is None:
                self._spawn(slot_id, restarts=0)
            elif slot["proc"] is None and now >= slot["next_start"]:
                self._spawn(slot_id, restarts=slot["restarts"])

    # ---- heartbeat --------------------------------------------------------- #
    def _state(self):
        procs = []
        alive = 0
        now = time.time()
        for slot_id, slot in sorted(self.slots.items()):
            proc = slot["proc"]
            if proc is not None and proc.poll() is None:
                alive += 1
                procs.append({
                    "slot": slot_id,
                    "container": container_name(slot_id),
                    "pid": proc.pid,
                    "uptime_s": round(now - slot["started_at"]),
                    "restarts": slot["restarts"],
                })
        return {
            "node_id": NODE_ID,
            "hostname": socket.gethostname(),
            "alive_count": alive,
            "desired_count": self.desired,
            "cpu_percent": cpu_percent(),
            "mem_percent": mem_percent(),
            "procs": procs,
        }

    def heartbeat(self):
        """POST state to the head; adopt the desired count it returns."""
        if not HEAD_URL:
            return  # standalone mode: no head, just supervise at INITIAL_DESIRED
        try:
            req = urllib.request.Request(
                f"{HEAD_URL}/agent/heartbeat",
                data=json.dumps(self._state()).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = json.loads(resp.read().decode())
            desired = body.get("desired_count")
            if isinstance(desired, int) and desired >= 0 and desired != self.desired:
                log(f"desired count changed {self.desired} -> {desired}")
                self.desired = desired
        except (urllib.error.URLError, OSError, ValueError) as e:
            # Head unreachable/4xx: keep running at the last known desired count.
            # (HTTPError is a URLError subclass, so 404s land here too.)
            log(f"heartbeat failed ({e}); holding desired={self.desired}")

    # ---- main loop --------------------------------------------------------- #
    def run(self):
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)
        log(f"starting: head={HEAD_URL or '(none)'} image={CRAWLER_IMAGE} "
            f"namespace={CTR_NAMESPACE} initial_desired={self.desired}")
        cleanup_stale_containers()
        ensure_image()

        last_heartbeat = 0.0
        self.heartbeat()  # learn desired count before we start spawning
        while self.running:
            self.reconcile()
            if time.time() - last_heartbeat >= HEARTBEAT_INTERVAL:
                self.heartbeat()
                last_heartbeat = time.time()
            time.sleep(RECONCILE_INTERVAL)

        self.shutdown()

    def _handle_signal(self, signum, frame):
        log(f"received signal {signum}, shutting down")
        self.running = False

    def shutdown(self):
        self.desired = 0
        for slot_id in list(self.slots):
            self._stop(slot_id)
        # Best-effort final heartbeat so the dashboard shows 0 alive immediately.
        try:
            self.heartbeat()
        except Exception:
            pass
        log("stopped")


if __name__ == "__main__":
    NodeAgent().run()
