# Optional local model (Ollama)

DRONGO runs on **free cloud providers by default**. A local model is an *optional*
never-fail fallback for when every cloud tier is rate-limited or you're offline. It's
**off by default** because it costs ~2 GB RAM plus the model download — which a
cloud-only box doesn't need.

## Add one

- **At install:** answer *yes* to the "install a local fallback model?" prompt, or run
  `sudo ./install.sh --local` (or `--model <name>`, which implies it). That installs
  Ollama, pulls the model, and flips the `local` provider on in the config.
- **After install / by hand:** `ollama pull <model>`, then set the `local` provider
  `enabled: true` in `/etc/drongo/config.yaml` and `sudo systemctl restart drongo`.

## Which model

The RK3399 is **CPU-only** (no usable GPU/NPU for LLMs), so keep to a **3B-class Q4**
model — anything 7B+ swaps and crawls on 4 GB.

| Model | When |
|---|---|
| `qwen2.5:3b-instruct` ⭐ | **Default.** Best all-round agentic 3B (follows instructions, clean JSON). |
| `qwen2.5-coder:3b` | If you mostly want code/scripts/games. |
| `hermes3:3b` | You want a Hermes/Nous persona; solid too. |
| `qwen2.5:1.5b-instruct` / `llama3.2:1b` | If 3B is too slow or RAM is tight. |

> *"OpenClaw"* isn't a real Ollama model — likely a mix-up (OpenHermes / OpenChat?).
> Stick with the well-supported Qwen/Hermes options above.

## Cloud + local together

The router tries providers **top-to-bottom** (`prefer: cloud_first`): free cloud first
(Groq serves Llama-3.3-70B free and fast — night-and-day better than a local 3B), then
the local model as the floor when everything else is rate-limited. Want it fully
local/private (slower, simpler output)? Set `llm.prefer: local_first`.

## Tuning for a local model on 4 GB

```bash
# zram (compressed-RAM swap) — headroom without thrashing an SD card:
sudo apt-get install -y zram-tools
echo -e 'ALGO=zstd\nPERCENT=50' | sudo tee /etc/default/zramswap
sudo systemctl restart zramswap
```

Keep `MemoryMax=1200M` (in `drongo.service`) so the agent can't starve the OS or
Ollama. If you see swapping, drop to a smaller model:
`sudo ./install.sh --model qwen2.5:1.5b-instruct` (or `0.5b`). Put the Ollama models
and `/var/lib/drongo` on eMMC/NVMe, never a microSD.

---
← back to the [main README](../README.md)
