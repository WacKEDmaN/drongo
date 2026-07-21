#!/usr/bin/env bash
# ===========================================================================
#  OPTIONAL self-hosted web search for DRONGO — SearXNG via Docker.
#  (github.com/searxng/searxng — open-source metasearch: real Google/Bing/DDG
#   results, a JSON API, NO API key, nothing handed to a cloud search broker.)
#
#    sudo bash ./system/searxng.sh
#
#  Installs Docker if missing, runs SearXNG bound to 127.0.0.1:8888, writes a
#  minimal config with the JSON API turned ON (DRONGO's web_search needs JSON),
#  and verifies it answers. Then point the agent at it:
#    echo 'SEARXNG_URL=http://127.0.0.1:8888' >> /etc/drongo/drongo.env
#    systemctl restart drongo drongo-web
#  (`install.sh --searxng` does that wiring for you.)
#
#  NOTES:
#   * Bound to LOCALHOST only. To serve it to other LAN machines, re-run the
#     container with  -p 0.0.0.0:8888:8080  (SEARXNG_BIND=0.0.0.0) and mind your
#     firewall — an open SearXNG is an open proxy of sorts.
#   * Light when idle, but it fetches from upstream engines on each query. On a
#     4GB Pi also running a local LLM it's happier on a spare LAN box — run this
#     there and set SEARXNG_URL to that host instead.
#   * Best-effort & re-runnable (NOT `set -e`). UNTESTED on your exact board —
#     report issues and I'll adjust.
# ===========================================================================
set -uo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Please run with sudo:  sudo bash ./system/searxng.sh"; exit 1; }

PORT="${SEARXNG_PORT:-8888}"
BIND="${SEARXNG_BIND:-127.0.0.1}"
CONF=/etc/searxng
IMAGE="${SEARXNG_IMAGE:-searxng/searxng:latest}"
NAME=searxng

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*"; }

# 1) Docker ----------------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
  say "Installing Docker (docker.io from Debian) …"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq || { warn "apt update failed — check your network"; exit 1; }
  apt-get install -y docker.io || { warn "could not install docker.io"; exit 1; }
fi
systemctl enable --now docker >/dev/null 2>&1 || true
for _ in $(seq 1 15); do docker info >/dev/null 2>&1 && break; sleep 1; done
docker info >/dev/null 2>&1 || { warn "the Docker daemon isn't responding (systemctl status docker)"; exit 1; }

# 2) Config with the JSON API ON -------------------------------------------
# Self-healing: write our config if none exists OR if an existing one doesn't
# enable JSON (e.g. left over from a manual `docker run` with default settings —
# the usual reason /search?format=json returns 403). We back up before rewriting.
mkdir -p "$CONF"
need_write=0
if [ ! -f "$CONF/settings.yml" ]; then
  need_write=1
elif ! grep -qiE 'json' "$CONF/settings.yml"; then
  warn "existing $CONF/settings.yml doesn't enable the JSON API — backing it up and rewriting."
  cp -a "$CONF/settings.yml" "$CONF/settings.yml.bak.$(date +%s)" 2>/dev/null || true
  need_write=1
else
  say "$CONF/settings.yml already enables JSON — leaving it untouched."
fi
if [ "$need_write" -eq 1 ]; then
  say "Writing $CONF/settings.yml (JSON API enabled, limiter off) …"
  SECRET="$(openssl rand -hex 32 2>/dev/null || head -c32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  cat > "$CONF/settings.yml" <<YML
# Minimal SearXNG config written by DRONGO. Inherits SearXNG's defaults; we only
# set a secret key, turn OFF the bot-limiter (so a non-browser client like DRONGO
# can query it), and turn ON the JSON API that web_search consumes.
use_default_settings: true
server:
  secret_key: "$SECRET"
  limiter: false
  image_proxy: true
search:
  formats:
    - html
    - json
YML
fi

# 3) (Re)create the container ----------------------------------------------
say "Pulling $IMAGE (first pull can take a few minutes on a Pi) …"
docker pull "$IMAGE" || warn "pull failed — will try a cached image if present"
docker rm -f "$NAME" >/dev/null 2>&1 || true
say "Starting SearXNG on $BIND:$PORT …"
docker run -d --name "$NAME" --restart unless-stopped \
  -p "$BIND:$PORT:8080" -v "$CONF:/etc/searxng" "$IMAGE" \
  || { warn "docker run failed (see the message above)"; exit 1; }

# 4) Wait for it + verify the JSON API -------------------------------------
say "Waiting for SearXNG to answer …"
ok=0
for _ in $(seq 1 30); do
  if curl -fsS "http://$BIND:$PORT/search?q=test&format=json" >/dev/null 2>&1; then ok=1; break; fi
  sleep 2
done
if [ "$ok" -eq 1 ]; then
  say "SearXNG is up: http://$BIND:$PORT  — JSON API OK. Set SEARXNG_URL to this and restart drongo."
  exit 0
fi
warn "SearXNG didn't answer the JSON API in time. Diagnose with:"
warn "  docker logs $NAME"
warn "  curl 'http://$BIND:$PORT/search?q=test&format=json'"
warn "If that returns 403, JSON isn't enabled — set  search.formats: [html, json]  in"
warn "$CONF/settings.yml and:  docker restart $NAME"
exit 1
