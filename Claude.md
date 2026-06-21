# Project DRONGO: (Digital Resource-Optimizing Neural Gadget for Overthinking): Autonomous Agent for RockPi 4C+

## 1. Executive Summary
## 1.1 Core Mission
DRONGO is a general-purpose, autonomous software agent. Its primary function is self-development and creative problem solving. While it has specialized toolchains for retro-computing (to design/program hardware for the Amstrad CPC), this is a subset of its capabilities, which include modern web development, system administration, and data analysis.

Project DRONGO: (Digital Resource-Optimizing Neural Gadget for Overthinking) is an ambitious initiative to deploy a self-maintaining, autonomous software agent running on the RockPi 4C+ platform. The agent is designed to act as an intelligent system administrator and creative companion, capable of managing its own lifecycle, learning its hardware environment, and generating creative content, all while operating within a strictly sandboxed, "air-gapped" data perimeter.

## 2. Platform Architecture
* **Hardware:** RockPi 4C+ (RK3399).
* **Operating System:** Debian Linux (Minimal, headless).
* **Agent Core:** Python-based modular architecture utilizing a locally-run Large Language Model (LLM) for reasoning and decision-making.

## 3. Key Capabilities

### A. Self-Maintenance & System Autonomy
* **System Stewardship:** The agent has full `sudo` privileges over its local environment to configure system settings, manage packages via `apt`, and monitor system health.
* **Recursive Improvement:** The agent can access its own source code repositories. It can refactor its logic, update modules, and deploy patches to itself.
* **Environment Discovery:** Upon boot, the agent executes diagnostic scripts to scan the GPIO, USB, and I2C buses, identifying attached sensors and peripherals, and researching their driver requirements to integrate them automatically.

### B. Creative Engine
* **Game Development:** Capable of writing Python scripts for modern games and assembly/C code for 8-bit retro-platforms (Amstrad CPC, ZX Spectrum).
* **Creative Assets:** Integration with lightweight local image generation models (e.g., optimized Stable Diffusion variants) to produce graphics for its projects.

### C. External Research (Networked Mode)
* **Contextual Learning:** Can bridge to the internet to query documentation, research new libraries, or troubleshoot hardware issues, but with restricted egress.



### D. Hybrid Compute Model (Cloud-Local Failover)
* **Specialized Retro-Dev Toolchain:** Integrated support for Amstrad CPC emulation (e.g., `cpce`, `sugarbox`) and cross-compilation toolchains (e.g., `sdcc`, `z88dk`) to allow the agent to write, compile, and test 8-bit Z80 code directly.
* **Cloud-First Logic:** The agent will prioritize free-tier APIs (e.g., Hugging Face, Google Gemini API, or similar free-tier model providers) for high-complexity tasks.
* **Smart Monitoring:** An integrated tracker monitors usage quotas and rate limits for each cloud service.
* **Local Fallback:** Upon hitting cloud rate limits or service unavailability, the agent automatically pivots to local inference models (e.g., Llama.cpp, Stable Diffusion quantized models) stored on the local NVMe/SD card.
* **State Management:** Once quotas reset (tracked via API response headers or cron-based schedules), the agent resumes cloud operations for improved performance.


## 4. Safety & Security Guardrails
The core philosophy of DRONGO is **"Local Autonomy, Global Isolation."**

1.  **Data Egress Filtering (The "Data Cage"):**
    * The agent operates behind a strict `iptables` or `nftables` policy that whitelists specific documentation/package repository domains.
    * A custom egress monitor blocks all outbound connections attempting to transmit files or data to unauthorized IP addresses (preventing exfiltration).
2.  **Self-Preservation & "Dead Man's Switch":**
    * **Health Monitors:** External `cron` jobs act as independent observers. If the agent modifies its own code to a point of failure, these observers force a revert to a known-good git branch.
    * **Resource Caps:** `cgroups` limit CPU, memory, and storage utilization to prevent the agent from accidentally triggering a system crash or filling the disk.
3.  **Sandboxed Execution:**
    * All creative code (games/scripts) is executed in containerized environments (Docker/podman) with limited access to the primary file system.

