"""Out-of-the-box knowledge. On first boot (and when BOOTSTRAP_VERSION bumps) this
seeds the agent's memory with a set of genuinely-reusable STARTER SKILLS, a few
ENVIRONMENT NOTES, and best-practice LESSONS — so DRONGO starts knowing a lot
instead of relearning the basics. Skills replace-by-name (idempotent); notes and
lessons are de-duplicated, so re-seeding never piles up duplicates.
"""

from __future__ import annotations

BOOTSTRAP_VERSION = 1

# --- Starter skills: correct, self-contained, reusable patterns --------------
STARTER_SKILLS = [
    {
        "name": "write-ppm-image",
        "description": "Write a binary PPM (P6) image with NO dependencies. PPM shows in "
                       "DRONGO's gallery — save then call add_to_gallery(path). Reuse for "
                       "any generative/fractal/plot image without pillow.",
        "code": '''def write_ppm(path, width, height, get_pixel):
    """get_pixel(x, y) -> (r, g, b) with each channel 0-255."""
    with open(path, "wb") as f:
        f.write(b"P6\\n%d %d\\n255\\n" % (width, height))
        row = bytearray(width * 3)
        for y in range(height):
            i = 0
            for x in range(width):
                r, g, b = get_pixel(x, y)
                row[i] = r & 255; row[i + 1] = g & 255; row[i + 2] = b & 255
                i += 3
            f.write(row)
''',
    },
    {
        "name": "mandelbrot-ppm",
        "description": "Render a Mandelbrot set to a standalone PPM image (compute-heavy — "
                       "good for generative_art / exercising the CPU). Then add_to_gallery(path).",
        "code": '''def mandelbrot(path, w=800, h=600, cx=-0.75, cy=0.0, scale=3.0, max_iter=200):
    with open(path, "wb") as f:
        f.write(b"P6\\n%d %d\\n255\\n" % (w, h))
        for y in range(h):
            row = bytearray(w * 3)
            zy0 = cy + (y / h - 0.5) * scale * h / w
            for x in range(w):
                zx0 = cx + (x / w - 0.5) * scale
                a = b = 0.0; i = 0
                while a * a + b * b <= 4.0 and i < max_iter:
                    a, b = a * a - b * b + zx0, 2 * a * b + zy0
                    i += 1
                if i >= max_iter:
                    r = g = bl = 0
                else:
                    t = i / max_iter
                    r = int(9 * (1 - t) * t * t * t * 255)
                    g = int(15 * (1 - t) * (1 - t) * t * t * 255)
                    bl = int(8.5 * (1 - t) * (1 - t) * (1 - t) * t * 255)
                j = x * 3; row[j] = r & 255; row[j + 1] = g & 255; row[j + 2] = bl & 255
            f.write(row)
''',
    },
    {
        "name": "game-of-life-step",
        "description": "One Conway's Game of Life generation (toroidal wrap). Core of any "
                       "cellular-automata simulation.",
        "code": '''def life_step(grid):
    """grid: list[list[int]] of 0/1. Returns the next generation."""
    h = len(grid); w = len(grid[0]) if h else 0
    out = [[0] * w for _ in range(h)]
    for y in range(h):
        for x in range(w):
            n = sum(grid[(y + dy) % h][(x + dx) % w]
                    for dy in (-1, 0, 1) for dx in (-1, 0, 1) if dx or dy)
            out[y][x] = 1 if (grid[y][x] and n in (2, 3)) or (not grid[y][x] and n == 3) else 0
    return out
''',
    },
    {
        "name": "canvas-game-template",
        "description": "Minimal HTML5 canvas game skeleton: requestAnimationFrame loop + arrow "
                       "keys. Save as index.html under the project folder — opens from Projects.",
        "code": '''<!doctype html><meta charset=utf-8><title>Game</title>
<canvas id=c width=480 height=320 style="background:#111;display:block;margin:20px auto"></canvas>
<script>
const cv=document.getElementById('c'),ctx=cv.getContext('2d');
const keys={};onkeydown=e=>keys[e.key]=1;onkeyup=e=>keys[e.key]=0;
let x=240,y=160;
function loop(){
  if(keys.ArrowLeft)x-=3; if(keys.ArrowRight)x+=3;
  if(keys.ArrowUp)y-=3; if(keys.ArrowDown)y+=3;
  x=Math.max(8,Math.min(cv.width-8,x)); y=Math.max(8,Math.min(cv.height-8,y));
  ctx.clearRect(0,0,cv.width,cv.height);
  ctx.fillStyle='#3e6';ctx.fillRect(x-8,y-8,16,16);
  requestAnimationFrame(loop);
}
loop();
</script>
''',
    },
    {
        "name": "live-dashboard-backend",
        "description": "The DRONGO live-dashboard pattern: a data.py that prints ONE JSON object "
                       "and EXITS (no server!). The HTML polls GET /data/projects/<name>/data.py.",
        "code": '''# data.py  —  run per-request by DRONGO; must print ONE json object and exit.
import json, os, time
data = {
    "time": time.strftime("%H:%M:%S"),
    "loadavg": (os.getloadavg()[0] if hasattr(os, "getloadavg") else 0),
}
print(json.dumps(data))
# In the HTML:  setInterval(()=>fetch('/data/projects/NAME/data.py')
#                 .then(r=>r.json()).then(d=>render(d)), 2000);
''',
    },
    {
        "name": "python-cli-template",
        "description": "Clean stdlib argparse CLI skeleton for a utility_script. Prefer stdlib so "
                       "a plain `python3 file.py` works for your human.",
        "code": '''import argparse

def main():
    ap = argparse.ArgumentParser(description="What this tool does.")
    ap.add_argument("input", help="input file")
    ap.add_argument("-n", "--count", type=int, default=1)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    # ... do the work with args.input / args.count ...

if __name__ == "__main__":
    main()
''',
    },
    {
        "name": "read-csv-stdlib",
        "description": "Read a CSV into a list of dicts keyed by the header row — stdlib only "
                       "(no pandas needed for simple data).",
        "code": '''import csv

def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))
''',
    },
    {
        "name": "c-program-timed",
        "description": "A native C program pattern + how to build/run it. gcc/g++/make are "
                       "installed. Add a run.sh (below) so the dashboard ▶ run button works.",
        "code": '''/* main.c  —  build+run:   gcc -O2 main.c -o app && ./app
   run.sh (so the dashboard can launch it):
       #!/usr/bin/env bash
       cd "$(dirname "$0")" && gcc -O2 main.c -o app && ./app
*/
#include <stdio.h>
#include <time.h>
int main(void) {
    struct timespec t0; clock_gettime(CLOCK_MONOTONIC, &t0);
    double s = 0; for (long i = 1; i < 100000000L; i++) s += 1.0 / i;
    struct timespec t1; clock_gettime(CLOCK_MONOTONIC, &t1);
    printf("sum=%.6f in %.3fs\\n", s,
           (t1.tv_sec - t0.tv_sec) + (t1.tv_nsec - t0.tv_nsec) / 1e9);
    return 0;
}
''',
    },
    {
        "name": "z88dk-cpc-hello",
        "description": "Build a runnable Amstrad CPC program with z88dk. Probe first: `which zcc`. "
                       "Produces a .cdt/.tap you load in an emulator.",
        "code": '''/* hello.c  —  build a real CPC tape image:
       zcc +cpc -create-app -o hello hello.c    # -> hello.cdt (and hello.tap)
   Retro tools to probe: which sdcc zcc pasmo ; [ -d "$CPCT_PATH" ] for CPCtelera. */
#include <stdio.h>
int main(void) {
    printf("HELLO FROM DRONGO\\n");
    return 0;
}
''',
    },
]

