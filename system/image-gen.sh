#!/usr/bin/env bash
# ===========================================================================
#  OPTIONAL local image generator for DRONGO — OnnxStream.
#  (github.com/vitoplantamura/OnnxStream — built to run Stable Diffusion on
#   tiny-RAM devices like a Raspberry Pi, so it suits the Rock Pi 4C+ far better
#   than stable-diffusion.cpp.)
#
#    sudo ./system/image-gen.sh
#    sudo XNNPACK_COMMIT=<hash> IMG_MODEL_GIT=<hf-repo-url> ./system/image-gen.sh
#
#  Builds the OnnxStream `sd` CLI into /opt/imggen. Then set (dashboard:
#  Control -> Settings -> Image generation, provider=local) the command:
#    /opt/imggen/sd --turbo --models-path /opt/imggen/models/<dir> --steps 1 \
#        --prompt {prompt} --output {out}
#
#  HONEST WARNINGS:
#   * Slow on a Pi (tens of seconds to minutes/image with SD-Turbo at 1 step) but
#     low-RAM-friendly. The cloud Pollinations default stays recommended for speed.
#   * OnnxStream pins a SPECIFIC XNNPACK commit in its README. If the XNNPACK build
#     fails, read OnnxStream's README and re-run with XNNPACK_COMMIT=<that hash>.
#   * Models are separate downloads (see OnnxStream's README) — pass IMG_MODEL_GIT
#     to git-lfs-clone one into /opt/imggen/models, or place files there yourself.
#   * Best-effort & re-runnable (NOT set -e). UNTESTED on your exact board — report
#     build errors and I'll adjust the script.
# ===========================================================================
set -uo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Please run with sudo:  sudo ./system/image-gen.sh"; exit 1; }

OPT=/opt/imggen
XNNPACK_COMMIT="${XNNPACK_COMMIT:-}"      # set to the commit OnnxStream's README pins
say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m    ! %s\033[0m\n' "$*"; }
export DEBIAN_FRONTEND=noninteractive
mkdir -p "$OPT"

say "1/4  build deps"
apt-get update -y || warn "apt update failed"
apt-get install -y --no-install-recommends git cmake build-essential wget ca-certificates \
  || warn "apt install failed"

say "2/4  XNNPACK (static lib OnnxStream links against)"
if [ -d "$OPT/XNNPACK/build" ] && find "$OPT/XNNPACK/build" -name 'libXNNPACK*' | grep -q .; then
  echo "    already built, skipping."
else
  rm -rf "$OPT/XNNPACK"
  if git clone https://github.com/google/XNNPACK.git "$OPT/XNNPACK"; then
    ( cd "$OPT/XNNPACK"
      if [ -n "$XNNPACK_COMMIT" ]; then git checkout "$XNNPACK_COMMIT" || warn "couldn't checkout $XNNPACK_COMMIT"; \
        else warn "no XNNPACK_COMMIT set — building HEAD; if it fails, use the commit from OnnxStream's README"; fi
      mkdir -p build && cd build \
        && cmake -DXNNPACK_BUILD_TESTS=OFF -DXNNPACK_BUILD_BENCHMARKS=OFF .. \
        && cmake --build . -j"$(nproc)" ) || warn "XNNPACK build failed"
  else
    warn "XNNPACK clone failed"
  fi
fi

say "3/4  OnnxStream (the 'sd' binary — slow build)"
if [ -x "$OPT/sd" ]; then
  echo "    already built, skipping."
else
  rm -rf "$OPT/OnnxStream"
  if git clone https://github.com/vitoplantamura/OnnxStream.git "$OPT/OnnxStream"; then
    ( cd "$OPT/OnnxStream/src" && mkdir -p build && cd build \
        && cmake -DMAX_SPEED=ON -DXNNPACK_DIR="$OPT/XNNPACK" .. \
        && cmake --build . -j"$(nproc)" ) || warn "OnnxStream build failed"
    sdbin="$(find "$OPT/OnnxStream/src/build" -name sd -type f 2>/dev/null | head -1)"
    [ -n "$sdbin" ] && cp "$sdbin" "$OPT/sd" || warn "build finished but no 'sd' binary found"
  else
    warn "OnnxStream clone failed"
  fi
fi
[ -x "$OPT/sd" ] && echo "    sd built: $OPT/sd" || warn "sd not built"

say "4/4  model"
mkdir -p "$OPT/models"
if [ -n "${IMG_MODEL_GIT:-}" ]; then
  apt-get install -y git-lfs >/dev/null 2>&1 && git lfs install || warn "git-lfs unavailable"
  if git clone "$IMG_MODEL_GIT" "$OPT/models/$(basename "$IMG_MODEL_GIT" .git)"; then
    echo "    model -> $OPT/models/$(basename "$IMG_MODEL_GIT" .git)"
  else warn "model clone failed"; fi
else
  warn "No IMG_MODEL_GIT set. Grab an OnnxStream model (SD-Turbo or SD-1.5) from"
  warn "https://github.com/vitoplantamura/OnnxStream (Download the Weights) into $OPT/models/<dir>."
fi
chown -R root:root "$OPT" 2>/dev/null || true

cat <<EOF

Done. Once 'sd' is built and a model dir is in $OPT/models, set on the dashboard
(Control -> Settings -> Image generation), provider = local, command:

   $OPT/sd --turbo --models-path $OPT/models/<dir> --steps 1 --prompt {prompt} --output {out}

(drop --turbo and raise --steps for a full SD-1.5 model). Then:
   sudo systemctl restart drongo
Reminder: slow on a Pi — Pollinations (cloud) stays the sane default.
EOF
