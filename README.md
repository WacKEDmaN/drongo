# DRONGO - *lobotomized* *AI Agent*

**Digital Resource-Optimizing Neural Gadget for Overthinking** — a self-directed,
autonomous maker-agent for the **Rock Pi 4C+ (RK3399)** running headless Debian.

Set it and forget it. It wakes up on a loop, decides its own little project —
a browser game, a generative image, a handy script, a live hardware dashboard,
a short research note — builds it with real tools, and writes a journal entry
you can read later from a web dashboard. It can ping you on **Discord** or
**blink an LED** you wire to a GPIO when it makes something good. It runs on
**free cloud LLMs** (with an **optional local model** as a never-fail fallback),
and it is wrapped in serious, tamper-resistant safety rails so you can actually
trust it with the keys.

> Philosophy (from the project spec): **"Local Autonomy, Global Isolation."**

---

## 🟢 Start here (the whole thing in 4 steps)

**No Linux expertise needed.** If you can flash an SD card and copy-paste, you can do this.

**0. What you need**
- A Rock Pi 4C+ (the **4 GB** model is best; 2 GB works with a smaller brain; 1 GB is tight).
- Storage flashed with **Debian** (eMMC or NVMe SSD strongly preferred over a microSD).
- The Pi on your network, and the ability to **SSH** into it.
- *(Optional, makes it smarter)* free API keys — links in the provider table below.

**1. Get the code onto the Pi** (either works):
```bash
git clone <your-fork-url> drongo && cd drongo     # preferred (enables self-update)
# — or — download the ZIP, unzip it, and `cd` into the folder
```

**2. Install (one command):**
```bash
sudo ./install.sh
```
It shows a banner, then **asks a few quick setup questions** — install a local fallback
model?, disable the desktop to free RAM?, install the retro toolchain?, build the local
image generator? — each with a sensible default, so you can just press **Enter** through
them. Then it checks your system, sets everything up, and ends with a **health check**.
When it prints `✓ DRONGO is installed and running` it's live — running on the **free
cloud providers** once you add a key in the wizard (step 4), or on a local model if you
opted into one. *(Prefer flags? `--local --strip-desktop --retro --imggen --model NAME`
pre-answer the questions, and `--yes` accepts every default without prompting.)*

**3. Did it work?**
```bash
sudo drongo doctor
```
You want **`VERDICT: ✓ READY`**. Then open the dashboard in a browser:
**`http://<your-pi-ip>:8080/`** and watch it build things. Check in whenever you like.

