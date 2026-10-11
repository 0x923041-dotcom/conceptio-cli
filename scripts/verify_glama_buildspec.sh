#!/usr/bin/env bash
# verify_glama_buildspec.sh — prove the Glama build spec works, end to end.
#
# Why this exists: the awesome-mcp-servers entry (PR punkpeye/awesome-mcp-servers#15931)
# is gated on Glama's quality score, whose pipeline builds the server from a
# GENERATED Dockerfile (Glama's build spec — NOT the repo's own Dockerfile).
# When that pipeline reports `DockerBuildError: (HTTP code 502)` with empty
# logs, this script answers the only question that matters: does the spec's
# own output build and serve? If this passes, the failure is in the request
# path to the builder (docker-modem -> nginx -> dockerd), not the submission.
#
#   bash scripts/verify_glama_buildspec.sh            # build + run + tools/list
#   PIN=<sha> bash scripts/verify_glama_buildspec.sh  # pin a different commit
#
# Exit codes: 0 = built and served every tool · 1 = a stage failed ·
# 2 = could not check (no docker).
#
# Measured once on VPS docker 29.8.1 against the spec as of commit 30971b0:
# BUILD_RC=0, initialize 200 + session id, tools/list = 9 tools. A buildx
# "activity: permission denied" is a local docker-config quirk, not a verdict —
# DOCKER_CONFIG points at a scratch dir to dodge it.

set -u
PIN="${PIN:-30971b0ddf619db7e7db30e36010c80847f939d0}"
PORT="${PORT:-}"  # empty = docker picks a free one (a fixed port collides with
                   # whatever else runs on the box — measured once, 8099 was taken)
HERE="$(cd "$(dirname "$0")" && pwd)"

command -v docker >/dev/null 2>&1 || { echo "could not check: no docker"; exit 2; }
export DOCKER_CONFIG="${DOCKER_CONFIG:-/tmp/verify_glama_dcfg}"
mkdir -p "$DOCKER_CONFIG"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# The build spec's generated Dockerfile — template verbatim, pin parameterized.
cat > "$WORK/Dockerfile" <<EOF
FROM debian:trixie-slim

ENV DEBIAN_FRONTEND=noninteractive \\
    GLAMA_VERSION="1.0.0" \\
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl git && curl -fsSL https://deb.nodesource.com/setup_24.x | bash - && apt-get install -y --no-install-recommends nodejs && npm install -g mcp-proxy@6.7.16 pnpm@10.14.0 && node --version && curl -LsSf https://astral.sh/uv/install.sh | UV_INSTALL_DIR="/usr/local/bin" sh && uv python install 3.14 --default --preview && ln -s \$(uv python find) /usr/local/bin/python && python --version && apt-get clean && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

WORKDIR /app

RUN git clone https://github.com/0x923041-dotcom/conceptio-cli . && git checkout ${PIN}

ENV PATH="/app/node_modules/.bin:\$PATH"

RUN (uv sync) && (ln -s /app/.venv/bin/conceptio /usr/local/bin/conceptio)

CMD ["mcp-proxy","--","conceptio","mcp"]
EOF

echo "== building the spec's generated Dockerfile (pin ${PIN})"
docker build -t verify-glama-buildspec "$WORK" || { echo "FAIL: docker build"; exit 1; }

docker rm -f verify-glama-run >/dev/null 2>&1
if [ -n "$PORT" ]; then
  PUBLISH="127.0.0.1:${PORT}:8080"
else
  PUBLISH="127.0.0.1::8080"   # ephemeral — collision-proof
fi
docker run -d --name verify-glama-run -p "$PUBLISH" verify-glama-buildspec >/dev/null || { echo "FAIL: docker run"; exit 1; }
trap 'docker rm -f verify-glama-run >/dev/null 2>&1; rm -rf "$WORK"' EXIT
sleep 4
if [ -z "$PORT" ]; then
  PORT="$(docker port verify-glama-run 8080/tcp | head -1 | awk -F: '{print $NF}')"
fi
echo "== serving on 127.0.0.1:${PORT}"

echo "== initialize (keyless discovery must answer)"
INIT="$(curl -s -i -X POST -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"verify","version":"0"}}}' \
  "http://127.0.0.1:${PORT}/mcp")"
SID="$(printf '%s' "$INIT" | grep -i '^mcp-session-id:' | tr -d '\r' | awk '{print $2}')"
[ -n "$SID" ] || { echo "FAIL: no session id from initialize"; printf '%s\n' "$INIT" | head -3; exit 1; }

curl -s -X POST -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' -H "Mcp-Session-Id: $SID" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  "http://127.0.0.1:${PORT}/mcp" >/dev/null

echo "== tools/list (the capture Glama scores)"
TOOLS="$(curl -s -X POST -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' -H "Mcp-Session-Id: $SID" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
  "http://127.0.0.1:${PORT}/mcp" | grep -o '"name":"[a-z_]*"' | sort -u)"
printf '%s\n' "$TOOLS"
N="$(printf '%s' "$TOOLS" | grep -c 'conceptio_')"
if [ "$N" -ge 9 ]; then
  echo "VERIFIED — spec builds and serves ${N} tools"
  exit 0
fi
echo "FAIL: expected 9 conceptio tools, saw ${N}"
exit 1
