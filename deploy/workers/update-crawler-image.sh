#!/usr/bin/env bash
# Build the crawler image from this checkout, push it to db01's registry, and switch the
# workers to it. Each worker's agent stops its crawlers (SIGTERM, so they hand their URLs
# back), pulls the new image and starts them again.
#
# Run on db01, from the repo, as your normal user (it uses sudo itself):
#   git pull
#   ./deploy/workers/update-crawler-image.sh              # all workers
#   ./deploy/workers/update-crawler-image.sh in03 in04    # just these
#
# You're asked for your sudo password on db01 once, then once per worker.
# If the proxy changed too, update it first (docs/docs/proxy.md: VPS, then db01).
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
REGISTRY="${REGISTRY:-db01:5000}"
if [[ $# -gt 0 ]]; then
    NODES=("$@")
else
    NODES=(in01 in02 in03 in04 in05 in06 in07 in08 in09 in10 in11 in12)
fi

cd "$REPO"
TAG="$(git rev-parse --short HEAD)"
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
    TAG="$TAG-dirty-$(date +%Y%m%d%H%M)"
    echo "note: uncommitted changes are included; tagging the image $TAG"
fi
IMAGE="$REGISTRY/stultus:$TAG"

echo "== building $IMAGE"
# --network=host: the build's own container network can't get out on db01
sudo podman build --network=host -t "$IMAGE" . || { echo "build failed" >&2; exit 1; }

echo "== pushing to $REGISTRY"
sudo podman push --tls-verify=false "$IMAGE" || { echo "push failed" >&2; exit 1; }

ok=() failed=()
for n in "${NODES[@]}"; do
    echo "== $n"
    if ! ssh -o ConnectTimeout=5 -o BatchMode=yes "$n" true 2>/dev/null; then
        echo "   unreachable, skipped"
        failed+=("$n (unreachable)")
        continue
    fi
    if ssh -t "$n" "sudo sed -i 's|^CRAWLER_IMAGE=.*|CRAWLER_IMAGE=$IMAGE|' /etc/stultus/crawler.env \
                    && sudo systemctl restart crawler-agent \
                    && systemctl is-active --quiet crawler-agent"; then
        ok+=("$n")
    else
        failed+=("$n")
    fi
done

echo
echo "image:   $IMAGE"
echo "updated: ${ok[*]:-none}"
[[ ${#failed[@]} -eq 0 ]] || echo "FAILED:  ${failed[*]}  (rerun with just those nodes once fixed)"
echo "The dashboard shows the crawlers coming back within a minute (the first start pulls the image)."
[[ ${#failed[@]} -eq 0 ]]
