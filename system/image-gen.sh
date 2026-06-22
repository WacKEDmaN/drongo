#!/usr/bin/env bash
# ===========================================================================
#  OPTIONAL local image generator for DRONGO (stable-diffusion.cpp).
#    sudo ./system/image-gen.sh                 # build the `sd` CLI into /opt/imggen
#    sudo IMG_MODEL_URL=<url> ./system/image-gen.sh   # also fetch a model
#
#  Builds the `sd` command-line tool (CPU, ggml/gguf) so the agent can make
#  images OFFLINE via tools.images.provider=local. Then set, on the dashboard
#  (Control -> Settings -> Image generation, provider=local) or in config.yaml:
#    /opt/imggen/sd -m /opt/imggen/model.gguf -p {prompt} -o {out} --steps 4 -W 512 -H 512
#
#  HONEST WARNING: image generation on a Rock Pi 4C+ is SLOW (minutes per image)
#  and RAM-hungry — use a small SD-1.5 / SD-Turbo gguf and few steps. The default
#  cloud provider (Pollinations) is far more practical for routine use; this is
#  for offline / air-gapped operation. Best-effort & re-runnable; NOT set -e.
#  This script has not been tested on your exact board — report build errors.
# ===========================================================================
set -uo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Please run with sudo:  sudo ./system/image-gen.sh"; exit 1; }

OPT=/opt/imggen
say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m    ! %s\033[0m\n' "$*"; }
export DEBIAN_FRONTEND=noninteractive

say "1/3  build deps (cmake, build-essential)"
apt-get update -y || warn "apt update failed"
apt-get install -y --no-install-recommends git cmake build-essential curl || warn "apt install failed"

say "2/3  build stable-diffusion.cpp (this takes a while on a Pi)"
if [ -x "$OPT/sd" ]; then
  echo "    already built, skipping."
else
  rm -rf "$OPT/src"; mkdir -p "$OPT"
  if git clone --depth 1 --recursive https://github.com/leejet/stable-diffusion.cpp "$OPT/src"; then
    if ( cd "$OPT/src" && cmake -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build -j"$(nproc)" ); then
      sdbin="$(find "$OPT/src/build" -name sd -type f 2>/dev/null | head -1)"
      [ -n "$sdbin" ] && cp "$sdbin" "$OPT/sd" || warn "build finished but no 'sd' binary found"
    else
      warn "stable-diffusion.cpp build failed"
    fi
  else
    warn "clone failed (network?)"
  fi
fi
[ -x "$OPT/sd" ] && echo "    sd built: $OPT/sd" || warn "sd not built"

say "3/3  model"
if [ -n "${IMG_MODEL_URL:-}" ]; then
  curl -fSL "$IMG_MODEL_URL" -o "$OPT/model.gguf" && echo "    model -> $OPT/model.gguf" \
    || warn "model download failed"
else
  warn "No IMG_MODEL_URL set. Drop a Stable Diffusion .gguf (small SD-1.5 / SD-Turbo)"
  warn "at $OPT/model.gguf — get one from huggingface."
fi
chown -R root:root "$OPT" 2>/dev/null || true

cat <<EOF

Done. If 'sd' built and a model is at $OPT/model.gguf, set on the dashboard
(Control -> Settings -> Image generation), provider = local, command:

   $OPT/sd -m $OPT/model.gguf -p {prompt} -o {out} --steps 4 -W 512 -H 512

Then restart:  sudo systemctl restart drongo
Reminder: this is SLOW on a Pi — Pollinations (cloud) stays the sane default.
EOF