**4. Setup wizard (runs automatically at the end of install):** it asks for a Discord
webhook, an LED pin, and any free API keys — **press Enter to skip anything**, and it
restarts DRONGO for you. Re-run it any time with `sudo /opt/drongo/configure.sh`. (You
can also hand-edit `/etc/drongo/drongo.env` — see [Alerts](#alerts--discord-or-an-led).)

> **Changed your mind?** `sudo /opt/drongo/uninstall.sh` removes it cleanly (keeps your
> data unless you add `--purge`). Jump to [Troubleshooting](#troubleshooting) if anything looks off.

---

## What you get

| Capability | How |
|---|---|
| Decides what to do | Self-directed ideation each cycle, avoids repeating itself |
| Writes & runs scripts | Sandboxed `shell` + file tools, each project kept in its own `projects/<name>/` folder |
| Makes images | Keyless, free generation (Pollinations) into a gallery; optional local generator (OnnxStream) |
| Builds games / dashboards | HTML/JS written to the workspace, served by the dashboard |
| Senses its hardware | Scans i2c/spi/1-wire/thermals/USB/cameras, builds dashboards |
| Learns from its work | Indexes its own repo + past projects, auto-harvests reusable skills, retrieves relevant knowledge (RAG), downloads skill packs, and curates a fine-tuning dataset |
| Uses YOUR docs as truth | Upload reference docs (API/specs/datasheets) — indexed with FTS5 and searched/injected when it builds, so it stops guessing |
| Extends via MCP | Connect Model Context Protocol servers (stdio/http) — their tools become the agent's tools, sandboxed like everything else |
| Talk to it & steer it | A **Chat** tab — ask what it's doing or tell it what to build next, any time (even mid-project); teaching it facts saves them to memory |
| Shows its token usage | Per-provider tokens (in/out) + totals, charted live, with the latest call's count by the thinking stream |
| Installs its own packages | Requests apt packages; a scoped root helper installs the ones you allow (policy + hard allow-list) |
| Updates itself | **Request-based**, applied by a privileged root updater with rollback |
| Alerts you | Discord webhook, a GPIO LED you wire up, ntfy, or any command — pick any combo |
| Stays alive | systemd + in-agent watchdog + external observer + SoC hardware watchdog |
| Can't hurt itself/you | Immutable safeguard, OS sandbox, resource caps, crash-loop safe mode |

---

## Hardware & the brain

The RK3399 is a **6-core CPU (2×A72 + 4×A53), up to 4 GB RAM, no usable GPU/NPU
for LLMs** — so DRONGO's brain is the **free cloud providers** below, and the heavy
CPU work goes into its *projects* (fractals, sims, native code), not inference.

**Storage:** use **eMMC or an NVMe SSD**, not a microSD. The agent writes a lot
(SQLite, logs, artifacts) and SD cards die from that. Put `/var/lib/drongo` on the
fastest storage you have.

**Optional local model:** want an offline never-fail fallback for when the cloud
tiers are all rate-limited? It's **off by default** (~2 GB RAM) — add it with the
installer's prompt or `--local`. Full guide: **[docs/local-model.md](docs/local-model.md)**.

**Providers wired in** (add the keys you want, skip the rest — a provider with no
key is silently skipped). Tried top-to-bottom, **free first, paid Claude a capped
last resort** (the optional local model sits below them all):

| Provider | Cost | Get a key |
|---|---|---|
| **Cerebras** | free, very fast | <https://cloud.cerebras.ai> |
| **Groq** | free, fast | <https://console.groq.com> |
| **Google Gemini** | free tier | <https://aistudio.google.com/apikey> |
| **Mistral** | free tier | <https://console.mistral.ai> |
| **OpenRouter** | free models | <https://openrouter.ai/keys> |
| **Pollinations** | free (needs a free `pk_` token — no card) | <https://auth.pollinations.ai> |
| **Claude (Anthropic)** | **paid** — Haiku 4.5, last-resort, capped at 200 calls/day | <https://console.anthropic.com> |
| **Local Ollama** *(optional, off by default)* | free, no limits | `--local` at install, or flip it on in the config |

Claude uses the **native Anthropic API** (not an OpenAI shim) and needs
`pip install anthropic` (already in `requirements.txt`). It's reached only when
every free provider is simultaneously rate-limited, so spend stays minimal; raise
`daily_limit` or set `model: claude-sonnet-4-6` / `claude-opus-4-8` in the config
if you want more of it. More free options (GitHub Models, NVIDIA NIM, Pollinations)
are commented in `config.example.yaml`.

> Heads-up on *keyless*: there's no longer a reliable no-key text provider —
> Pollinations (once anonymous) now requires a free `pk_` token for text
> generation. So "free" here means *free tier with a free key*, not *no key*.

> Providers can be **added, removed and re-ordered live from the dashboard**
> (Control → Settings), and the router **backs off and auto-retries** a failing
> provider (429 / 5xx / 404) instead of wedging — so a retired model id no longer
> stalls the agent until you restart it. Gemini defaults to `gemini-flash-latest`,
> which tracks Google's current Flash model so it won't 404 when a version is retired.

Cloud-only, the 4 GB is comfortable (the agent is capped at `MemoryMax=1200M`).
Running a **local model**? Add zram for headroom — see **[docs/local-model.md](docs/local-model.md)**.

---

## The safety architecture (why you can trust it unattended)

Five nested layers. The agent sits **unprivileged at the centre and cannot reach
outward**; each outer layer is owned by root and recovers the one inside it.

```
1  SoC hardware watchdog (/dev/watchdog)   kernel/system wedged  → board reboots
2  External observer (root timer)          crash-loop            → rollback + alert
3  systemd sandbox + cgroups + WatchdogSec hang / OOM            → kill + restart, capped CPU/RAM
4  safeguard.py (root-owned 0444)          denylist + integrity self-check; tamper → refuse
5  DRONGO agent (unprivileged 'drongo')    sandboxed to /var/lib/drongo only
```

- **`agent/safeguard.py` is the conscience, in its own file, installed
  `root:root` mode `0444`.** The agent runs as `drongo` and **physically cannot
  modify it** (the OS blocks the write; `ProtectSystem=strict` makes `/opt/drongo`
  read-only to the process). The file also **verifies its own SHA-256, owner and
  permissions on every shell call** and, in strict mode, **refuses to run if
  anything is off** (fail closed).
- **Self-update can't let the agent rewrite itself.** The agent only *requests*
  an update (drops a marker); the **root `drongo-update` service** pulls from your
  trusted git remote, syntax-checks, **re-seals the safeguard**, restarts, and
  **rolls back automatically** if the new code won't start.
- **Crash-loop safe mode.** If the agent restarts too many times in a short
  window it boots into **SAFE MODE**: shell and self-update disabled, long sleeps,
  and it alerts you — instead of thrashing the box.
- **The external observer** (independent, root, stdlib-only so it survives a
  broken agent) restarts a wedged agent, rolls back a crash-looping one to the
  last-known-good commit, watches temperature/load/disk, and — only if you opt in
  — reboots the host as an absolute last resort.

---

## Quick install (on the Pi)

```bash
# 1. Flash Debian (Armbian/Radxa) to eMMC/NVMe, boot headless, SSH in.
# 2. Get the code and run the installer as root — it asks a few setup questions:
git clone <your-fork-url> drongo && cd drongo
sudo ./install.sh
#   You'll be asked (each with a default; Enter accepts it):
#     • local Ollama model        • disable the desktop to free RAM?
#     • install the retro toolchain?   • build the local image generator?
#   Prefer non-interactive? Pass flags to pre-answer, --yes to take all defaults:
#     sudo ./install.sh --yes --strip-desktop --retro
```

> **Retro / 8-bit dev (optional):** answer **yes** to the retro prompt (or pass
> `--retro`, or run `sudo ./system/retro-toolchain.sh` any time). It installs
> **sdcc**, **z88dk**, **CPCtelera** and **pasmo** so the agent can build for the
> Amstrad CPC, ZX Spectrum and Z80 (incl. SymbOS assembly). z88dk + CPCtelera
> compile from source, so it's a heavy, best-effort step — it won't break the base
> install, and you can re-run it (on failure it saves the build log to
> `/opt/retro/*-build.log` so the error is recoverable).

> **Local image generator (optional):** answer **yes** to the image-gen prompt (or
> `--imggen`, or `sudo ./system/image-gen.sh`) to build **OnnxStream** into
> `/opt/imggen`. It's slow on a Pi — the keyless cloud generator stays the default;
> set image `provider: local` in Settings once it's built.

The installer (see [`install.sh`](install.sh)) does everything: packages, the
`drongo` user, `/opt/drongo` (root-owned) + `/var/lib/drongo` (agent-writable),
the venv, (optionally) Ollama + a local model, **locks the safeguard to 0444 and
seals its hash**, **arms the SoC hardware watchdog**, and installs/enables all the
systemd units.

Then add your keys (and optionally a Discord webhook — see [Alerts](#alerts--discord-or-an-led)):

```bash
sudoedit /etc/drongo/drongo.env       # CEREBRAS / GROQ / GEMINI / MISTRAL / OPENROUTER (+ ANTHROPIC_API_KEY for Claude)
                                      # and DISCORD_WEBHOOK_URL for alerts
sudo systemctl restart drongo drongo-web
```

> **You need at least one provider now** — the free cloud tiers are the default brain
> (the local model is optional). Add a free key above, or install a local floor with
> `sudo ./install.sh --local`.

Check on it:

```bash
journalctl -u drongo -f                                   # live log
sudo drongo doctor
# Dashboard:  http://<pi-ip>:8080/
```

---

## What the manual steps actually are (if you prefer not to use `install.sh`)

1. **OS:** flash Debian, then free RAM by *disabling* (not removing) the GUI:
   `sudo systemctl set-default multi-user.target` + `sudo systemctl stop display-manager`.
   Don't `apt purge`/`autoremove` the desktop on a vendor SBC image — it can pull
   out hardware/firmware packages. (Sensors/GPIO/I2C come from the kernel + device
   tree, so a headless box still has full hardware access.)
2. **User:** create a system user `drongo` with `nologin`; add it to `i2c`,
   `gpio`, `spi`, `video` groups so it can read sensors.
3. **Layout:** code → `/opt/drongo` (**chown root:root**), runtime →
   `/var/lib/drongo` (**chown drongo**), config → `/etc/drongo`.
4. **Python:** `python3 -m venv /opt/drongo/.venv && .venv/bin/pip install -r requirements.txt`.
5. **Seal the guard:** `python -m agent seal` then
   `chmod 0444 agent/safeguard.py*` and `chown root:root` them.
6. **Ollama (optional — skip for cloud-only):** install, `ollama pull qwen2.5:3b-instruct`,
   and set the `local` provider `enabled: true` in the config.
7. **Hardware watchdog:** add `RuntimeWatchdogSec=20s` to `/etc/systemd/system.conf`.
8. **Services:** copy `systemd/*.{service,timer}` to `/etc/systemd/system/`,
   `daemon-reload`, enable `drongo`, `drongo-web`, `drongo-observer.timer`,
   `drongo-update.timer`, `drongo-pkg.timer`.

---

## Operating it

Most of this is now in the **dashboard** at `http://<pi-ip>:8080/` (password-protected,
LAN-only) — a **"command centre"** styled after desktop agent apps: a **left nav rail**,
a **top bar with always-visible host vitals (CPU · RAM · temp)** and today's token
meter, and a **command palette on ⌘K / Ctrl-K** to jump to any view or run any action.
It's a dark cockpit; the accent colour is switchable (amber/mint/ice/synth) from the
rail. The rail collapses to a hamburger on phones.

- **Activity** — the default view, a **three-pane live cockpit**: the **live-thinking
  stream** (everything it's doing, untruncated) down the middle, and an inspector on the
  right showing **what it's building now**, a mini **latest-requests** list (tokens
  in→out), and **host stats**. This is where you *watch it work*.
- **Requests** — **every LLM call, metered per request**: time · purpose
  (ideate/plan/execute/critique/chat) · provider·model · **tokens in · out · total** ·
  latency · status. Plus summary tiles (calls, in/out, in:out ratio), a per-provider
  in/out token chart, and the aggregate usage table. Full token observability.
- **Chat** — **talk to DRONGO and steer it, any time — even while it's building.**
  Multi-turn (it remembers the conversation), markdown replies with code blocks, a
  **provider picker** (auto/router-order or a specific provider), per-reply provider +
  token counts, copy/regenerate/clear. (Watch it think on the Activity tab.) It can act
  on you: "build X next" queues that project, a standing preference becomes its mission,
  and teaching it a fact saves it to memory. See [Chat & steering](#chat--steering).
- **Projects** — everything it built (HTML games/dashboards open in a click), each with
  **tags** and a **🔧 Fix this** button — flag a broken one and the agent works it *before*
  starting anything new.
- **Gallery** — every image it has generated (PNG/PPM), in a lightbox.
- **Files** — browse the agent's workspace, view/run files, see its **package requests**,
  and set the **Install policy** (see [Letting it install packages](#letting-it-install-packages-scoped)).
- **Brain** — steer it and see everything it has *learned*: **suggest its next project**,
  set its **standing mission**, a knowledge-base summary (repo files indexed /
  skills / notes / lessons / training examples), saved **skills** (code view + delete),
  **notes** and **lessons**. Import a skill by pasting JSON or downloading a pack from a URL,
  and download the curated **training dataset** (JSONL). Plus a **raw memory browser** —
  every key in its long-term store, inspectable (API keys masked) and deletable, so you
  see and control exactly what it remembers.
- **Settings** — Run-now / Pause / Resume / Stop / Restart and **full control over
  everything**: API keys, **add / remove / re-order LLM providers**, per-provider
  enable/model, hardware scan, Discord / ntfy / LED, personality & interests, and all the
  cooldown/loop timers. Saved settings live in the agent's DB and apply on the next
  restart ("Save & Restart" does both).
- **Help** — an SSH/admin cheat-sheet (where the key files live, the common commands).

The dashboard template lives in [`agent/dashboard.html`](agent/dashboard.html) (rendered by
the Flask app) if you want to tweak the UI.

> Note: the dashboard is plain HTTP on your LAN (behind the password), so keys you
> type into Settings cross the LAN in clear — same as the login itself. Fine for a
> trusted home network; if you don't want keys in the browser at all, keep using
> `/etc/drongo/drongo.env` + `sudo drongo configure`. Keys are stripped from the
> agent's own shell so a prompt-injected script can't read them.

| You want to… | Do this |
|---|---|
| See/launch what it built | Dashboard → Projects (HTML opens in a new tab) |
| Pause / resume / restart | Dashboard → Control (or `touch …/workspace/PAUSE`, remove to resume) |
| Send a broken project back for fixing | Dashboard → Projects → **🔧 Fix this** |
| Delete a project (removes its files too) | Dashboard → Projects → **🗑 Delete** |
| Run a cycle right now | Dashboard → Control → **▶ Run a cycle now** |
| Watch it live | `journalctl -u drongo -f` |
| Steer its interests | edit `interests:` in `/etc/drongo/config.yaml`, restart |
| Force a code update | `sudo systemctl start drongo-update` |
| Check health/guard | `sudo drongo doctor` |
| **Wipe ALL projects + history** (keeps your settings/keys) | `sudo drongo reset` |

The agent only pushes an alert when it finishes something with artifacts (set
`alerts.notify_every_cycle: true` for a ping every cycle).

---

## MCP tool servers

Give the agent external tools via the **Model Context Protocol**. On the **Brain**
tab, add an MCP server — **stdio** (a command like `npx -y @modelcontextprotocol/
server-filesystem /path`, needs Node) or **http** (a URL). Its tools become
`mcp__<server>__<tool>` that the ReAct loop can call like any built-in; **test** a
server from the dashboard to see the tools it exposes before relying on it. Servers
connect at agent startup (add one → restart).

The client is dependency-light (stdlib JSON-RPC + `requests`, no heavy SDK). An MCP
server runs inside the agent's **own sandbox** (unprivileged `drongo`, no sudo) and
gets a clean environment, so it's no more privileged than the `shell` tool and never
sees DRONGO's LLM keys. MCP tools are disabled in **safe mode**.

---

## Reference docs — your source of truth (RAG)

Upload documentation the agent should treat as **authoritative** — API references,
datasheets, hardware specs, your own notes — on the **Brain** tab (or scp a folder
into `runtime/docs/`). Text is chunked and indexed with **SQLite FTS5** (BM25
ranking, no embeddings/torch, so it's light on the Pi). The agent then:

- **searches these before guessing** (a `search_docs` tool), and
- gets the most relevant passages **injected into the build context** when it starts
  a related project, marked as the source of truth.

Manage it all from the browser — upload (`.md/.txt/.py/.html/.json/.csv/…`; binaries
skipped), list, delete, and a live search box to test retrieval. Uploads are capped
at 32 MB to spare the SD card.

---

## Chat & steering

The **Chat** tab is a conversation with DRONGO itself — not a raw model window. It's
answered by the **dashboard process**, separate from the agent loop, so it replies
instantly **even while the agent is mid-project**. Beyond answering, it can act on you:

- **"build a snake game next"** → queued as its next project (jumps the loop's queue).
- **"focus on retro stuff from now on"** → becomes its standing **mission** (biases ideation).
- **"remember I prefer tabs over spaces"** → saved to memory as a note it retrieves later.

So the same box lets you *ask what it's doing* and *steer where it goes* — and teaching it
things is a genuine learning channel (chat facts land in its knowledge base). Token usage
for every call is metered per-provider and charted on Home.

---

## Letting it install packages (scoped)

The agent can't `sudo`, and its sandbox blocks it from touching the system — so when it
needs an apt package (a compiler, a C library, a CLI tool) it **requests** one, and a small
**root helper** (`drongo-pkg`, on a 2-minute timer) installs it *only if your policy allows*.
The helper never runs an arbitrary command — only `apt-get install` of a **validated package
name** (no options, no paths, no local `.deb`) — so it can't be tricked into arbitrary root.

Two allow-lists decide what's permitted (a package installs if **either** matches, or the
mode is `auto`):

- **Dashboard list** — Files → **Install policy**. `manual` + an allow-list you edit live in
  the browser, or `auto` (install any valid package it asks for). Convenient, but editable by
  the same user the agent runs as, so treat it as a *soft* control.
- **Hard list** — `/etc/drongo/pkg-allow.conf`, **root-owned so the agent can't touch it**.
  One package/glob per line; edit it over SSH. The dashboard shows it **read-only**. This is
  the tamper-proof baseline.

Default is `manual` with an empty list, so out of the box it installs **nothing** until you
allow something. Watch it work with `journalctl -u drongo-pkg -f`.

---

## Skills & self-learning (the Brain tab)

DRONGO accumulates a knowledge base it reuses over time — the realistic form of
"self-improving" on a Pi (it can't *train* a model on a 4 GB CPU board, so instead it
**learns by retrieval** and **curates a dataset you can fine-tune off-box**):

- **Starter knowledge** — it ships **bootstrapped**: ~9 reusable starter skills (PPM/image
  writers, a Mandelbrot renderer, Game of Life, a canvas-game template, the live-dashboard
  pattern, a CLI template, native-C + retro/Z80 patterns), plus environment notes and
  best-practice lessons — seeded on first boot so it knows a lot day one.
- **Skills** — when a project finishes it **auto-harvests** one reusable code snippet into
  its library. Import your own on the **Brain** tab by pasting `{"name","description","code"}`
  JSON, or **download** a skill (or a `{"skills":[…]}` pack) from a URL — stored, never auto-run.
- **Notes & lessons** — research findings and one-line takeaways it saves as it works,
  **plus anything you teach it in [Chat](#chat--steering)** (facts you tell it are saved and retrieved).
- **Its own repo** — at boot it **indexes its own code + docs** (`agent/*.py`, `system/*.py`,
  the docs) so it can answer "how does my own X work?" — the repository is its first-class context.
- **Retrieval (RAG)** — before each build it pulls the most relevant of *all* of the above,
  **plus its past projects**, back into context (lightweight, embeddings-free, so it's light on
  the Pi). The agent can also search it on demand with the `recall_knowledge` tool.
- **Training dataset** — every finished project appends a chat-format example (task → the
  code it produced) to `workspace/dataset/train.jsonl`. The Pi **curates** the corpus; when you
  want an actual fine-tuned model you run training **elsewhere** (a rented GPU / a cloud
  fine-tune API) and point a provider at it. Download the JSONL from the Brain tab.

---

## Alerts — Discord or an LED

No phone needed. Enable **any combination** of channels in `/etc/drongo/config.yaml`
under `alerts:` — they all fire together. The root observer/updater also use Discord
(and ntfy) to warn you about crash-loops, rollbacks, and host health.

**Discord (easiest):**
1. In Discord: **Server Settings → Integrations → Webhooks → New Webhook**, pick a
   channel, **Copy Webhook URL**.
2. Put it in `/etc/drongo/drongo.env`: `DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...`
   (and the same line in `/etc/drongo/observer.env` as `DRONGO_DISCORD_WEBHOOK=...`).
3. `alerts.discord.enabled: true` (it already is in the example), then
   `sudo systemctl restart drongo`.

**LED on a GPIO pin:**
1. Wire it: **GPIO line → ~330 Ω resistor → LED (long leg / +) → LED (−) → a GND pin.**
2. Find which `gpiochip` and **line offset** your pin is: `gpioinfo` (from the
   `gpiod` package the installer adds). The RK3399 exposes `gpiochip0`–`gpiochip4`;
   the number you want is the **line offset on that chip**, *not* the board pin number.
3. Set it in `config.yaml`:
   ```yaml
   alerts:
     led:
       enabled: true
       chip: /dev/gpiochip0
       line: 17           # <- your line offset from gpioinfo
       active_high: true  # false if the LED is wired active-low
   ```
4. `sudo systemctl restart drongo`. It blinks 3× on a normal alert, 6× on urgent.
   (Needs `python-periphery`, already in `requirements.txt`.)

> Quick test: `sudo systemctl restart drongo` and watch — or trigger one cycle with
> `sudo drongo once`.
> The **`command`** channel can run any script on an alert (it gets `DRONGO_ALERT_*`
> env vars) if you want to drive something more exotic.

---

## Security — who can reach it

Designed so **only you, on your LAN**, can reach it — and nothing from the internet can.

**What listens on the network (the whole attack surface):**

| Service | Port | Exposure |
|---|---|---|
| Dashboard | 8080 | **Password-protected** (HTTP Basic) **+ LAN-only** (kernel-enforced). No internet. |
| Ollama (model) | 11434 | *Only if you enabled the local model.* **localhost only** (`OLLAMA_HOST=127.0.0.1`) — never on the LAN. |
| SSH | 22 | Yours, managed by you. The optional firewall limits it to the LAN. |
| The agent / observer / updater / pkg-installer | — | **No listeners.** They only make *outbound* calls. |

**How "only me" is enforced (defence in depth):**
1. **Password** — the installer auto-generates a strong `DRONGO_WEB_PASSWORD` (shown at the
   end of install and stored in `/etc/drongo/drongo.env`, mode `0600`). The dashboard asks for
   it (log in with *any* username). No password ⇒ it refuses the LAN and binds to localhost only.
2. **Kernel LAN-lock** — `drongo-web.service` sets `IPAddressDeny=any` + allow only private
   ranges, so the kernel drops any non-LAN connection *before it reaches the app* — even if your
   router accidentally forwards the port.
3. **Optional IP allowlist** — set `DRONGO_WEB_ALLOW="192.168.1.50"` (or a CIDR) in `drongo.env`
   to let in only specific machines.
4. **Optional firewall** — `sudo ./system/firewall.sh` adds a default-drop inbound nftables
   ruleset (SSH-safe; LAN-only SSH + dashboard). Run it if you want the box invisible from the
   internet at the packet level too.

**Stopping the agent attacking *outward* (if a web page tries to prompt-inject it):**
- `web_fetch` has an **SSRF guard** — it only fetches public http/https addresses and refuses
  localhost / LAN / link-local / cloud-metadata IPs, validating every redirect hop. So it can't
  be tricked into hitting `localhost:11434`, your router, or other LAN devices.
- The `shell` tool runs as the unprivileged `drongo` user inside a systemd sandbox
  (read-only `/opt`, writable only under `/var/lib/drongo`), behind the [safeguard](agent/safeguard.py) denylist.

**Want it tightest?** Set `web.host: 127.0.0.1` in the config and view the dashboard through an
SSH tunnel: `ssh -L 8080:localhost:8080 <pi>` then browse `http://localhost:8080/`. Nothing is
exposed on the LAN at all.

> Honest notes: the dashboard's controls sit behind the password + kernel LAN-lock, and they only
> ever flip DB flags or write files the agent already manages (add/remove providers, set the
> package policy, import a skill, flag a project) — **never arbitrary root** — so a same-LAN
> request's blast radius is small. The SSRF guard resolves DNS at fetch time — a determined
> DNS-rebinding attacker is out of scope for a home maker box. And whoever can SSH to the Pi
> controls it — protect SSH (keys, not passwords) as you would any server.

---

## Updating

The agent runs from **`/opt/drongo`** (root-owned), *not* from your clone — so
`git pull` alone won't reach it. Deploy your pulled changes with:

```bash
cd ~/drongo
git pull
sudo ./update.sh      # syncs code to /opt/drongo, re-seals the safeguard, restarts
```

`update.sh` is the fast path (no apt / no model re-pull). Re-running
`sudo ./install.sh` also deploys, but does the full setup. *(The root auto-updater
only works if `/opt/drongo`'s git remote is reachable as root — with a private
SSH-key repo that's usually not the case, so `update.sh` is the reliable path.)*

---

## Troubleshooting

**First move, always:** run the doctor — it tells you in plain English what's wrong.
```bash
sudo drongo doctor
```

| Symptom | Likely cause → fix |
|---|---|
| `doctor` says **no LLM answered** | No usable provider. Add at least one free key to `/etc/drongo/drongo.env` (local is optional/off by default), **or** add a local model: `sudo ./install.sh --local`. Then `sudo systemctl restart drongo`. |
| Dashboard won't load at `:8080` | Service not up, or wrong IP. `systemctl status drongo-web`; find the IP with `hostname -I`. |
| Dashboard asks for a password | That's intentional. It's in `/etc/drongo/drongo.env` (`DRONGO_WEB_PASSWORD`); log in with **any** username. Change it there, then `sudo systemctl restart drongo-web`. |
| Dashboard only works on the Pi itself, not other devices | No password set ⇒ localhost-only. Set `DRONGO_WEB_PASSWORD` and restart `drongo-web` (the installer normally does this for you). |
| Agent keeps restarting / `systemctl status drongo` shows **failed** | Read `journalctl -u drongo -n 50`. If it's a safeguard error, the installer's seal step didn't finish — just re-run `sudo ./install.sh`. |
| **SAFE MODE** in the logs | It restarted too many times and threw the handbrake on. Fix the underlying error (logs), then `sudo systemctl restart drongo`; two clean cycles and it exits safe mode on its own. |
| Whole board feels sluggish / OOM | Only if you added a **local model** — it's too big. Drop to a smaller one and add zram (see [docs/local-model.md](docs/local-model.md)). Cloud-only shouldn't OOM. |
| **`/sbin/init` (PID 1) constantly at 10–20% CPU** | The **apport** crash-reporter is stuck in a restart-loop (Ubuntu [LP#1895286](https://bugs.launchpad.net/bugs/1895286)) — not the agent. Fix once: `sudo rm -f /var/crash/*`, then `sudo systemctl disable --now apport.service whoopsie.service`. The installer now does this automatically. |
| No Discord alerts | Check `DISCORD_WEBHOOK_URL` is set in `/etc/drongo/drongo.env` and `alerts.discord.enabled: true`. Test the webhook with `curl -d '{"content":"test"}' -H "Content-Type: application/json" <url>`. |
| "pip: permission denied" / can't install packages | It needs its writable venv at `/var/lib/drongo/runtime/venv`. `sudo ./update.sh` (or a restart) creates it; after that `pip install …` and `python …` in its shell use that venv automatically. Native packages (numpy etc.) need ARM wheels or build tools; pure-Python (Flask, etc.) just works. |
| LED never blinks | Wrong `chip`/`line` (check `gpioinfo`), LED wired backwards (try `active_high: false`), or `python-periphery` missing. Confirm the `drongo` user is in the `gpio` group (`id drongo`). |
| Cloud provider ignored | Its key is blank/invalid in `/etc/drongo/drongo.env`, or it's rate-limited (the dashboard's *usage* table shows cooldowns). Blank keys are skipped on purpose. |
| `web_search` returns "no results" | Keyless web search is dead (DuckDuckGo blocks scraping). Add a **free** search key to `/etc/drongo/drongo.env` and restart: `BRAVE_API_KEY` (2000/mo, [brave.com/search/api](https://brave.com/search/api/)), `TAVILY_API_KEY`, or `SERPER_API_KEY`. |
| `self_update` does nothing | You installed from a ZIP (no git remote). Self-update needs a real remote — `git clone` instead. Rollback still works either way. |
| Want to start over | `sudo /opt/drongo/uninstall.sh --purge` then re-install. |
| It's doing something I don't like | `touch /var/lib/drongo/runtime/workspace/STOP` to halt it now; investigate; delete the file to resume. |

**Useful one-liners**
```bash
journalctl -u drongo -f                 # watch the agent think, live
journalctl -u drongo-observer -n 20     # what the Dead-Man's-Switch has done
systemctl list-timers 'drongo*'         # confirm observer + updater are scheduled
ls /var/lib/drongo/runtime/workspace/   # everything it has built
```

---

## Tuning for 4 GB RAM

Cloud-only, there's little to tune — the agent is capped at `MemoryMax=1200M`
(in `drongo.service`) so it can never starve the OS, and with no local model the
board sits comfortably. If you add a **local model**, see the RAM/zram/model-size
tuning in **[docs/local-model.md](docs/local-model.md)**.

---

## File map

```
agent/
  __main__.py     CLI: run | web | once | discover | doctor | verify | seal
  config.py       config loading + runtime paths
  loop.py         the autonomous ideate→act→reflect loop + safe mode
  llm.py          multi-provider router (cloud-first, local fallback, rate limits)
  tools.py        shell, files, web, image-gen, sensors, dashboards, alerts,
                  skills/notes/knowledge (RAG), scoped package requests
  safeguard.py    ★ tamper-resistant safety core (install root:root 0444)
  watchdog.py     heartbeats, systemd notify, crash-loop self-defence
  memory.py       SQLite journal / kv / provider usage / skills / pkg policy
  alerts.py       multi-channel: Discord / LED (GPIO) / ntfy / command
  server.py       web dashboard (views + control panel)
system/
  observer.py     external root "Dead Man's Switch" (liveness, rollback, health)
  updater.py      privileged root self-updater (pull, verify, re-seal, rollback)
  pkg-installer.py scoped root apt-installer (validated names, policy + hard allow-list)
  firewall.sh     OPTIONAL inbound nftables lockdown (SSH-safe, LAN-only)
  retro-toolchain.sh OPTIONAL Z80/Amstrad toolchain (sdcc/z88dk/CPCtelera/pasmo)
  image-gen.sh    OPTIONAL local image generator (OnnxStream)
  *.env.example   environment templates for /etc/drongo
systemd/          hardened units + timers (agent, web, observer, updater, pkg)
install.sh        interactive installer / hardener (preflight, RAM-aware, self-checking)
uninstall.sh      clean removal (keeps data unless --purge)
config.example.yaml
```

---

## Notes & honest limitations

- The brain is the free cloud tiers; if they're **all** rate-limited at once and
  you haven't enabled a local fallback, the agent idles and retries (it won't
  wedge). Add a [local model](docs/local-model.md) if you want a guaranteed floor.
- It **can't train a model on the Pi** (4 GB CPU) — the "learning" is retrieval
  over its knowledge base + a curated dataset you fine-tune off-box. See the Brain tab.
- The `shell` denylist is defence-in-depth, **not** a perfect jail — the real
  isolation is the unprivileged user + systemd sandbox. Keep `allow_sudo: false`.
- Free cloud tiers change their limits often; tweak `rpm_limit`/`daily_limit`
  in the config if you start seeing 429s.
- The optional last-resort host reboot in the observer is **off by default**
  (`DRONGO_ALLOW_REBOOT=0`). The SoC hardware watchdog already covers true lockups.