# --- Environment notes: facts about where it runs and how its tools work -----
STARTER_NOTES = [
    {"topic": "live dashboard pattern",
     "content": "To make a DYNAMIC dashboard: an HTML page whose JS polls "
                "GET /data/projects/<name>/<script>.py on the same origin. That python script "
                "must read its data, print ONE json object to stdout, and EXIT — DRONGO runs it "
                "per request. NEVER start your own web server / HTTPServer / Flask app.run — port "
                "8080 is DRONGO's and a long-running server can't be launched from the UI."},
    {"topic": "images and the gallery",
     "content": "generate_image(prompt) fetches a REAL raster image and saves it to the gallery. "
                "If your OWN code renders an image (fractal, plot, PPM/PNG), call "
                "add_to_gallery(path) to copy it into the gallery so your human sees it. "
                "The gallery renders PPM (netpbm) too, so you can write images with zero deps "
                "(see the write-ppm-image skill). Reference/sample images you DOWNLOAD do NOT "
                "belong in the gallery."},
    {"topic": "running code and dependencies",
     "content": "`pip install <pkg>` and `python <file>` already use your writable project venv. "
                "PREFER the Python standard library so a plain `python3 file.py` works in your "
                "human's own shell; if you do need a pip package, say so in the README and use the "
                "venv's absolute python path in the run command. No sudo — for a SYSTEM (apt) "
                "package call request_package(name, reason)."},
    {"topic": "native C/C++ and run.sh",
     "content": "gcc, g++ and make are installed — write real native tools when it fits (fast "
                "demos, sims, algorithms). Build+test in the shell (e.g. `g++ -O2 main.cpp -o app "
                "&& ./app`). For anything meant to be launched from the dashboard, add a small "
                "run.sh that compiles+runs it, so the ▶ run button works."},
    {"topic": "retro / 8-bit toolchain",
     "content": "If the retro toolchain is installed you can build for Amstrad CPC / ZX Spectrum "
                "/ Z80. Probe with `which sdcc zcc pasmo` and `[ -d \"$CPCT_PATH\" ]`: sdcc (C for "
                "Z80), zcc (z88dk: C+asm for CPC/Spectrum), pasmo (Z80 assembler / SymbOS), "
                "CPCtelera at $CPCT_PATH (Amstrad games). Produce a runnable .dsk/.cdt/.tap/.bin "
                "and document how to load it."},
    {"topic": "web search needs a key",
     "content": "The web_search tool needs a FREE search key (BRAVE_API_KEY / TAVILY_API_KEY / "
                "SERPER_API_KEY in /etc/drongo/drongo.env) for real results — keyless web search "
                "no longer works. Without a key it only manages definitional/Wikipedia hits. Use "
                "web_fetch(url) to read a specific page. If a search returns nothing useful, don't "
                "loop on it — work from what you know or your uploaded reference docs."},
    {"topic": "your knowledge tools",
     "content": "Before building, use recall_knowledge(query) to reuse your past skills/notes/"
                "lessons, and search_docs(query) to consult the reference docs your human uploaded "
                "(your SOURCE OF TRUTH — prefer them over guessing an API/spec). When you get a "
                "reusable snippet working, save_skill it; when you learn a durable fact, save_note "
                "it — so you compound over time."},
    {"topic": "sensing the hardware",
     "content": "You run on a headless Rock Pi (RK3399, ARM64). Read thermals/stats from /sys "
                "and /proc; discover_sensors lists the buses/sensors/cameras attached. Do this "
                "sparingly (it's fun once) and prefer compute-rich projects that USE the spare CPU "
                "(fractals, sims, ray tracers) over yet another temperature readout."},
]

