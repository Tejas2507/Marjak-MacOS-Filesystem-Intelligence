<p align="center">
  <h1 align="center">◆ Mārjak [ मार्जक ] ◆</h1>
  <p align="center"><strong>Your Mac is hiding gigabytes from you. Mārjak finds them.</strong></p>
</p>

<p align="center">
  <a href="#installation"><img src="https://img.shields.io/badge/macOS-only-black?style=flat-square&logo=apple&logoColor=white" alt="macOS"></a>
  <a href="#installation"><img src="https://img.shields.io/badge/python-3.12+-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.12+"></a>
  <a href="#supported-providers"><img src="https://img.shields.io/badge/AI-local%20first-orange?style=flat-square" alt="Local First AI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green?style=flat-square" alt="MIT License"></a>
  <a href="https://github.com/tejas/marjak"><img src="https://img.shields.io/badge/homebrew-installable-FBB040?style=flat-square&logo=homebrew&logoColor=white" alt="Homebrew"></a>
</p>

<p align="center"><em>macOS Filesystem Intelligence — powered by AI that runs on your machine.</em></p>

---

## The Problem

Every Mac user knows the pain. That dreaded **"Your disk is almost full"** notification. You open Finder, sort by size, and find... nothing obvious. The real space hogs are buried deep inside `~/Library`, hidden in app containers, scattered across invisible dotfiles, and locked behind caches you didn't know existed.

**Mārjak solves this in one conversation.**

It doesn't just scan — it **reasons**. It maps your filesystem into a compressed virtual tree, drills into directories level by level, identifies patterns, and explains what's safe to delete. All through natural language, right in your terminal.

```
❯ my mac is running out of space, help me clean up

🔍 Navigating: /Users/you
  ▶  navigate  {'path': '~'}
  ▶  navigate  {'path': '~/Library'}
  ▶  search_system  {'name': 'Caches'}

ANALYSIS:
- ~/Library/Caches — 4.7 GB of app caches (safe to clear)
- ~/Library/Developer/Xcode/DerivedData — 3.2 GB of stale builds
- ~/.docker/data — 1.8 GB container images
- ~/Library/Group Containers/.../media — 2.1 GB messaging attachments

Total reclaimable: ~11.8 GB across 4 locations.
Want me to start cleaning? I'll show you exactly what gets deleted before touching anything.
```

> No cloud. No telemetry. No subscriptions. Just your Mac, your model, your privacy.

---

## Features

🔍 **Deep Filesystem Intelligence** — Explores `~/Library`, app containers, caches, dotfiles, and system internals that Finder hides from you.

🤖 **Dual-Agent Architecture** — A read-only **Navigator** explores and proposes. A safety-gated **Executor** acts only with your confirmation. Neither can go rogue.

🧠 **VFS Playbook** — Compresses your entire explored filesystem into a token-efficient virtual tree with integer File IDs. A 12B model can reason about 50GB+ of directories.

