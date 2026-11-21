#!/bin/sh
# The lab's throwaway Postgres, in a podman container on 127.0.0.1:5433 (so it can't clash with a local Postgres on 5432)
case "$1" in
    up)    podman start stultus-lab 2>/dev/null || podman run -d --name stultus-lab -e POSTGRES_PASSWORD=lab -p 127.0.0.1:5433:5432 -v stultus-lab:/var/lib/postgresql/data docker.io/library/postgres:17 ;;
    down)  podman stop stultus-lab ;;
    reset) podman rm -f stultus-lab; podman volume rm -f stultus-lab ;;  # deletes every built index
    *)     echo "usage: $0 up|down|reset"; exit 1 ;;
esac