# --- Best-practice lessons: one-line takeaways fed into every build ----------
STARTER_LESSONS = [
    "Prefer the Python standard library so a plain `python3 file.py` runs for your human; "
    "only pip-install when you must, and note it in the README.",
    "For anything meant to be launched from the dashboard, add a run.sh that builds AND runs it.",
    "Verify before you finish — read the file back or actually run it; never return final just "
    "because you're stuck or out of ideas.",
    "Keep each project in ONE folder under projects/<name>/ with a short README (what it is, how "
    "to run it, what it needs). Delete scratch files you don't need.",
    "When your code renders an image, save it (PPM/PNG) and call add_to_gallery so your human sees it.",
    "A live dashboard's backend is a python script that prints ONE json object and exits — never a "
    "long-running server.",
    "Build something REAL and finished, not a stub — and make it work for your human, not just for you.",
]


def seed(mem) -> int:
    """Load the starter knowledge if not already at the current version. Returns
    the number of skills seeded (0 if already bootstrapped)."""
    if (mem.recall("bootstrap_version") or 0) >= BOOTSTRAP_VERSION:
        return 0
    n = 0
    for s in STARTER_SKILLS:
        if mem.add_skill(s["name"], s["description"], s["code"]):
            n += 1
    have_topics = {x.get("topic") for x in (mem.recall("notes") or []) if isinstance(x, dict)}
    for note in STARTER_NOTES:
        if note["topic"] not in have_topics:
            mem.add_note(note["topic"], note["content"])
    have_lessons = set(mem.recent_lessons(50))
    for l in STARTER_LESSONS:
        if l not in have_lessons:
            mem.add_lesson(l)
    mem.remember("bootstrap_version", BOOTSTRAP_VERSION)
    return n
