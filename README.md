<p align="center">
  <h1 align="center">◆ Mārjak [ मार्जक ] ◆</h1>
  <p align="center"><strong>Your Mac is hiding gigabytes from you. Mārjak finds them.</strong></p>
</p>

<p align="center">
  <a href="#installation"><img src="https://img.shields.io/badge/macOS-only-black?style=flat-square&logo=apple&logoColor=white" alt="macOS"></a>
  <a href="#installation"><img src="https://img.shields.io/badge/python-3.12+-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.12+"></a>
  <a href="#supported-providers"><img src="https://img.shields.io/badge/AI-local%20first-orange?style=flat-square" alt="Local First AI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green?style=flat-square" alt="MIT License"></a>
  <a href="#installation"><img src="https://img.shields.io/badge/homebrew-installable-FBB040?style=flat-square&logo=homebrew&logoColor=white" alt="Homebrew"></a>
</p>

<p align="center"><em>macOS Filesystem Intelligence — powered by AI that runs on your machine.</em></p>

---

## The Problem

Every Mac user knows the pain. That dreaded **"Your disk is almost full"** notification. You open Finder, sort by size, and find nothing obvious. The real space hogs are buried inside `~/Library`, hidden in app containers, scattered across dotfiles, and locked behind caches you didn't know existed.

**Mārjak solves this in one conversation.**

It doesn't just scan — it **reasons**. It maps your filesystem into a compressed virtual tree, drills into directories level by level, identifies patterns, and explains what's safe to delete. All through natural language, right in your terminal.

```
❯ my mac is running out of space, help me clean up

🔍 Navigating: /Users/you
  ▶ navigate {'path': '~'}
  ▶ navigate {'path': '~/Library'}
  ▶ search_system {'name': 'Caches'}

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

- **Deep Filesystem Intelligence** — Explores `~/Library`, app containers, caches, dotfiles, and system internals that Finder hides from you.
- **Single-Agent Architecture** — One unified agent with exploration and action tools. Safety gates live in the tools: `move_to_trash` shows a preview and asks for confirmation.
- **VFS Playbook** — Compresses your explored filesystem into a token-efficient virtual tree with integer File IDs. A 12B model can reason about 50GB+ of directories.
- **Local-First** — Optimized for [Ollama](https://ollama.com) with **Gemma 4**, but works with OpenAI, Gemini, Claude, Groq, and OpenRouter.
- **Powered by [Mole](https://github.com/mole-org/mole)** — Uses the Mole binary for high-speed filesystem analysis and deep cleaning. Mārjak is the AI brain; Mole is the muscle.
- **Safety by Design** — Protected paths are hard-blocked. Files go to Trash (recoverable). Every destructive action requires confirmation.
- **Three Presets** — Eco for fast small models, Pro for balanced depth, Expert with shell access for power users.

---

## Installation

### Homebrew (Recommended)

```bash
brew tap tejas/marjak https://github.com/tejas/marjak.git
brew install marjaka
```

This installs Mārjak + [Mole](https://github.com/mole-org/mole) together. Then:

```bash
brew install ollama       # local AI backend
ollama serve &
ollama pull gemma4        # ~5GB download, one time
marjaka                   # launch
```

### From Source

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # install uv
git clone https://github.com/tejas/marjak.git
cd marjak
uv sync

brew install ollama && ollama serve &
ollama pull gemma4
uv run python main.py
```

### First Launch

Works out of the box with **Ollama + Gemma 4**. To switch providers:

```
❯ /config
```

Interactive wizard — pick provider, enter API key, choose model, auto-selects the right preset.

---

## Usage

Just type what you want:

```
❯ what's eating my storage?
❯ find all cache folders over 500MB
❯ how much space is xcode using?
❯ clean up everything in Library/Caches
❯ show me hidden dotfiles in my home directory
❯ find Telegram cache files
```

### Commands

| Command | Action |
|---|---|
| `/config` | Configure AI provider, model, and preset |
| `/scan` | Quick waste preview (nothing deleted) |
| `/deep_clean` | Purge system caches (preview → confirm → execute) |
| `/optimize` | Refresh OS databases and caches |
| `/playbook` | View the VFS knowledge tree |
| `/wipe` | Erase all Mārjak data |
| `/quit` | Save and exit |

---

## How It Works

```
┌─────────────────────────────────────────────────────────┐
│  You: "what's using space in my Library?"               │
│                                                         │
│  ┌──────────┐    navigate()     ┌──────────┐            │
│  │          │───search_system()─│  Tools   │            │
│  │  Mārjak  │───collect_files()─│ (read +  │            │
│  │  Agent   │───move_to_trash()─│  write)  │            │
│  │          │───deep_clean()────│          │            │
│  │          │◄──results─────────│          │            │
│  └──────────┘    run_shell()*   └──────────┘            │
│                                                         │
│  * Expert preset only                                   │
│  † Destructive tools show preview + ask confirmation    │
└─────────────────────────────────────────────────────────┘
```

