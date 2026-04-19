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