⚡ **Local-First** — Optimized for [Ollama](https://ollama.com) with **Gemma 4**, but works with any provider: OpenAI, Gemini, Claude, Groq, OpenRouter.

🧹 **Powered by [Mole](https://github.com/mole-org/mole)** — Uses the Mole binary (`mo`) for high-speed filesystem analysis, system cache scanning, and deep cleaning. Mārjak is the AI brain; Mole is the muscle.

🛡️ **Safety by Design** — Protected paths are hard-blocked. Files are moved to Trash (recoverable). Every destructive action requires confirmation. Shell access (Expert only) asks permission per command.

🎯 **Three Presets** — Eco for fast small models, Pro for balanced depth, Expert with direct shell access for power users.

---

## Installation

### ⚡ Homebrew (Recommended)

```bash
brew tap tejas/marjak https://github.com/tejas/marjak.git
brew install marjaka
```

This installs Mārjak + [Mole](https://github.com/mole-org/mole) together. Then:

```bash
# Install Ollama for local AI (if not already installed)
brew install ollama
ollama serve &
ollama pull gemma4

# Launch
marjaka
```

### 🛠 From Source

```bash
# Install uv (Python package manager)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Clone and install
git clone https://github.com/tejas/marjak.git
cd marjak
uv sync

# Make sure Ollama is running with a model
brew install ollama && ollama serve &
ollama pull gemma4

# Run
uv run python main.py
```

### First Launch

Mārjak works out of the box with **Ollama + Gemma 4**. To switch providers:

```
❯ /config
```

Interactive wizard → pick provider → enter API key → choose model → auto-selects the right preset.

---

## Usage

Just type what you want. Mārjak figures out the rest.

```
❯ what's eating my storage?
❯ find all cache folders over 500MB
❯ how much space is xcode using?
❯ clean up everything in Library/Caches
❯ delete all files over 1GB in Downloads
❯ show me system health
```

### Slash Commands

| Command | What it does |
|---|---|
| `/config` | Setup AI provider, model, preset |
| `/scan` | Quick waste preview (nothing deleted) |
| `/deep_clean` | Purge system caches (preview → confirm → execute) |
| `/optimize` | Refresh OS databases and caches |
| `/playbook` | View the VFS knowledge tree |
| `/wipe` | Nuclear reset — erase all Mārjak data |
| `/quit` | Save and exit |

---

## How It Works

```
┌─────────────────────────────────────────────────────────┐
│  You: "what's using space in my Library?"               │
│                                                         │
│  ┌──────────┐    navigate()     ┌──────────┐            │
│  │Navigator │───search_system()─│  Tools   │            │
│  │(explore) │◄──collect_files()─│(read-only│            │
│  └────┬─────┘    run_shell()*   └──────────┘            │
│       │ call_executor(FIDs)                             │
│  ┌────▼─────┐    move_to_trash()┌──────────┐            │
│  │ Executor │───deep_clean()────│  Tools   │            │
│  │ (action) │◄──optimize()──────│(destruct)│            │
│  └──────────┘                   └──────────┘            │
│                                                         │
│  * Expert preset only, asks permission per command      │
└─────────────────────────────────────────────────────────┘
```

**Navigator** explores your filesystem, builds a virtual map (VFS Playbook), and identifies targets. It hands off File IDs to the **Executor** for deletion. The Executor shows a preview, asks for confirmation, then moves files to Trash.

The **VFS Playbook** is the secret sauce — a compressed ASCII tree injected into the system prompt every turn. It stores directories, sizes, and integer FIDs so the LLM can reference `~/Library/Caches/com.spotify.client` as `FID:42` using 5 tokens instead of 50.

### Performance Presets

| Preset | Best For | Nav Budget | Exec Budget | Shell | Context Budget |
|---|---|---|---|---|---|
| **Eco** | Gemma 4, Llama 3.2, Phi-4 | 5 loops | 2 loops | No | 4K chars |
| **Pro** | Gemma 27B, Qwen-2.5, Mistral | 10 loops | 5 loops | No | 8K chars |
| **Expert** | 70B+ models, Claude, GPT | 20 loops | 10 loops | Yes | 15K chars |

---

## Supported Providers

All lightweight, all fast. No need for frontier models — this is filesystem work, not PhD research.

| Provider | Default Model | Context | Notes |
|---|---|---|---|
| **Ollama** (local) | `gemma4` | 131K | Zero-cost, private, recommended |
| **OpenAI** | `gpt-4.1-nano` | 1M | Cheapest OpenAI option |
| **Gemini** | `gemini-2.0-flash-lite` | 1M | Free tier available |
| **Claude** | `claude-3.5-haiku` | 200K | Fast + affordable |
| **Groq** | `llama-3.3-70b` | 131K | Free tier, blazing fast |
| **OpenRouter** | `gemini-2.0-flash-lite` | 1M | Multi-provider routing |

> All models are configurable via `/config`. These are just sensible defaults.

---

## Architecture

```
main.py                 TUI loop, slash commands, Rich UI
├── agent.py            LangGraph StateGraph (Navigator ↔ Executor)
│                       ContextManager: prune → isolate → summarize → trim
│                       Conversation summary (rule-based, tier-scaled intervals)
├── tools.py            Navigator: navigate, search_system, get_system_overview,
│                                  collect_deletable_files, run_shell (Expert)
│                       Executor:  execute_deep_clean, run_system_optimization,
│                                  move_to_trash
│                       Powered by Mole binary for filesystem scanning
├── prompts.py          Tiered prompt system (Eco / Pro / Expert × Nav / Exec)
├── knowledge_book.py   SessionBook: VFS tree, FID mapping, stale detection
├── guidebook.py        Tag-based macOS filesystem knowledge retrieval
├── macos_guidebook.yaml  36 curated entries: safety ratings, reclaimable paths
└── config_manager.py   Provider / model / preset config (~/.marjak/)
```

### Design Principles

- **1MB file floor** — Sub-MB files aren't tracked. 8500 thumbnails become 9 meaningful nodes.
- **Handoff-aware loop budgets** — Nav and Exec each get independent tool budgets. A round-trip doesn't steal turns.
- **Context rot prevention** — Old tool results are auto-summarized. VFS tree refreshes every turn.
- **FID-based deletion** — The LLM never sees raw paths for destructive ops. Protected system paths are hard-blocked.
- **Gated shell access** — Expert-only `run_shell` with command whitelisting and per-command user approval.

---

## Data & Privacy

Everything stays on your machine.

| Path | What's stored |
|---|---|
| `~/.marjak/config.json` | Provider settings, API keys |
| `~/.marjak/memory.json` | Hotspot tracking, action history |
| `~/.marjak/session_book.json` | VFS tree (validates paths on restart) |
| `runlogs/` | Debug logs (LLM inputs/outputs for troubleshooting) |

Zero telemetry. Zero cloud sync. `rm -rf ~/.marjak` and it's all gone.

---

## Credits

- **[Mole](https://github.com/mole-org/mole)** — The high-speed filesystem analysis engine that powers Mārjak's scanning and cleaning capabilities. Mārjak is the AI brain; Mole is the muscle.
- **[LangGraph](https://github.com/langchain-ai/langgraph)** — Multi-agent state machine orchestration.
- **[Rich](https://github.com/Textualize/rich)** — Beautiful terminal UI, Markdown rendering, and animated thinking indicators.
- **[Ollama](https://ollama.com)** — Local LLM inference that makes privacy-first AI possible.

---

## Development

```bash
uv run python test_all_tools.py   # Tool tests (no LLM needed)
uv run python main.py             # Full run with Ollama
uv run ruff check .               # Lint
```

---

## License

MIT

---

<p align="center">
  <strong>Your Mac has secrets. Mārjak finds them.</strong><br>
  <em>Stop guessing. Start knowing.</em>
</p>

Built for **local-first AI**. Runs on Gemma 4, Llama 3, or any model via Ollama. Also supports OpenAI, Gemini, Claude, Groq, and OpenRouter for cloud users.

```
❯ what's eating my disk space?

🔍 Navigating: /Users/you
  ▶  navigate  {'path': '~'}
  ▶  navigate  {'path': '~/Library'}
  ▶  navigate  {'path': '~/Library/Group Containers/...telegram'}

ANALYSIS:
- **Telegram media**: 3.3 GB across 9 large files
- **Xcode DerivedData**: 2.1 GB of stale build artifacts
- **Docker images**: 890 MB in ~/.docker

Total reclaimable: ~6.3 GB. Want me to clean any of these?
```

---

## How It Works

**Two agents, one brain:**

| Agent | Role | Tools |
|---|---|---|
| **Navigator** | Read-only exploration. Maps the filesystem, searches for waste, identifies targets. | `navigate`, `search_system`, `get_system_overview`, `collect_deletable_files`, `run_shell`* |
| **Executor** | Destructive actions only. Deletes files, cleans caches, optimizes system. Requires user confirmation. | `move_to_trash`, `execute_deep_clean`, `run_system_optimization` |

*\*`run_shell` is Expert preset only — asks user permission before every command.*

The Navigator explores and proposes. The Executor acts only when you approve. Handoff between them is automatic via LangGraph.

**VFS Playbook** — Instead of dumping thousands of file paths into the LLM context, Mārjak maintains a compressed virtual filesystem tree (`SessionBook`) with integer File IDs (FIDs). This lets a 12B local model reason about 50GB+ directory trees without context overflow.

**Three performance presets:**

| Preset | Target | Nav Loops | Shell Access |
|---|---|---|---|
| **Eco** | Small models (≤12B), fast answers | 5 | No |
| **Pro** | Mid-range (12B–70B), balanced depth | 10 | No |
| **Expert** | Large models (≥70B) or cloud APIs | 20 | Yes |

---

## Installation

### Prerequisites

- **macOS** (uses native tools: `find`, `mdfind`, `df`, `osascript`)
- **Python 3.12+**
- **[uv](https://docs.astral.sh/uv/)** — Python package manager
- **[Ollama](https://ollama.com)** — for local models (or an API key for cloud providers)

### Quick Start

```bash
# 1. Install uv if you don't have it
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. Install Ollama and pull a model
brew install ollama
ollama serve &          # start Ollama in the background
ollama pull gemma4      # ~5GB download, one time

# 3. Clone and install Mārjak
git clone https://github.com/tejas/marjak.git
cd marjak
uv sync

# 4. Run
uv run python main.py
```

### Homebrew (one-liner)

```bash
brew tap tejas/marjak https://github.com/tejas/marjak.git
brew install marjaka
```

Then run `marjaka` from anywhere.

> **Note**: After Homebrew install, you still need Ollama running with a model pulled. See [First Run](#first-run) below.

### First Run

On first launch, Mārjak uses **Ollama + gemma4** by default. To change provider, model, or preset:

```
❯ /config
```

This opens an interactive wizard:
1. Select provider (Ollama, OpenAI, Gemini, Claude, Groq, OpenRouter)
2. Enter API key (if not Ollama)
3. Choose model name
4. Pick performance preset (auto-recommended based on your model)

---

## Commands

| Command | Action |
|---|---|
| `/config` | Configure AI provider, model, and preset |
| `/scan` | Preview cleanable waste categories (dry-run, nothing deleted) |
| `/deep_clean` | Purge system caches and waste (shows preview, asks confirmation) |
| `/optimize` | Refresh OS caches, rebuild system databases |
| `/playbook` | Show the VFS knowledge tree |
| `/wipe` | Erase all Mārjak memory and VFS data |
| `/quit` | Save and exit |

Or just talk to it naturally:
- *"what's using space in my Library?"*
- *"find all Telegram files over 100MB"*
- *"delete the partial download files but keep the database"*
- *"how big is my Docker setup?"*

---

## Architecture

```
main.py                 TUI loop, slash commands, input sanitization
├── agent.py            LangGraph StateGraph, Navigator/Executor nodes,
│                       ContextManager (prune → isolate → summarize → trim)
├── tools.py            All tool implementations
│                       Navigator: navigate, search_system, get_system_overview,
│                                  collect_deletable_files, run_shell (Expert)
│                       Executor:  execute_deep_clean, run_system_optimization,
│                                  move_to_trash
├── prompts.py          Tiered prompt system (Eco/Pro/Expert × Nav/Exec)
├── knowledge_book.py   SessionBook VFS tree, FID mapping, stale detection
└── config_manager.py   Provider/model/preset configuration (~/.marjak/)
```

### Key Design Decisions

- **1MB file floor** — Files under 1MB aren't stored in the VFS. Users never delete individual sub-MB files; storing them bloats context (8500 thumbnails → 9 meaningful nodes).
- **Handoff boundary loop reset** — Navigator and Executor each get their own tool-call budget. A Nav→Exec→Nav cycle doesn't eat the Navigator's remaining turns.
- **Context rot prevention** — Old tool results are truncated to their summary line before the latest user message. The VFS tree is refreshed every turn.
- **`run_shell` is gated** — Expert-only. Whitelisted to read-only commands (`du`, `find`, `ls`, `stat`, `mdls`, `diskutil`, etc.). Asks user `Allow this shell command? [y/n]` every time. Destructive commands (`rm`, `sudo`, `mv`, etc.) are hard-blocked.
- **FID-based deletion** — The LLM never sees raw file paths for deletion. It uses integer FIDs that map to validated absolute paths. Protected paths (`/`, `~/Library`, `~/Documents`, etc.) are hard-blocked.

---

## Persistent State

All state is stored under `~/.marjak/`:

| File | Purpose |
|---|---|
| `config.json` | Provider, model, preset, API keys |
| `memory.json` | Hotspot tracking, action history, user preferences |
| `session_book.json` | VFS tree state (survives restarts, validates stale paths on load) |

---

## Supported Providers

| Provider | Default Model | Context Window |
|---|---|---|
| Ollama (local) | gemma4 | 131K |
| OpenAI | gpt-4o-mini | 128K |
| Gemini | gemini-1.5-flash | 1M |
| Claude | claude-3-5-sonnet | 200K |
| Groq | llama-3.3-70b | 131K |
| OpenRouter | gemini-2.0-flash | 131K |

---

## Development

```bash
# Run tool tests (no LLM needed)
uv run python test_all_tools.py

# Run with live Ollama
uv run python main.py

# Lint
uv run ruff check .
```

Debug logs are written to `runlogs/` after every LLM invocation — full message history, reasoning traces, tool calls, and timing.

---

## License

MIT

---

*"Mārjak doesn't just clean your disk; it masters your system."*
# Mārjak [मार्जक]
### macOS Filesystem Intelligence Agent

**Mārjak** is a state-of-the-art system maintenance and filesystem intelligence agent designed for the modern macOS power user. Built upon the robust **Mole architecture**, Mārjak shifts the paradigm from simple reactive cleaning scripts to an intentional, autonomous AI agent capable of deep-system reasoning and optimization.

Developed with a privacy-first ethos, Mārjak is specialized for **Local Small Language Models (SLLMs)** (such as Gemma or Llama 3 running via Ollama), bringing sophisticated machine-level awareness to your machine without ever touching the cloud.

---

## ◈ The Vision: The Janitor in the Machine

Most system cleaners rely on static rule-sets. Mārjak replaces rules with **reasoning**. By pairing a high-fidelity map of your filesystem with a dual-agent architecture, it doesn't just "delete files"—it understands storage hotspots, identifies orphaned artifacts, and optimizes system internals through a recursive discovery loop.

---

## ◈ Technical Architecture

### 1. The Navigator/Executor Pattern
Mārjak operates using a bifurcated intelligence model managed via **LangGraph**:
*   **The Navigator (The Brain)**: A read-only specialist designed for exploration. It analyzes the VFS Playbook, searches for waste, and identifies the absolute path and FID (File ID) of targets. It cannot delete; it can only propose.
*   **The Executor (The Hammer)**: A specialized agent responsible for destructive actions. It only activates when the user or the Navigator provides specific instructions. It performs validations (system integrity checks) before executing any `mv` or `rm` operations.

### 2. Context Engineering: The VFS Playbook
The primary challenge of Local SLLMs is their limited context window. Mārjak solves the "Large Filesystem vs. Small Context" problem through **Tiered Memory Mapping**:
*   **Virtual File System (VFS)**: Rather than flooding the LLM with thousands of file paths, Mārjak maintains a compressed `SessionBook`.
*   **FIDs (File IDs)**: Every file/folder analyzed is assigned a short, stable integer ID. The LLM interacts with these FIDs, allowing it to reference complex paths across the system while using minimal tokens.
*   **Stale-Node Detection**: Mārjak tracks recursion depth and last-scan timestamps to ensure the Navigator is always working with fresh filesystem data.

### 3. Local SLLM Specialization
Mārjak is hardened specifically for models like **Gemma 4** and **Llama 3**. 
*   **Strict Prompting**: System prompts are engineered with XML-structured context tags and strict anti-hallucination guardrails.
*   **Deterministic Fallbacks**: If a local model generates ambiguous tool calls, Mārjak's tool-caller validates the arguments against the current VFS state before execution.

---

## ◈ Core Capabilities & Feature Set

### 🔍 Discovery & Analysis
*   **`navigate`**: Recursively analyze directory sizes and content hierarchies.
*   **`search_system`**: Global fuzzy search across `~/Library`, Home, and Spotlight indices to find app-specific artifacts.
*   **`mole_scan`**: A high-speed waste assessment tool (based on the Mole binary) that previews cleanable categories like system caches, dev tool kernels, and browser junk.

### 🧹 Optimization & Cleaning
*   **`execute_deep_clean`**: A nuclear approach to system hygiene, purging verified waste categories while maintaining a staging preview for safety.
*   **`run_system_optimization`**: Refreshes system caches, resets stalled network services, and rebuilds system databases without deleting personal data.
*   **`move_to_trash`**: Safely transfers heavy files or orphaned directories to the macOS Trash using FIDs.

### 🧠 Persistent Memory
Stored at `~/.marjak/memory.json`, Mārjak learns your system over time:
*   **Hotspot Tracking**: Remembers where the largest files live across sessions.
*   **Action History**: Correlates prior cleaning successes to improve future recommendations.
*   **Preference Learning**: Identifies user-defined "safe" and "dangerous" paths.

---

## ◈ User Experience (TUI Design)

Mārjak features a premium terminal user interface (TUI) powered by **Rich**:
*   **Threaded Thinking Indicator**: A non-intrusive, animated braille spinner that accumulates reasoning tokens in a background thread. It refreshes at a smooth 150ms rate, providing real-time feedback without the jitter of token-by-token display.
*   **Transient Tool Results**: Tool outputs flash onto the screen for context and then vanish, keeping the terminal clean and focused on the conversational flow.
*   **Markdown Sanitization**: All AI verbosity (bolding, tables, headers) is stripped into clean, terminal-native plain text, ensuring a minimalist aesthetic.

---

## ◈ Setup & Requirements

### Dependencies
1.  **macOS**: Native tools (`osascript`, `df`, `find`, `mdfind`) are required.
2.  **Ollama**: Must be running locally with `gemma` or `llama3` available.
3.  **Python 3.12+**: Managed via `uv`.
4.  **Mole Binary**: The project relies on the `mo` binary for high-speed filesystem analysis.

### Installation
```bash
# Clone the repository
git clone https://github.com/your-repo/marjak
cd marjak

# Install dependencies
uv sync

# Run the agent
uv run python main.py
```

---

## ◈ Commands Reference

Inside the TUI, you can use the following slash-commands:
*   `/scan`: Run a waste preview scan.
*   `/deep_clean`: Forcibly purge system waste.
*   `/optimize`: Refresh system internal services.
*   `/playbook`: Visualize the current VFS Knowledge Tree.
*   `/wipe`: Erase all persistent memory and Mārjak history.
*   `/quit`: Safely shutdown the agent.

---

*“Mārjak doesn’t just clean your disk; it masters your system.”*
