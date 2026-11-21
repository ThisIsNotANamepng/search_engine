#!/usr/bin/env bash
# Build the crawler image from this checkout, push it to db01's registry, and switch the
# workers to it, together with this checkout's node_agent.py (the agent and the crawler
# code change together). Each worker's agent stops its crawlers (SIGTERM, so they hand
# their URLs back), pulls the new image and starts them again.
#
# Run on db01, from the repo, as your normal user (it uses sudo itself):
#   git pull
#   ./deploy/workers/update-crawler-image.sh                      # all workers
#   ./deploy/workers/update-crawler-image.sh in03 in04            # just these
#   ./deploy/workers/update-crawler-image.sh --set RUN_BATCHER=1  # also set crawler.env options
#
# --set KEY=VALUE (repeatable) writes KEY=VALUE into each worker's /etc/stultus/crawler.env,
# replacing an existing KEY line. --no-build reuses the image already tagged for this commit.
# You're asked for your sudo password on db01 once, then once per worker.
# If the proxy changed too, update it first (docs/docs/proxy.md: VPS, then db01).
set -uo pipefail

if [[ $EUID -eq 0 ]]; then
    echo "Run this as your normal user, without sudo: it uses sudo itself where needed." >&2
    echo "As root, ssh has none of your keys, so every worker looks unreachable." >&2
    exit 1
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
REGISTRY="${REGISTRY:-db01:5000}"
ENV_FILE=/etc/stultus/crawler.env

NODES=() SETS=() BUILD=1
while [[ $# -gt 0 ]]; do
    case $1 in
        --set)
            [[ ${2:-} =~ ^[A-Z_][A-Z0-9_]*=[^\'\|]*$ ]] || { echo "--set needs KEY=VALUE (no quotes or |), got '${2:-}'" >&2; exit 1; }
            SETS+=("$2"); shift 2 ;;
        --no-build) BUILD=0; shift ;;
        -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
        -*) echo "unknown option $1" >&2; exit 1 ;;
        *) NODES+=("$1"); shift ;;
    esac
done
[[ ${#NODES[@]} -gt 0 ]] || NODES=(in01 in02 in03 in04 in05 in06 in07 in08 in09 in10 in11 in12)

cd "$REPO"
TAG="$(git rev-parse --short HEAD)"
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
    TAG="$TAG-dirty-$(date +%Y%m%d%H%M)"
    echo "note: uncommitted changes are included; tagging the image $TAG"
fi
IMAGE="$REGISTRY/stultus:$TAG"

if [[ $BUILD -eq 1 ]]; then
    echo "== building $IMAGE"
    # --network=host: the build's own container network can't get out on db01
    sudo podman build --network=host -t "$IMAGE" . || { echo "build failed" >&2; exit 1; }
    echo "== pushing to $REGISTRY"
    sudo podman push --tls-verify=false "$IMAGE" || { echo "push failed" >&2; exit 1; }
fi

# The remote commands: install the agent, set the image and any --set options, restart.
remote="sudo install -m 755 /tmp/node_agent.py /usr/local/bin/node_agent.py && rm -f /tmp/node_agent.py"
for kv in "CRAWLER_IMAGE=$IMAGE" "${SETS[@]}"; do
    key="${kv%%=*}"
    remote+=" && sudo sed -i '/^$key=/d' $ENV_FILE && echo '$kv' | sudo tee -a $ENV_FILE >/dev/null"
done
remote+=" && sudo systemctl restart crawler-agent && systemctl is-active --quiet crawler-agent"

ok=() failed=()
for n in "${NODES[@]}"; do
    echo "== $n"
    if ! ssh -o ConnectTimeout=5 -o BatchMode=yes "$n" true 2>/dev/null; then
        echo "   unreachable, skipped"
        failed+=("$n (unreachable)")
        continue
    fi
    if scp -q node_agent.py "$n:/tmp/node_agent.py" && ssh -t "$n" "$remote"; then
        ok+=("$n")
    else
        failed+=("$n")
    fi
done

echo
echo "image:   $IMAGE"
[[ ${#SETS[@]} -eq 0 ]] || echo "set:     ${SETS[*]}"
echo "updated: ${ok[*]:-none}"
[[ ${#failed[@]} -eq 0 ]] || echo "FAILED:  ${failed[*]}  (rerun with just those nodes once fixed)"
echo "The dashboard shows the crawlers coming back within a minute (the first start pulls the image)."
[[ ${#failed[@]} -eq 0 ]]
