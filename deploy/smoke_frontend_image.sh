#!/usr/bin/env bash
set -euo pipefail

if [[ $# != 1 ]]; then
  printf 'Usage: bash deploy/smoke_frontend_image.sh IMAGE\n' >&2
  exit 2
fi

image=$1
scratch=$(mktemp -d "${TMPDIR:-/tmp}/maraclaw-web-smoke.XXXXXX")
network="maraclaw-web-smoke-${scratch##*.}"
container="${network}-web"
cleanup() {
  status=$?
  if [[ $status != 0 ]]; then
    docker logs "$container" >&2 || true
  fi
  docker rm -f "$container" || true
  docker network rm "$network" || true
  rm -rf "$scratch"
  return "$status"
}
trap cleanup EXIT

docker network create "$network"
docker run -d --name "$container" --network "$network" \
  --network-alias maraclaw-engine --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  -p 127.0.0.1::8080 "$image"
test "$(docker exec "$container" id -u)" != 0

healthy=false
for ((attempt = 0; attempt < 60; attempt++)); do
  health=$(docker inspect --format '{{.State.Health.Status}}' "$container")
  if [[ $health == healthy ]]; then
    healthy=true
    break
  fi
  if [[ $health == unhealthy ]] || [[ $(docker inspect --format '{{.State.Running}}' "$container") != true ]]; then
    exit 1
  fi
  sleep 1
done
test "$healthy" = true

address=$(docker port "$container" 8080/tcp)
curl --fail --silent --show-error --max-time 10 "http://$address/healthz" > "$scratch/health"
test "$(< "$scratch/health")" = ok
curl --fail --silent --show-error --max-time 10 "http://$address/" > "$scratch/index"
curl --fail --silent --show-error --max-time 10 "http://$address/app/smoke/deep-link" > "$scratch/spa"
test -s "$scratch/index"
cmp "$scratch/index" "$scratch/spa"
test "$(curl --silent --show-error --max-time 10 -o /dev/null -w '%{http_code}' "http://$address/assets/smoke-missing.js")" = 404
printf 'PASS %s: non-root, healthy :8080, /healthz, SPA fallback, missing asset 404\n' "$image"
