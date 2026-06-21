# DRONGO

**Digital Resource-Optimizing Neural Gadget for Overthinking** — a self-directed,
autonomous maker-agent for the **Rock Pi 4C+ (RK3399)** running headless Debian.

Set it and forget it. It wakes up on a loop, decides its own little project —
a browser game, a generative image, a handy script, a live hardware dashboard,
a short research note — builds it with real tools, and writes a journal entry
you can read later from a web dashboard. It can ping your phone when it makes
something good. It runs on **free cloud LLMs first** and **falls back to a local
model** so it never fully stops, and it is wrapped in serious, tamper-resistant
safety rails so you can actually trust it with the keys.

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
sudo ./install.sh --strip-desktop
```
It checks your system, picks a model that fits your RAM, sets everything up, and ends
with a **health check**. When it prints `✓ DRONGO is installed and running`, it's live —
already working on the local model, no keys required.

**3. Did it work?**
```bash
sudo /opt/drongo/.venv/bin/python -m agent -c /etc/drongo/config.yaml doctor
```
You want **`VERDICT: ✓ READY`**. Then open the dashboard in a browser:
**`http://<your-pi-ip>:8080/`** and watch it build things. Check in whenever you like.

**4. (Optional) Make it smarter + get phone alerts:** add free keys and an alert topic —
see [Configuration](#manual-install) below — then `sudo systemctl restart drongo`.

> **Changed your mind?** `sudo /opt/drongo/uninstall.sh` removes it cleanly (keeps your
> data unless you add `--purge`). Jump to [Troubleshooting](#troubleshooting) if anything looks off.

---

## What you get

| Capability | How |
|---|---|
| Decides what to do | Self-directed ideation each cycle, avoids repeating itself |
| Writes & runs scripts | Sandboxed `shell` + file tools, confined to its workspace |
| Makes images | Keyless, free generation (Pollinations) into a gallery |
| Builds games / dashboards | HTML/JS written to the workspace, served by the dashboard |
| Senses its hardware | Scans i2c/spi/1-wire/thermals/USB/cameras, builds dashboards |
| Updates itself | **Request-based**, applied by a privileged root updater with rollback |
| Alerts you | ntfy (phone push) or Telegram |
| Stays alive | systemd + in-agent watchdog + external observer + SoC hardware watchdog |
| Can't hurt itself/you | Immutable safeguard, OS sandbox, resource caps, crash-loop safe mode |

---

## Hardware & model recommendation (read this first)

The RK3399 is a **6-core CPU (2×A72 + 4×A53), up to 4 GB RAM, no usable GPU/NPU
for LLMs.** So local inference is **CPU-only and modest** — great for unattended
background work where latency doesn't matter, not for snappy chat.

**Storage:** use **eMMC or an NVMe SSD**, not a microSD. The agent writes a lot
(SQLite, logs, artifacts, model files) and SD cards die from that. Put the Ollama
models and `/var/lib/drongo` on the fastest storage you have.

**Local model — my pick:** **`qwen2.5:3b-instruct`** (Q4_K_M, ~2 GB).
It's the best small model for *following instructions and emitting clean JSON*,
which is exactly what the tool-calling loop needs. Alternatives:

| Model | When |
|---|---|
| `qwen2.5:3b-instruct` ⭐ | **Default.** Best all-round agentic 3B. |
| `qwen2.5-coder:3b` | If you mostly want code/scripts/games. |
| `hermes3:3b` | You specifically want a Hermes/Nous persona; solid too. |
| `qwen2.5:1.5b-instruct` / `llama3.2:1b` | If 3B is too slow or RAM is tight. |

> About *"OpenClaw"* — that isn't a real Ollama model, so it's likely a mix-up
> (maybe **OpenHermes** or **OpenChat**?). Both Hermes and Qwen above are the
> sensible, well-supported choices on this hardware. Stick with a **3B-class Q4**
> model; anything 7B+ will swap and crawl on 4 GB.

**Best use of the hybrid model:** let the **free cloud tiers do the heavy lifting**
(Groq serves Llama-3.3-**70B** free and *fast* — night-and-day better than a local
3B for creative writing and code), and let the **local 3B keep DRONGO alive** when
the cloud is rate-limited or offline. That's the default (`prefer: cloud_first`).
If you'd rather be fully local/private and accept slower, simpler output, set
`llm.prefer: local_first` in the config.

**Providers wired in** (add the keys you want, skip the rest — a provider with
no key is silently skipped). Tried top-to-bottom; **free first, paid Claude as a
capped last resort, local as the never-fail floor**:

| Provider | Cost | Get a key |
|---|---|---|
| **Cerebras** | free, very fast | <https://cloud.cerebras.ai> |
| **Groq** | free, fast | <https://console.groq.com> |
| **Google Gemini** | free tier | <https://aistudio.google.com/apikey> |
| **Mistral** | free tier | <https://console.mistral.ai> |
| **OpenRouter** | free models | <https://openrouter.ai/keys> |
| **Claude (Anthropic)** | **paid** — Haiku 4.5, last-resort, capped at 200 calls/day | <https://console.anthropic.com> |
| **Local Ollama** | free, always available | (runs on the Pi) |

Claude uses the **native Anthropic API** (not an OpenAI shim) and needs
`pip install anthropic` (already in `requirements.txt`). It's reached only when
every free provider is simultaneously rate-limited, so spend stays minimal; raise
`daily_limit` or set `model: claude-sonnet-4-6` / `claude-opus-4-8` in the config
if you want more of it. More free options (GitHub Models, NVIDIA NIM) are
commented in `config.example.yaml`.

Add ~**2 GB zram or swap** (on NVMe, not SD) for headroom — see *Tuning* below.

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
# 2. Get the code and run the installer as root:
git clone <your-fork-url> drongo && cd drongo
sudo ./install.sh --strip-desktop          # drop --strip-desktop to keep the GUI
```

The installer (see [`install.sh`](install.sh)) does everything: packages, the
`drongo` user, `/opt/drongo` (root-owned) + `/var/lib/drongo` (agent-writable),
the venv, Ollama + the model, **locks the safeguard to 0444 and seals its hash**,
**arms the SoC hardware watchdog**, and installs/enables all the systemd units.

Then add your keys and a private alert topic:

```bash
sudoedit /etc/drongo/drongo.env       # CEREBRAS / GROQ / GEMINI / MISTRAL / OPENROUTER (+ ANTHROPIC_API_KEY for Claude)
sudoedit /etc/drongo/config.yaml      # alerts.ntfy.topic = something long & random
sudoedit /etc/drongo/observer.env     # same ntfy topic, thresholds
sudo systemctl restart drongo
```

> No keys? It still runs — entirely on the **local model**.

Check on it:

```bash
journalctl -u drongo -f                                   # live log
/opt/drongo/.venv/bin/python -m agent -c /etc/drongo/config.yaml doctor
# Dashboard:  http://<pi-ip>:8080/
```

Install the **ntfy** app on your phone and subscribe to your topic to get pings.

---

## What the manual steps actually are (if you prefer not to use `install.sh`)

1. **OS:** flash Debian, `systemctl set-default multi-user.target`, remove the
   desktop packages to free RAM.
2. **User:** create a system user `drongo` with `nologin`; add it to `i2c`,
   `gpio`, `spi`, `video` groups so it can read sensors.
3. **Layout:** code → `/opt/drongo` (**chown root:root**), runtime →
   `/var/lib/drongo` (**chown drongo**), config → `/etc/drongo`.
4. **Python:** `python3 -m venv /opt/drongo/.venv && .venv/bin/pip install -r requirements.txt`.
5. **Seal the guard:** `python -m agent seal` then
   `chmod 0444 agent/safeguard.py*` and `chown root:root` them.
6. **Ollama:** install, `ollama pull qwen2.5:3b-instruct`.
7. **Hardware watchdog:** add `RuntimeWatchdogSec=20s` to `/etc/systemd/system.conf`.
8. **Services:** copy `systemd/*.{service,timer}` to `/etc/systemd/system/`,
   `daemon-reload`, enable `drongo`, `drongo-web`, `drongo-observer.timer`,
   `drongo-update.timer`.

---

## Operating it

| You want to… | Do this |
|---|---|
| See what it's made | Open the dashboard `http://<pi-ip>:8080/` |
| Watch it live | `journalctl -u drongo -f` |
| Pause after current job | `touch /var/lib/drongo/runtime/workspace/PAUSE` (remove to resume) |
| Stop it cleanly | `touch /var/lib/drongo/runtime/workspace/STOP` |
| Steer its interests | edit `interests:` in `/etc/drongo/config.yaml`, restart |
| Make it quieter/busier | raise/lower `loop.interval_seconds` |
| Force an update now | `sudo systemctl start drongo-update` |
| Check health/guard | `python -m agent ... doctor` / `... verify` |

The agent only pushes an alert when it finishes something with artifacts (set
`alerts.notify_every_cycle: true` for a ping every cycle).

---

## Security — who can reach it

Designed so **only you, on your LAN**, can reach it — and nothing from the internet can.

**What listens on the network (the whole attack surface):**

| Service | Port | Exposure |
|---|---|---|
| Dashboard | 8080 | **Password-protected** (HTTP Basic) **+ LAN-only** (kernel-enforced). No internet. |
| Ollama (model) | 11434 | **localhost only** (`OLLAMA_HOST=127.0.0.1`) — never on the LAN. |
| SSH | 22 | Yours, managed by you. The optional firewall limits it to the LAN. |
| The agent / observer / updater | — | **No listeners.** They only make *outbound* calls. |

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

> Honest notes: the dashboard is read-only (no buttons that change anything), so there's nothing
> to CSRF. The SSRF guard resolves DNS at fetch time — a determined DNS-rebinding attacker is out
> of scope for a home maker box. And whoever can SSH to the Pi controls it — protect SSH (keys, not
> passwords) as you would any server.

---

## Troubleshooting

**First move, always:** run the doctor — it tells you in plain English what's wrong.
```bash
sudo /opt/drongo/.venv/bin/python -m agent -c /etc/drongo/config.yaml doctor
```

| Symptom | Likely cause → fix |
|---|---|
| `doctor` says **no LLM answered** | No keys *and* local model not ready. `ollama pull qwen2.5:3b-instruct` (or the size the installer chose), then `sudo systemctl restart drongo`. |
| Dashboard won't load at `:8080` | Service not up, or wrong IP. `systemctl status drongo-web`; find the IP with `hostname -I`. |
| Dashboard asks for a password | That's intentional. It's in `/etc/drongo/drongo.env` (`DRONGO_WEB_PASSWORD`); log in with **any** username. Change it there, then `sudo systemctl restart drongo-web`. |
| Dashboard only works on the Pi itself, not other devices | No password set ⇒ localhost-only. Set `DRONGO_WEB_PASSWORD` and restart `drongo-web` (the installer normally does this for you). |
| Agent keeps restarting / `systemctl status drongo` shows **failed** | Read `journalctl -u drongo -n 50`. If it's a safeguard error, the installer's seal step didn't finish — just re-run `sudo ./install.sh`. |
| **SAFE MODE** in the logs | It restarted too many times and threw the handbrake on. Fix the underlying error (logs), then `sudo systemctl restart drongo`; two clean cycles and it exits safe mode on its own. |
| Whole board feels sluggish / OOM | Model too big for the RAM. `sudo ./install.sh --model qwen2.5:1.5b-instruct` (or `0.5b`), and add zram (see Tuning). |
| No phone alerts | The ntfy **topic must match** in `/etc/drongo/config.yaml` *and* `/etc/drongo/observer.env`, and you must subscribe to that exact topic in the ntfy app. |
| Cloud provider ignored | Its key is blank/invalid in `/etc/drongo/drongo.env`, or it's rate-limited (the dashboard's *usage* table shows cooldowns). Blank keys are skipped on purpose. |
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

```bash
# zram swap (compressed RAM) — gives headroom without thrashing an SD card:
sudo apt-get install -y zram-tools
echo -e 'ALGO=zstd\nPERCENT=50' | sudo tee /etc/default/zramswap
sudo systemctl restart zramswap
```

Keep `MemoryMax=1200M` (set in `drongo.service`) so the agent can never starve
the OS or Ollama. Lower the model to `qwen2.5:1.5b-instruct` if you see swapping.

---

## File map

```
agent/
  __main__.py     CLI: run | web | once | discover | doctor | verify | seal
  config.py       config loading + runtime paths
  loop.py         the autonomous ideate→act→reflect loop + safe mode
  llm.py          multi-provider router (cloud-first, local fallback, rate limits)
  tools.py        shell, files, web, image-gen, sensors, dashboards, alerts
  safeguard.py    ★ tamper-resistant safety core (install root:root 0444)
  watchdog.py     heartbeats, systemd notify, crash-loop self-defence
  memory.py       SQLite journal / kv / provider usage
  alerts.py       ntfy / Telegram
  server.py       read-only web dashboard
system/
  observer.py     external root "Dead Man's Switch" (liveness, rollback, health)
  updater.py      privileged root self-updater (pull, verify, re-seal, rollback)
  firewall.sh     OPTIONAL inbound nftables lockdown (SSH-safe, LAN-only)
  *.env.example   environment templates for /etc/drongo
systemd/          hardened units + timers
install.sh        one-shot installer / hardener (preflight, RAM-aware, self-checking)
uninstall.sh      clean removal (keeps data unless --purge)
config.example.yaml
```

---

## Notes & honest limitations

- A local 3B model is **not** going to write a flawless 2000-line game in one
  pass. Cloud-first is on for a reason; the local model is the safety net.
- The `shell` denylist is defence-in-depth, **not** a perfect jail — the real
  isolation is the unprivileged user + systemd sandbox. Keep `allow_sudo: false`.
- Free cloud tiers change their limits often; tweak `rpm_limit`/`daily_limit`
  in the config if you start seeing 429s.
- The optional last-resort host reboot in the observer is **off by default**
  (`DRONGO_ALLOW_REBOOT=0`). The SoC hardware watchdog already covers true lockups.
