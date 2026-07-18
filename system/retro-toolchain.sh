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
#    * CPCtelera - Amstrad CPC game-dev framework (needs Mono +    [source build]
#                  FreeImage; bundled SDCC builds from source)
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

# Cap build parallelism by RAM. z88dk/CPCtelera/SDCC compile heavy C++; a full
# `-j$(nproc)` (6 on the RK3399) exhausts a 4GB board's RAM, thrashes, and can
# take the box (and your SSH session) down. MAKEFLAGS caps the sub-makes; one
# job per ~1.5GB RAM keeps peak memory sane.
_ram_mb=$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 2000)
JOBS=1; [ "$_ram_mb" -ge 3000 ] && JOBS=2; [ "$_ram_mb" -ge 6000 ] && JOBS=4
export MAKEFLAGS="-j${JOBS}"
echo "    building with -j${JOBS} (RAM ${_ram_mb}MB) to avoid OOM"

# Install as many of the named packages as are available, and NEVER let one
# missing/renamed package abort the whole batch -- a plain `apt-get install A B C`
# installs NONE of them if any single name is unavailable. Try the fast batch
# first; on failure, fall back to installing each package on its own so every
# package that DOES exist still gets in.
apt_install() {
  apt-get install -y --no-install-recommends "$@" && return 0
  warn "batch install hit a snag — retrying each package individually"
  local p
  for p in "$@"; do
    apt-get install -y --no-install-recommends "$p" || warn "  skipped (unavailable?): $p"
  done
}

say "1/4  apt packages (sdcc, pasmo + build deps for z88dk & CPCtelera)"
apt-get update -y || warn "apt update failed"
# Ready-to-use compilers/assemblers + fetch tools.
apt_install sdcc pasmo build-essential git curl wget unzip m4 dos2unix pkg-config
# Build-from-source deps shared by z88dk and CPCtelera's bundled SDCC:
#   bison/flex/ragel/re2c    - lexer/parser generators z88dk's build invokes
#   libxml2-dev/zlib1g-dev/libgmp3-dev/libpng-dev - libs z88dk & SDCC link against
#   libboost-dev/-graph-dev  - SDCC's register allocator needs Boost.Graph
#   texinfo                  - z88dk docs build (this is the one most often missing on ARM)
#   libfreeimage-dev         - CPCtelera's image-conversion tools need FreeImage
apt_install libxml2-dev zlib1g-dev libgmp3-dev libpng-dev bison flex ragel re2c \
            libboost-dev libboost-graph-dev texinfo libfreeimage-dev
# CPCtelera's bundled tools need the Mono runtime. Kept separate from the above so
# a mono hiccup can't block the compilers. mono-complete is the safe catch-all.
apt-get install -y mono-complete \
  || apt-get install -y mono-runtime libmono-system-drawing4.0-cil \
  || warn "mono failed to install — CPCtelera tools that need it won't run"

# --- z88dk -----------------------------------------------------------------
say "2/4  z88dk (build from the official release tarball)"
# IMPORTANT: use the release '-src-' tarball, NOT github's auto-generated "Source
# code (tar.gz)" — that one is missing generated files and will NOT compile.
Z88DK_URL="https://github.com/z88dk/z88dk/releases/download/v2.4/z88dk-src-2.4.tgz"
if [ -x "$OPT/z88dk/bin/zcc" ]; then
  echo "    already built, skipping."
else
  rm -rf "$OPT/z88dk" /tmp/z88dk-src /tmp/z88dk-src.tgz
  mkdir -p /tmp/z88dk-src
  if curl -fsSL "$Z88DK_URL" -o /tmp/z88dk-src.tgz && tar xzf /tmp/z88dk-src.tgz -C /tmp/z88dk-src; then
    BUILDSH="$(find /tmp/z88dk-src -maxdepth 3 -name build.sh 2>/dev/null | head -1)"
    SRCDIR="$(dirname "$BUILDSH")"
    if [ -n "$BUILDSH" ] && [ -d "$SRCDIR" ]; then
      mv "$SRCDIR" "$OPT/z88dk"
      Z88LOG="$OPT/z88dk-build.log"
      # tee the build to a log so the REAL error is saved (it otherwise scrolls
      # off-screen); pipefail (set above) keeps the pipeline status == build.sh's.
      if ( cd "$OPT/z88dk" && export ZCCCFG="$OPT/z88dk/lib/config" PATH="$OPT/z88dk/bin:$PATH" \
           && chmod +x build.sh && ./build.sh ) 2>&1 | tee "$Z88LOG"; then
        echo "    z88dk built."
      else
        warn "z88dk build failed. FULL LOG: $Z88LOG"
        warn "last 25 lines (send me these to diagnose):"
        tail -n 25 "$Z88LOG" 2>/dev/null | sed 's/^/      /'
      fi
    else
      warn "couldn't find build.sh inside the z88dk tarball"
    fi
  else
    warn "z88dk tarball download/extract failed (network?)"
  fi
  rm -rf /tmp/z88dk-src /tmp/z88dk-src.tgz
fi

# --- CPCtelera (Amstrad CPC) ----------------------------------------------
say "3/4  CPCtelera (source build)"
if [ -d "$OPT/cpctelera/tools/sdcc" ] || [ -x "$OPT/cpctelera/cpct_winape.sh" ]; then
  echo "    already set up, skipping."
else
  rm -rf "$OPT/cpctelera"
  if git clone --depth 1 https://github.com/lronaldo/cpctelera.git "$OPT/cpctelera"; then
    # setup.sh builds its bundled toolchain; feed it 'y' for any prompt.
    CPCLOG="$OPT/cpctelera-build.log"
    if ( cd "$OPT/cpctelera" && yes | ./setup.sh ) 2>&1 | tee "$CPCLOG"; then
      echo "    CPCtelera set up."
    else
      warn "CPCtelera setup failed. FULL LOG: $CPCLOG"
      warn "last 25 lines (send me these to diagnose):"
      tail -n 25 "$CPCLOG" 2>/dev/null | sed 's/^/      /'
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