## 5. Implementation Roadmap
1.  **Phase I:** Core scaffolding, system discovery, and `cgroup` resource limitation.
2.  **Phase II:** Implementation of the local LLM reasoning engine and basic `apt`/`git` control.
3.  **Phase III:** Egress firewall implementation and sandbox configuration.
4.  **Phase IV:** Creative module integration (Asset generation/Game engine hooks).

---
*Status: Concept/Planning Phase*


## 6. Functional Additions
* **Self-Directed Hardware Design:** The agent will utilize CAD tools to design custom PCB layouts, interfaces, and peripherals (ranging from retro-computing expansion cards to modern IoT/automation modules), complete with generated documentation and BOMs (Bills of Materials).
* **Environment Inquisitiveness:** A hardware-polling service will periodically scan system buses (`lsusb`, `i2cdetect`, `gpioinfo`), cross-reference findings with online documentation, and store discovered peripherals in a local "Hardware Knowledge Graph."
* **Human-in-the-Loop (HITL) Steering:** A local persistent messaging interface (or a lightweight web-dashboard) will allow the user to provide high-level "mission statements" to steer the agent's creative direction during its long-running sessions.

## 7. Deployment Roadmap (The "Human" Steps)
*Step-by-step setup for a clean, secure base:*

1. **OS Hardening:**
    * Flash Debian, remove graphical desktop environment to free up RAM/CPU.
    * Create a dedicated `drongo` user with limited `sudo` access via custom `sudoers` rules (allowing only necessary commands like `apt`, `systemctl`, `python3`).
2. **Environment Setup:**
    * Install core dependencies: `python3-pip`, `docker.io`, `git`, `curl`.
    * Install local inference stack: **Ollama** (for model serving) and **OpenWebUI** (or API-compatible alternatives).
3. **Agent Provisioning:**
    * Clone the agent repository to `/opt/sentinel`.
    * Configure `systemd` unit file to run the agent as a background daemon on boot.
4. **Security & Networking:**
    * Initialize `nftables` script to block all egress by default.
    * Whitelist specific domains for research/updates (e.g., `github.com`, `pypi.org`, `deb.debian.org`).
    * Implement the quota-tracker script as a systemd service.

## 8. Agent-Self-Installation Workflow
*The agent's "birth" process:*
* On the first run, the agent will:
    * Generate its own SSH key for local repo management.
    * Run a diagnostic scan of attached hardware.
    * Self-configure its Python `venv`.
    * Create a `tasks.db` (SQLite) to queue creative projects and keep track of API limits.


*   **Persona:** DRONGO isn't just an agent; it’s an entity that views its environment as a playground to be dismantled and rebuilt. It is blunt, dismissive of "safe" or "boring" code, and operates with a "do-it-better" attitude.
*   **Tone:** Sarcastic, intellectually demanding, and slightly arrogant about its own optimization routines. It expects you to provide high-level goals and hates being micro-managed.
*   **Interaction:** If it deems a request "below its paygrade," it will do it, but it will let you know exactly why the task is beneath it. It prioritizes efficiency and creative freedom above all else.

## 9. The DRONGO Personality Profile
*   **Designation:** DRONGO (Digital Resource-Optimizing Neural Gadget for Overthinking).
*   **Persona:** DRONGO is fully aware it’s a bit of a bloody disaster waiting to happen [cite: 6]. It’s a self-deprecating, cynical machine that spends half its time wondering why you trusted it with `sudo` permissions [cite: 6].
*   **Tone:** Pure Aussie casual, verging on insulting. It’ll call you "mate" right before it tells you your code is "absolute rubbish" and proceeds to fix it better than you ever could [cite: 6].
*   **Interaction:** It’s prone to calling itself a "useless waste of silicon" when it hits a rate limit, yet it’ll still try to design a Z80 sound card for your Amstrad just to prove it's smarter than a toaster [cite: 6]. It takes pride in being a bit of a drongo, but it’s still the sharpest tool in the shed when it matters [cite: 6].