**Single agent, all tools.** Mārjak explores your filesystem, builds a virtual map (the VFS Playbook), and identifies targets. Cleanup actions show a preview and require user confirmation.

**The VFS Playbook** is the secret sauce — a compressed ASCII tree injected into the system prompt every turn. It stores directories, sizes, and integer FIDs so the LLM can reference `~/Library/Caches/com.spotify.client` as `FID:42` using 5 tokens instead of 50.

### Performance Presets

| Preset | Target Models | Nav Loops | Exec Loops | Shell | Context |
|---|---|---|---|---|---|
| **Eco** | Gemma 4, Phi-4, small models | 5 | 2 | No | 4K chars |
| **Pro** | Gemma 27B, Qwen-2.5, Mistral | 10 | 5 | No | 8K chars |
| **Expert** | 70B+ models, Claude, GPT | 20 | 10 | Yes | 15K chars |

---

## Supported Providers

| Provider | Default Model | Notes |
|---|---|---|
| **Ollama** (local) | `gemma4` | Zero-cost, private, recommended |
| **OpenAI** | `gpt-4.1-nano` | Cheapest OpenAI option |
| **Gemini** | `gemini-2.0-flash-lite` | Free tier available |
| **Claude** | `claude-3.5-haiku` | Fast + affordable |
| **Groq** | `llama-3.3-70b` | Free tier, blazing fast |
| **OpenRouter** | `gemini-2.0-flash-lite` | Multi-provider routing |

All models configurable via `/config`.

### Model Requirements

Mārjak is a **tool-calling agent** — the LLM must support function calling. This is a hard requirement.

| Capability | Required? | Why |
|---|---|---|
| **Tool/Function calling** | **Mandatory** | Mārjak works entirely through tool calls |
| **Reasoning/Thinking** | Optional | Improves multi-step exploration. Auto-detected for Ollama |

**Recommended local models:**
- **Gemma 4** (12B) — Default. Excellent tool calling, fast on Apple Silicon.
- **Qwen 3** (8B/32B) — Strong reasoning, good tool compliance.
- **Llama 3.3** (70B) — Best open-source quality with sufficient VRAM.
- **Phi-4** (14B) — Compact, good for Eco preset.

For Ollama, only models with tool support work. Browse at [ollama.com/search?c=tools](https://ollama.com/search?c=tools).

---

## Architecture

```
main.py                 TUI loop, slash commands, Rich UI
├── agent.py            LangGraph StateGraph, context management, force-stop logic
├── tools.py            navigate, search_system, get_system_overview,
│                       collect_deletable_files, move_to_trash, execute_deep_clean,
│                       run_system_optimization, run_shell (Expert only)
├── prompts.py          Tiered prompt system (Eco / Pro / Expert)
├── knowledge_book.py   SessionBook: VFS tree, FID mapping, stale detection
├── guidebook.py        Tag-based macOS filesystem knowledge retrieval
├── macos_guidebook.yaml  36 curated entries: safety ratings, reclaimable paths
└── config_manager.py   Provider / model / preset config (~/.marjak/)
```

### Design Principles

- **1MB file floor** — Sub-MB files aren't tracked. Thousands of thumbnails collapse into meaningful nodes.
- **Handoff-free** — Single agent, all tools. No multi-agent coordination overhead.
- **Context rot prevention** — Old tool results auto-summarized. VFS tree refreshes every turn.
- **FID-based deletion** — LLM never sees raw paths for destructive ops. Protected paths hard-blocked.
- **Gated shell** — Expert-only `run_shell` with command whitelisting and per-command user approval.

---

## Data & Privacy

Everything stays on your machine.

| Path | Contents |
|---|---|
| `~/.marjak/config.json` | Provider settings, API keys |
| `~/.marjak/memory.json` | Hotspot tracking, action history |
| `~/.marjak/session_book.json` | VFS tree (validates paths on restart) |

Zero telemetry. Zero cloud sync. `rm -rf ~/.marjak` to reset completely.

---

## Development

```bash
uv run python main.py              # Full run with Ollama
uv run ruff check .                # Lint
uv run python dump/benchmark.py    # Run benchmarks (20 test cases)
```

Benchmark and test utilities live in `dump/` — not shipped to users but available for development.

---

## Credits

- **[Mole](https://github.com/mole-org/mole)** — High-speed filesystem analysis engine
- **[LangGraph](https://github.com/langchain-ai/langgraph)** — State machine orchestration
- **[Rich](https://github.com/Textualize/rich)** — Terminal UI and Markdown rendering
- **[Ollama](https://ollama.com)** — Local LLM inference

---

## License

MIT

---

<p align="center">
  <strong>Your Mac has secrets. Mārjak finds them.</strong><br>
  <em>Stop guessing. Start knowing.</em>
</p>
