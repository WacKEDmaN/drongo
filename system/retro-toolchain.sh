#!/usr/bin/env bash
# ===========================================================================
#  DRONGO retro / Z80 cross-dev toolchain (OPTIONAL, best-effort).
#
#    sudo ./system/retro-toolchain.sh        (or:  sudo ./install.sh --retro)
#
#  Installs the tools the agent needs to build 8-bit software:
#    * sdcc      - Small Device C Compiler (Z80, etc.)            [apt]
#    * pasmo     - portable Z80 assembler (also the basis for     [apt]
#                  SymbOS / hand-written Z80 asm)
#    * z88dk     - C + asm for Z80 machines (Amstrad CPC, ZX       [source build]
#                  Spectrum, MSX...). Provides `zcc`.
#    * CPCtelera - Amstrad CPC game-dev framework (pulls in Mono) [source build]
#
#  Toolchains install to /opt/retro (root-owned, read-only to the agent). The
#  agent's shell picks them up automatically (see _project_env in tools.py).
#
#  NOTE: z88dk and CPCtelera compile from source and can take a while on a Pi
#  (and pull in build deps). This script is intentionally NOT `set -e`: a failed
#  component warns and is skipped rather than aborting the whole run. Re-runnable.
#
#  SymbOS: SymbOS apps are Z80 — `pasmo`/`sdcc` assemble/compile them. The full
#  SymbOS SDK GUI tooling is Windows-only (run under wine if you need it); see
#  https://www.symbos.de . We install the Z80 assembler basis here.
# ===========================================================================
set -uo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Please run with sudo:  sudo ./system/retro-toolchain.sh"; exit 1; }

OPT=/opt/retro
ENVF=/etc/drongo/retro.env
mkdir -p "$OPT" "$(dirname "$ENVF")"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m    ! %s\033[0m\n' "$*"; }

export DEBIAN_FRONTEND=noninteractive

say "1/4  apt packages (sdcc, pasmo + build deps)"
apt-get update -y || warn "apt update failed"
apt-get install -y --no-install-recommends \
  sdcc pasmo build-essential git curl unzip m4 dos2unix \
  libxml2-dev zlib1g-dev libpng-dev bison flex \
  || warn "some apt packages failed to install"
# CPCtelera's bundled tools need the Mono runtime. Installed on its own so a
# failure here can't block sdcc/pasmo above. mono-complete is the safe catch-all.
apt-get install -y mono-complete \
  || apt-get install -y mono-runtime libmono-system-drawing4.0-cil \
  || warn "mono failed to install — CPCtelera tools that need it won't run"

# --- z88dk -----------------------------------------------------------------
say "2/4  z88dk (source build — this can take several minutes)"
if [ -x "$OPT/z88dk/bin/zcc" ]; then
  echo "    already built, skipping."
else
  rm -rf "$OPT/z88dk"
  if git clone --depth 1 --recursive https://github.com/z88dk/z88dk.git "$OPT/z88dk"; then
    if ( cd "$OPT/z88dk" && chmod +x build.sh && ./build.sh ); then
      echo "    z88dk built."
    else
      warn "z88dk build failed — re-run later or build manually in $OPT/z88dk"
    fi
  else
    warn "z88dk clone failed (network?)"
  fi
fi

# --- CPCtelera (Amstrad CPC) ----------------------------------------------
say "3/4  CPCtelera (source build)"
if [ -d "$OPT/cpctelera/tools/sdcc" ] || [ -x "$OPT/cpctelera/cpct_winape.sh" ]; then
  echo "    already set up, skipping."
else
  rm -rf "$OPT/cpctelera"
  if git clone --depth 1 https://github.com/lronaldo/cpctelera.git "$OPT/cpctelera"; then
    # setup.sh builds its bundled toolchain; feed it 'y' for any prompt.
    if ( cd "$OPT/cpctelera" && yes | ./setup.sh ); then
      echo "    CPCtelera set up."
    else
      warn "CPCtelera setup failed — re-run later or see $OPT/cpctelera"
    fi
  else
    warn "CPCtelera clone failed (network?)"
  fi
fi

# --- env file the agent (and you) can source ------------------------------
say "4/4  writing $ENVF"
{
  echo "# DRONGO retro toolchain — paths for sdcc/z88dk/CPCtelera."
  echo "# sdcc + pasmo are in /usr/bin (apt). z88dk + CPCtelera are below."
  [ -d "$OPT/z88dk/bin" ]    && echo "export PATH=\"$OPT/z88dk/bin:\$PATH\""
  [ -d "$OPT/z88dk/lib/config" ] && echo "export ZCCCFG=\"$OPT/z88dk/lib/config\""
  [ -d "$OPT/cpctelera" ]    && echo "export CPCT_PATH=\"$OPT/cpctelera\""
} > "$ENVF"
chmod 0644 "$ENVF"
chown -R root:root "$OPT"

say "Retro toolchain done. Installed where found:"
command -v sdcc  >/dev/null && echo "    sdcc:  $(command -v sdcc)"   || warn "sdcc missing"
command -v pasmo >/dev/null && echo "    pasmo: $(command -v pasmo)"  || warn "pasmo missing"
[ -x "$OPT/z88dk/bin/zcc" ]      && echo "    zcc:   $OPT/z88dk/bin/zcc" || warn "z88dk/zcc missing"
[ -d "$OPT/cpctelera" ]          && echo "    cpctelera: $OPT/cpctelera" || warn "cpctelera missing"
echo
echo "The agent's shell picks these up automatically. Restart it to be sure:"
echo "    sudo systemctl restart drongo"
