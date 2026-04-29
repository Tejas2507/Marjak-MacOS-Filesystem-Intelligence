# agent.py — Mārjak v2: Single-Agent Architecture
#
# One unified agent with both read and write tools.
# Safety gates are in the tools themselves (Prompt.ask confirmation).
#
# Both use the same LLM (auto-configured via /config).
# They run sequentially — only 1 model slot used at a time.
 
from typing import TypedDict, Annotated, Sequence
from langchain_core.messages import (
    BaseMessage, SystemMessage, HumanMessage, AIMessage, ToolMessage, trim_messages
)
from langchain_core.runnables import RunnableConfig
from langgraph.graph.message import add_messages
from langgraph.graph import StateGraph, END
from langchain_ollama import ChatOllama
from langgraph.prebuilt import ToolNode
from langgraph.checkpoint.memory import MemorySaver
from rich.console import Console
from rich.live import Live
from rich.text import Text
from datetime import datetime
import itertools
import threading
import re
from marjak.config import config_manager
 
from marjak.tools import (
    navigate,
    search_system,
    get_system_overview,
    collect_deletable_files,
    run_shell,
    execute_deep_clean,
    run_system_optimization,
    move_to_trash,
    memory as persistent_memory,
    session_book,
)
from marjak.prompts import get_prompt
from marjak.guidebook import retrieve_guidebook
from marjak.fs_memory import fs_memory
import time
import os
import sys
 
# Top-level console (highlight=False to prevent random blue numbers)
_console = Console(highlight=False)
 
# Braille spinner frames for thinking indicator
_SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
 
# Track consecutive tool turns for display spacing
_last_turn_was_tool: bool = False
 
# Per-session runlog directory — set once per session via init_session_logging()
_session_log_dir: str | None = None
 
 
def init_session_logging(model: str = "", preset: str = ""):
    """Create a per-session runlog subfolder: runlogs/<timestamp>-<model>-<preset>/
 
    Call once at session start (from cli.py or benchmark.py).
    Subsequent write_llm_log calls write into this folder.
    """
    global _session_log_dir
    model_tag = (model or config_manager.current_model).replace("/", "_").replace(":", "_")
    preset_tag = preset or config_manager.config.get("preset", "Pro")
    folder_name = f"{time.strftime('%Y%m%d-%H%M%S')}-{model_tag}-{preset_tag}"
    _session_log_dir = os.path.join("runlogs", folder_name)
    os.makedirs(_session_log_dir, exist_ok=True)
    return _session_log_dir
 
 
# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
 
def write_llm_log(agent_name: str, messages: list, response, duration: float):
    """Writes everything the LLM saw and did to the session's runlog folder."""
    log_dir = _session_log_dir or "runlogs"
    os.makedirs(log_dir, exist_ok=True)
    filename = f"{log_dir}/{time.strftime('%Y%m%d-%H%M%S')}-{agent_name}.txt"
    with open(filename, "w", encoding="utf-8") as f:
        f.write(f"=== LLM INVOCATION ({agent_name}) ===\n")
        f.write(f"Duration: {duration:.2f} seconds\n\n")
        f.write("--- INPUT MESSAGES ---\n")
        for m in messages:
            label = m.type.upper()
            if label == "AI" and not m.content and hasattr(m, "tool_calls") and m.tool_calls:
                tool_names = ", ".join(t["name"] for t in m.tool_calls)
                label = f"AI TOOL CALL ({tool_names})"
            f.write(f"[{label}]:\n{m.content}\n\n")
           
        f.write("\n--- RAW RESPONSE ---\n")
        if hasattr(response, "content") and response.content:
            f.write(f"[FINAL OUTPUT TEXT]:\n{response.content}\n\n")
        else:
            f.write("[FINAL OUTPUT TEXT]: (Empty)\n\n")
           
        if hasattr(response, "additional_kwargs") and "reasoning_content" in response.additional_kwargs:
            f.write(f"[REASONING TRACE]:\n{response.additional_kwargs['reasoning_content']}\n\n")
           
        if hasattr(response, "tool_calls") and response.tool_calls:
            f.write("[TOOL CALLS]:\n")
            for t in response.tool_calls:
                f.write(f"  - {t['name']}({t['args']})\n")
        f.write("=== END ===\n")
 
 
# ---------------------------------------------------------------------------
# Token Estimation
# ---------------------------------------------------------------------------
 
def _serialize_messages(messages) -> str:
    """Flatten a list of messages to a single string for token estimation."""
    parts = []
    for m in messages:
        parts.append(m.content or "")
        if hasattr(m, "additional_kwargs") and "reasoning_content" in m.additional_kwargs:
            parts.append(m.additional_kwargs["reasoning_content"] or "")
        if hasattr(m, "tool_calls") and m.tool_calls:
            for t in m.tool_calls:
                parts.append(str(t.get("args", "")))
    return "\n".join(parts)
 
 
def _estimate_tokens(messages) -> int:
    """Estimates token count using ~4 chars/token heuristic (Gemma4-reasonable for English).
    Exact count is obtained post-response from Ollama's prompt_eval_count.
    """
    text = _serialize_messages(messages)
    return max(len(text) // 4, len(messages))
 
 
 
# ---------------------------------------------------------------------------
# Hierarchical Context Window Manager
# ---------------------------------------------------------------------------
 
class ContextManager:
    @staticmethod
    def prune_thinking(messages: list[BaseMessage]) -> list[BaseMessage]:
        """Strips reasoning traces from previous turns (before the latest user command)."""
        last_human_idx = -1
        for i, m in enumerate(messages):
            if isinstance(m, HumanMessage):
                last_human_idx = i
       
        if last_human_idx >= 0:
            for m in messages[:last_human_idx]:
                if hasattr(m, "additional_kwargs") and "reasoning_content" in m.additional_kwargs:
                    m.additional_kwargs["reasoning_content"] = ""
        return messages
 
    @staticmethod
    def strip_ghost_messages(messages: list[BaseMessage]) -> list[BaseMessage]:
        """Removes empty AI messages that carry no content and no tool calls (true ghosts)."""
        return [
            m for m in messages
            if not (isinstance(m, AIMessage) and not m.content and not getattr(m, "tool_calls", None))
        ]
 
    @staticmethod
    def isolate_for_agent(messages: list[BaseMessage], agent_type: str) -> list[BaseMessage]:
        """Pass-through for single-agent architecture. Keeps all messages."""
        return messages
 
    @staticmethod
    def summarize_old_tool_results(messages: list[BaseMessage]) -> list[BaseMessage]:
        """Aggressively compress old tool results to prevent context rot.
 
        Strategy:
        - Last 1 ToolMessage: keep verbatim (model needs the latest result).
        - Older ToolMessages: first line only (~25 tokens vs ~200).
        - "Already explored" navigate results: replace with short tag.
        - The VFS tree is refreshed every turn, so old results are redundant.
        """
        # Count ToolMessages from end to find the 1 most recent
        tool_indices = [i for i, m in enumerate(messages) if isinstance(m, ToolMessage)]
        recent_tool_set = set(tool_indices[-1:]) if len(tool_indices) > 1 else set(tool_indices)
 
        out = []
        for i, m in enumerate(messages):
            if isinstance(m, ToolMessage):
                content = m.content if isinstance(m.content, str) else str(m.content)
                # Compress "already explored" results aggressively
                if "Already explored" in content or "scanned" in content.split("\n", 1)[0]:
                    m = m.copy(update={"content": "[VFS up to date]"})
                elif i not in recent_tool_set:
                    # Old tool results: first line only
                    first_line = content.split("\n", 1)[0][:200]
                    m = m.copy(update={"content": first_line})
            out.append(m)
        return out
 
    @staticmethod
    def get_optimized_messages(messages: list[BaseMessage], agent_type: str, max_tokens: int = 120000) -> list[BaseMessage]:
        """Master entry point — prune, isolate, strip ghosts, then trim."""
        ctx = list(messages)
        ctx = ContextManager.prune_thinking(ctx)
        ctx = ContextManager.strip_ghost_messages(ctx)
        ctx = ContextManager.isolate_for_agent(ctx, agent_type)
        ctx = ContextManager.summarize_old_tool_results(ctx)
       
        tokens_before = _estimate_tokens(ctx)
       
        trimmed = trim_messages(
            ctx,
            max_tokens=max_tokens,
            strategy="last",
            token_counter=_estimate_tokens,
            include_system=True,
            start_on="human"
        )
       
        tokens_after = _estimate_tokens(trimmed)
        pct = round((tokens_after / max_tokens) * 100, 1)
       
        # Log context size silently (display moved to _stream_and_log with exact counts)
        with open("context_window_status.log", "a") as f:
            from datetime import datetime
            f.write(
                f"[{datetime.now().strftime('%H:%M:%S')}] {agent_type}: "
                f"{tokens_after}/{max_tokens} ({pct}%) msgs={len(trimmed)} est\n"
            )
       
        return trimmed
 
 
 
# ---------------------------------------------------------------------------
# Shared LLM & Config
# ---------------------------------------------------------------------------
# Dynamic LLM Factory
# ---------------------------------------------------------------------------
 
def get_llm():
    """Returns a ChatModel instance based on the active provider in config_manager."""
    provider = config_manager.current_provider
    model = config_manager.current_model
    keys = config_manager.api_keys
   
    if provider == "ollama":
        from langchain_ollama import ChatOllama
        # Auto-detect model capabilities via Ollama /api/show
        caps = set()
        try:
            import urllib.request, json as _json
            resp = urllib.request.urlopen(
                urllib.request.Request(
                    "http://localhost:11434/api/show",
                    data=_json.dumps({"name": model}).encode(),
                    headers={"Content-Type": "application/json"},
                ),
                timeout=3,
            )
            info = _json.loads(resp.read())
            caps = set(info.get("capabilities", []))
        except Exception:
            pass  # Ollama unreachable or model not pulled — assume basic
        if caps and "tools" not in caps:
            raise ValueError(
                f"Model '{model}' does not support tool calling "
                f"(capabilities: {', '.join(sorted(caps)) or 'none'}). "
                f"Mārjak requires a model with tool support. "
                f"See: https://ollama.com/search?c=tools"
            )
        use_reasoning = "thinking" in caps
        # Set num_ctx based on preset — smaller context = faster inference for Eco
        preset = config_manager.config.get("preset", "Pro")
        num_ctx = {"Eco": 8192, "Pro": 32768, "Expert": 131072}.get(preset, 32768)
        # Auto-detect native context length from model metadata
        native_ctx = 0
        try:
            mi = info.get("model_info", {})
            for k, v in mi.items():
                if k.endswith(".context_length") and isinstance(v, int):
                    native_ctx = v
                    break
        except Exception:
            pass
        # Use the smaller of preset and native (don't exceed model's training)
        if native_ctx > 0:
            num_ctx = min(num_ctx, native_ctx)
        config_manager._detected_num_ctx = num_ctx
        num_predict = {"Eco": 512, "Pro": 1024, "Expert": 2048}.get(preset, 1024)
        opts = {"num_ctx": num_ctx, "num_predict": num_predict}
        if use_reasoning:
            opts["reasoning"] = True
        return ChatOllama(model=model, **opts)
   
    elif provider == "openai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=model, api_key=keys.get("openai"))
       
    elif provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(model=model, google_api_key=keys.get("gemini"))
       
    elif provider == "claude":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model=model, api_key=keys.get("claude"))
       
    elif provider == "groq":
        from langchain_groq import ChatGroq
        return ChatGroq(model=model, api_key=keys.get("groq"))
       
    elif provider == "openrouter":
        from langchain_openai import ChatOpenAI
        or_config = config_manager.config["providers"].get("openrouter", {})
        return ChatOpenAI(
            model=model,
            api_key=keys.get("openrouter"),
            base_url=or_config.get("base_url", "https://openrouter.ai/api/v1")
        )
   
    # Fallback to Ollama gemma4 (known to support reasoning)
    from langchain_ollama import ChatOllama
    return ChatOllama(model="gemma4", reasoning=True)
 
def get_performance_caps():
    """Returns loop counts and tree density based on the current preset."""
    return config_manager.get_performance_settings()
 
 
class AgentState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], add_messages]
    conversation_summary: str
    original_goal: str  # Anchored from first HumanMessage — survives "continue"
 
 
# ---------------------------------------------------------------------------
# Thinking Indicator — smooth animated line, rate-limited to 150 ms updates
# ---------------------------------------------------------------------------
 
class ThinkingIndicator:
    """Single-line animated thinking indicator driven by a background thread.
 
    Key design decisions:
    - The background thread refreshes the display every REFRESH_MS milliseconds.
      Token arrivals only write to a buffer — they never trigger a display update.
      This prevents the jittery token-by-token flicker.
    - The reasoning excerpt only appears after MIN_CHARS characters have accumulated,
      so the user never sees a 1-2 word flash at the very start of reasoning.
    - transient=True on the Live erases the line completely when stop() is called.
    - start() shows the indicator immediately (for all providers, not just reasoning ones).
    """
    REFRESH_MS  = 150    # display update interval in milliseconds
    MIN_CHARS   = 80     # don't show excerpt until this many chars accumulated
    TAIL_LEN    = 68     # max chars of reasoning excerpt to display
 
    def __init__(self):
        self._buffer      = ""
        self._active      = False
        self._stop_event  = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock        = threading.Lock()
 
    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
 
    def _excerpt(self) -> str:
        """Return the tail of the accumulated buffer, trimmed to a word boundary."""
        with self._lock:
            raw = self._buffer.replace("\n", " ").replace("  ", " ").strip()
        if len(raw) < self.MIN_CHARS:
            return ""   # don't show excerpt until enough reasoning text
        if len(raw) <= self.TAIL_LEN:
            return raw
        tail = raw[-self.TAIL_LEN:]
        # Trim to nearest word boundary so we don't cut mid-word
        space = tail.find(" ")
        return tail[space + 1:] if space != -1 else tail
 
    def _run(self):
        """Background thread: drives the Live display at a fixed rate."""
        frames = itertools.cycle(_SPINNER_FRAMES)
        interval = self.REFRESH_MS / 1000.0
 
        with Live(
            Text.from_markup("[dim magenta]⠋  Thinking …[/dim magenta]"),
            console=_console,
            refresh_per_second=1,   # we drive it manually below
            transient=True,
        ) as live:
            while not self._stop_event.is_set():
                frame   = next(frames)
                excerpt = self._excerpt()
                live.update(
                    Text.from_markup(
                        f"[dim magenta]{frame}  Thinking …[/dim magenta]"
                        + (f"[dim]  {excerpt}[/dim]" if excerpt else "")
                    )
                )
                self._stop_event.wait(interval)   # interruptible sleep
 
    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
 
    def start(self):
        """Start the thinking indicator immediately.
 
        Shows 'Thinking ...' right away for ALL providers (not just those
        with reasoning_content).  Safe to call multiple times.
        """
        if not self._active:
            self._active = True
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
 
    def feed(self, text: str):
        """Append reasoning text to the buffer (excerpt shown once MIN_CHARS reached)."""
        if not text:
            return
        with self._lock:
            self._buffer += text
 
    def stop(self):
        """Stop the indicator and erase the line.  Idempotent."""
        if self._active:
            self._stop_event.set()
            if self._thread:
                self._thread.join(timeout=2.0)
            self._active  = False
            self._thread  = None
            with self._lock:
                self._buffer = ""
 
 
# ---------------------------------------------------------------------------
# Shared Streaming Helper (DRY)
# ---------------------------------------------------------------------------
 
def _build_hallucination_fallback(messages) -> str:
    """Build a useful fallback when the model hallucinates instead of summarizing.
 
    Extracts key data from ToolMessage results and the original question to
    produce a >100 char response that actually answers the user.
    """
    # Find the user's question
    user_q = ""
    for m in messages:
        if isinstance(m, HumanMessage):
            user_q = m.content if isinstance(m.content, str) else str(m.content)
 
    # Extract key lines from each tool result
    tool_summaries = []
    for m in messages:
        if isinstance(m, ToolMessage):
            content = m.content if isinstance(m.content, str) else str(m.content)
            lines = content.strip().split("\n")
            # Take first meaningful line (header) + any lines with sizes
            header = lines[0] if lines else ""
            size_lines = [l.strip() for l in lines[1:20] if any(u in l for u in ("GB", "MB", "KB", "Gi", "Mi"))]
            if header:
                tool_summaries.append(header)
            tool_summaries.extend(size_lines[:5])
 
    if not tool_summaries:
        return "I explored your filesystem but could not generate a complete summary. Please try a more specific question."
 
    summary_text = "\n".join(f"• {line}" for line in tool_summaries[:15])
    return f"Here's what I found on your system:\n\n{summary_text}\n\nPlease ask a follow-up if you'd like more detail on any of these."
 
 
def _stream_and_log(bound_llm, trimmed: list, config: RunnableConfig, agent_name: str):
    """Streams LLM response with live Rich markdown rendering."""
    from rich.live import Live
    from rich.markdown import Markdown as RichMarkdown
 
    start = time.time()
    response_chunk = None
    in_reasoning = False
    content_parts: list[str] = []
    first_token = True
    thinking = ThinkingIndicator()
    thinking.start()   # show indicator immediately for ALL providers
 
    for chunk in bound_llm.stream(trimmed, config=config):
        if hasattr(chunk, "additional_kwargs") and "reasoning_content" in chunk.additional_kwargs:
            r = chunk.additional_kwargs["reasoning_content"]
            if r:
                if not in_reasoning:
                    in_reasoning = True
                thinking.feed(r)   # just write to buffer, never touch display directly
 
        if chunk.content:
            if first_token:
                thinking.stop()    # erases the indicator line
                _console.file.flush()
                in_reasoning = False
                first_token = False
                # Start Rich Live context for progressive markdown rendering
                live = Live(RichMarkdown(""), console=_console, refresh_per_second=8, vertical_overflow="visible")
                live.start()
            content_parts.append(chunk.content)
            # Update the live display with accumulated text rendered as markdown
            live.update(RichMarkdown("".join(content_parts)))
 
        if response_chunk is None:
            response_chunk = chunk
        else:
            response_chunk += chunk
 
    # Stop live rendering
    if content_parts:
        # Final render with complete text
        full_text = "".join(content_parts)
        live.update(RichMarkdown(full_text))
        live.stop()
 
    # clean up indicator if model produced only reasoning / no content
    thinking.stop()
 
    # Strip leaked FID references from the accumulated content for the response object
    if content_parts:
        full_text = "".join(content_parts)
        full_text = re.sub(r"\[FID:\s*\d+\]", "", full_text)
        # Update the response content with cleaned text (no re-render needed — already streamed)
    response = response_chunk
    if response and content_parts:
        cleaned = re.sub(r"\[FID:\s*\d+\]", "", "".join(content_parts))
        if cleaned != response.content:
            response = response.copy(update={"content": cleaned})
 
    # --- Hallucination scrub ---
    # Gemma4 sometimes generates fake tool output: response:unknown{value:<|"|>...
    # This is the model hallucinating a navigate/search result instead of calling the tool.
    # Detect and replace with a useful summary built from actual tool results.
    if response and response.content:
        c = response.content
        if "response:unknown" in c or 'value:<|"|>' in c or "<tool_call|>" in c:
            # Build a meaningful fallback from actual tool results in the conversation
            fallback = _build_hallucination_fallback(trimmed)
            response = response.copy(update={"content": fallback})
            _console.print(f"[dim red]  ⚠ Hallucinated tool output detected and scrubbed[/dim red]")
 
    # --- Reasoning-only rescue ---
    # If model produced reasoning but no content and no tool calls,
    # extract the last paragraph from reasoning as visible content.
    if response and not response.content and not getattr(response, "tool_calls", None):
        reasoning = (response.additional_kwargs or {}).get("reasoning_content", "")
        if reasoning and len(reasoning) > 20:
            # Take the last meaningful paragraph as the answer
            paras = [p.strip() for p in reasoning.split("\n\n") if p.strip()]
            rescued = paras[-1] if paras else reasoning[-500:]
            if len(rescued) > 800:
                rescued = rescued[:800] + "…"
            response = response.copy(update={"content": rescued})
            from rich.markdown import Markdown as RichMarkdown
            _console.print(RichMarkdown(rescued))
            _console.print("[dim yellow]  (rescued from reasoning trace)[/dim yellow]")
 
    duration = time.time() - start
 
    # Token count: prefer Ollama's exact prompt_eval_count from the final streaming chunk.
    prompt_tokens = 0
    if hasattr(response, "response_metadata") and response.response_metadata:
        prompt_tokens = response.response_metadata.get("prompt_eval_count", 0)
    if not prompt_tokens and hasattr(response, "usage_metadata") and response.usage_metadata:
        prompt_tokens = response.usage_metadata.get("input_tokens", 0)
 
    if prompt_tokens:
        MAX_CTX = config_manager.context_window
        pct = round((prompt_tokens / MAX_CTX) * 100, 1)
        bar_filled = int(pct / 5)
        bar = "█" * bar_filled + "░" * (20 - bar_filled)
        color = "green" if pct < 60 else "yellow" if pct < 85 else "red"
        # Add spacing between consecutive tool turns to avoid wall-of-text
        global _last_turn_was_tool
        has_tool_calls = hasattr(response, "tool_calls") and response.tool_calls
        if _last_turn_was_tool and has_tool_calls:
            _console.print()  # blank line separator
        _last_turn_was_tool = bool(has_tool_calls)
        _console.print(
            f"[dim]🧠 [{agent_name}] [{color}]{bar}[/{color}] "
            f"{prompt_tokens:,} / {MAX_CTX:,} tokens ({pct}%)[/dim]"
        )
        with open("context_window_status.log", "a") as f:
            f.write(f"[{datetime.now().strftime('%H:%M:%S')}] {agent_name}: {prompt_tokens}/{MAX_CTX} ({pct}%) EXACT\n")
 
    write_llm_log(agent_name, trimmed, response, duration)
    return response
 
 
 
# ---------------------------------------------------------------------------
# Single Mārjak Agent — Unified read + write
# ---------------------------------------------------------------------------
 
# Base tools (all presets). Expert adds run_shell dynamically.
_tools_base = [navigate, search_system, get_system_overview, collect_deletable_files,
               move_to_trash, execute_deep_clean, run_system_optimization]
_tools_expert = _tools_base + [run_shell]
 
def _get_tools():
    """Return tool list based on active preset."""
    preset = config_manager.config.get("preset", "Pro")
    return _tools_expert if preset == "Expert" else _tools_base
 
# ToolNode needs ALL possible tools registered so it can dispatch any of them.
tool_node = ToolNode(_tools_expert)
 
# ---------------------------------------------------------------------------
# Tool-Alias Repair — intercept hallucinated tool names before dispatch
# ---------------------------------------------------------------------------
_TOOL_ALIASES = {
    "collect_candidates":           "collect_deletable_files",
    "search_file_system":           "search_system",
    "search_system_for_keywords":   "search_system",
    "search_system_directories":    "navigate",
    "search_system_for_user_data":  "search_system",
    "search_files":                 "search_system",
    "list_directory":               "navigate",
    "find_files":                   "search_system",
    "delete_files":                 "move_to_trash",
}
 
def tool_node_with_repair(state: AgentState):
    """Fix hallucinated tool names, then dispatch to real ToolNode."""
    messages = list(state["messages"])
    last = messages[-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        new_calls = []
        repaired_any = False
        for tc in last.tool_calls:
            name = tc["name"]
            if name in _TOOL_ALIASES:
                tc = {**tc, "name": _TOOL_ALIASES[name]}
                repaired_any = True
            new_calls.append(tc)
        if repaired_any:
            messages[-1] = last.copy(update={"tool_calls": new_calls})
            state = {**state, "messages": messages}
    return tool_node.invoke(state)
 
 
def _build_rule_summary(state: AgentState) -> str:
    """Build a compact rule-based conversation summary for context anchoring.
 
    Extracts the user's original question, top VFS findings, and recent
    actions — all without an extra LLM call.  ~200 tokens.
    """
    parts = []
 
    # 1. User's original question (first HumanMessage)
    for m in state["messages"]:
        if isinstance(m, HumanMessage):
            q = m.content if isinstance(m.content, str) else str(m.content)
            parts.append(f"USER ASKED: {q[:200]}")
            break
 
    # 2. Top VFS findings (largest 5 explored directories)
    if session_book.nodes:
        dirs = [
            (p, n.get("size", 0))
            for p, n in session_book.nodes.items()
            if n.get("type") == "DIR" and n.get("size", 0) > 0
        ]
        dirs.sort(key=lambda x: x[1], reverse=True)
        home = os.path.expanduser("~")
        top = []
        for p, s in dirs[:5]:
            try:
                rel = "~/" + os.path.relpath(p, home)
            except (ValueError, TypeError):
                rel = p
            top.append(f"{rel} ({_human_size(s)})")
        if top:
            parts.append("TOP FINDINGS: " + ", ".join(top))
 
    # 3. Recent actions from persistent store
    recent = fs_memory.get_recent_actions(3)
    if recent:
        acts = "; ".join(f"{a['action']}: {a['detail'][:50]}" for a in recent)
        parts.append(f"RECENT ACTIONS: {acts}")
 
    return "\n".join(parts) if parts else ""
 
 
def _human_size(nbytes: int) -> str:
    """Format bytes into human-readable string (duplicated here to avoid circular import)."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(nbytes) < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} PB"
 
 
# ---------------------------------------------------------------------------
# Quick Mode — bypass LLM for trivial single-tool queries
# ---------------------------------------------------------------------------
 
_QUICK_PATTERNS: list[tuple[re.Pattern, str, dict]] = [
    # "how much space", "disk space", "storage" → get_system_overview()
    (re.compile(r"\b(how\s+much\s+space|disk\s*space|storage\s+overview|free\s+space)\b", re.I),
     "get_system_overview", {}),
    # "what's in /path" or "show /path" → navigate(path)
    (re.compile(r"^(?:what(?:'s| is) in|show|list|ls)\s+([~/][\w/.\-]+)", re.I),
     "navigate", None),  # None = extract arg from group(1)
]
 
 
def try_quick_mode(user_text: str) -> str | None:
    """If *user_text* matches a known single-tool pattern, execute it directly
    and return a formatted result string.  Returns ``None`` for fall-through to
    the full LLM graph.
 
    This saves 1-3 LLM round-trips for trivially-answerable queries.
    """
    for pat, tool_name, static_kwargs in _QUICK_PATTERNS:
        m = pat.search(user_text)
        if not m:
            continue
        try:
            if tool_name == "get_system_overview":
                result = get_system_overview.invoke({})
            elif tool_name == "navigate":
                path = m.group(1)
                result = navigate.invoke({"path": path})
            else:
                continue
            return str(result)
        except Exception:
            return None  # fall through on error
    return None
 
 
# ---------------------------------------------------------------------------
# Planner — lightweight keyword-based plan injection for multi-step queries
# ---------------------------------------------------------------------------
# Rules: conservative matching only. A wrong plan is WORSE than no plan.
# Only inject when we're highly confident about the workflow.
 
def _generate_plan(query: str) -> str:
    """Return a short plan string for multi-step queries, or empty string.
 
    Conservative: returns empty string for ambiguous queries. A wrong plan
    actively hurts SLM performance by overriding the model's own reasoning.
 
    Plans guide WORKFLOW only — never reveal specific paths, app names, or
    tool arguments. The model must discover those via tools.
    """
    if not query or len(query) < 10:
        return ""
    ql = query.lower()
 
    # Clean/delete/optimize workflows (explicit action verbs + target)
    if re.search(r"\b(clean|free\s+up|reclaim|delete|remove)\b", ql) and \
       re.search(r"\b(space|disk|storage|cache|junk|temp)\b", ql):
        return (
            "1. get_system_overview() to check current disk usage\n"
            "2. collect_deletable_files() to find reclaimable space\n"
            "3. Report findings to user and ASK before deleting\n"
            "4. Only call execute_deep_clean/move_to_trash if user explicitly confirms"
        )
 
    # "My Mac is slow / fix it" — diagnose only
    if re.search(r"\b(slow|sluggish|lagging|freezing)\b", ql):
        return (
            "1. get_system_overview() ONCE to diagnose CPU/RAM/Disk\n"
            "2. Summarize the diagnosis for the user\n"
            "3. STOP — do NOT run cleanup tools without explicit user consent"
        )
 
    # "Find [thing]" or "where is [thing]" — search workflow
    if re.search(r"\b(find|where|locate|search)\b", ql):
        return (
            "1. search_system() to discover locations — do NOT guess paths\n"
            "2. If results found, navigate into the largest directories\n"
            "3. Summarize ALL locations found with sizes"
        )
 
    # "Browser" queries — multi-target search workflow
    if re.search(r"\bbrowser", ql):
        return (
            "1. The user may have MULTIPLE browsers installed\n"
            "2. Use search_system() for each browser you know about\n"
            "3. Summarize findings for ALL browsers found, not just one\n"
            "4. Only delete if user explicitly asks"
        )
 
    # Don't inject plans for anything else. The model's own reasoning
    # is better than a wrong plan for navigate/search/explore queries.
    return ""
 
 
def _extract_tool_findings(messages) -> str:
    """Build a condensed summary of tool results for force-stop nudge.
 
    Keeps the first ~200 chars of each ToolMessage so the model has enough
    context to produce a meaningful summary without exceeding the context window.
    """
    findings = []
    for m in messages:
        if isinstance(m, ToolMessage):
            content = m.content if isinstance(m.content, str) else str(m.content)
            # Grab the first line (tool header) plus truncated body
            lines = content.split("\n")
            header = lines[0] if lines else ""
            body = "\n".join(lines[1:])
            if len(body) > 200:
                body = body[:200] + "…"
            findings.append(f"- {header}\n  {body}" if body else f"- {header}")
    return "\n".join(findings) if findings else "(No tool results captured)"
 
 
def marjak_node(state: AgentState, config: RunnableConfig):
    """Single unified agent node — handles exploration AND actions."""
    messages = list(state["messages"])
    caps = get_performance_caps()
    ctx_window = config_manager.context_window
 
    # --- Force-stop detection ---
    # When should_continue() routes back here (instead of to tools node),
    # the last message is an AIMessage with tool_calls but NO ToolMessage
    # after it.  Conditional edges cannot mutate state in LangGraph, so the
    # nudge must be injected here — in the node function where state changes
    # are properly tracked.
    last_msg = messages[-1] if messages else None
    force_summary = (
        isinstance(last_msg, AIMessage)
        and getattr(last_msg, "tool_calls", None)
        and any(isinstance(m, ToolMessage) for m in messages)  # not first turn
    )
    if force_summary:
        # Strip the dangling tool call the model produced
        messages = messages[:-1]
        # Build condensed findings from all prior tool results
        findings = _extract_tool_findings(messages)
        nudge = SystemMessage(
            content=(
                "STOP — you have used enough tool calls for this request.\n"
                "Here is a summary of what your tools returned:\n"
                f"{findings}\n\n"
                "Now write a CLEAR, DETAILED summary for the user. "
                "Include folder names, file sizes, and any recommendations. "
                "Do NOT call any more tools."
            )
        )
        messages.append(nudge)
 
    # --- Empty-response retry detection ---
    # If the model produced an empty response (no content, no tool_calls) and
    # was routed back here, inject a nudge to try again.
    elif (
        isinstance(last_msg, AIMessage)
        and not last_msg.content
        and not getattr(last_msg, "tool_calls", None)
        and any(isinstance(m, ToolMessage) for m in messages)
    ):
        findings = _extract_tool_findings(messages)
        nudge = SystemMessage(
            content=(
                "You produced an empty response. Summarize what you found:\n"
                f"{findings}\n"
                "Answer the user's question with concrete details."
            )
        )
        messages.append(nudge)
 
    # --- Persistent system knowledge from SQLite ---
    # Extract user query early so we can use it for fs_memory context
    user_query = ""
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            user_query = m.content if isinstance(m.content, str) else str(m.content)
            break
    system_knowledge = fs_memory.get_context_for_query(user_query)
 
    # Scale tree budget: more nodes = tighter char limit to avoid prompt bloat
    node_count = len(session_book.nodes)
    tree_chars = min(caps.get("tree_chars", 8000), max(2000, 6000 - node_count * 80))
    book_view = session_book.render_tree(
        max_chars=tree_chars,
        max_children_per_dir=min(caps.get("max_children", 15), 10),
    )
   
    active_prov = f"{config_manager.current_provider.upper()} ({config_manager.current_model})"
    preset = config_manager.config.get("preset", "Pro")
    full_prompt = get_prompt(preset, config_manager.current_provider) + f"\n\n[SYSTEM STATE]\nProvider: {active_prov}"
 
    # Inject conversation summary (context anchor for long sessions)
    summary = state.get("conversation_summary", "")
    if summary:
        full_prompt += f"\n\n[SESSION SUMMARY]\n{summary}"
 
    if session_book.nodes:
        full_prompt += f"\n\n<explored_this_session>\n{book_view}\n</explored_this_session>"
 
    if system_knowledge:
        full_prompt += f"\n\n<system_knowledge>\n{system_knowledge}\n</system_knowledge>"
 
    # Retrieve relevant macOS filesystem knowledge on demand
    # --- Original goal preservation ---
    # Use original_goal if set; otherwise extract from first HumanMessage
    original_goal = state.get("original_goal", "")
    # Lock original_goal on the very first human message (not "continue"/"yes"/etc.)
    if not original_goal and user_query and len(user_query) > 10:
        original_goal = user_query
    # For current_goal: use original_goal as anchor, append latest follow-up if different
    goal_text = original_goal or user_query
    if user_query and user_query != original_goal and len(user_query) > 10:
        goal_text = f"{original_goal}\n(Follow-up: {user_query[:200]})"
 
    guidebook_text = retrieve_guidebook(user_query or original_goal, list(session_book.nodes.keys()))
    if guidebook_text:
        full_prompt += f"\n\n<macos_knowledge>\n{guidebook_text}\n</macos_knowledge>"
 
    # Inject a lightweight plan for multi-step queries (helps SLMs stay on track)
    plan = _generate_plan(user_query or original_goal)
    if plan:
        full_prompt += f"\n\n<plan>\n{plan}\n</plan>"
 
    # Anchor the user's goal prominently at the end of the system prompt
    if goal_text:
        full_prompt += (
            f"\n\n<current_goal>\nThe user's request: {goal_text[:400]}\n"
            "Follow the <plan> if provided. Fulfill this using tools. "
            "You may explain your findings to the user between tool calls.\n</current_goal>"
        )
 
    if not messages or not isinstance(messages[0], SystemMessage):
        messages = [SystemMessage(content=full_prompt)] + messages
    else:
        messages[0] = SystemMessage(content=full_prompt)
 
    trimmed = ContextManager.get_optimized_messages(messages, "marjak", max_tokens=ctx_window)
   
    # Dynamic LLM binding — Expert preset gets run_shell, others don't
    active_tools = _get_tools()
    llm_instance = get_llm().bind_tools(active_tools)
    response = _stream_and_log(llm_instance, trimmed, config, "marjak")
    return {"messages": [response], "original_goal": original_goal}
 
 
# ---------------------------------------------------------------------------
# Core Workflow Logic
# ---------------------------------------------------------------------------
 
def _count_recent_tool_calls(messages) -> int:
    """Count ToolMessages in the current conversation."""
    count = 0
    for m in reversed(messages):
        if isinstance(m, ToolMessage):
            count += 1
    return count
 
def should_continue(state: AgentState):
    """Decide whether to continue the tool loop or end.
 
    IMPORTANT: This is a LangGraph conditional edge function.
    It must ONLY return routing decisions — no state mutations.
    All nudge injection happens in marjak_node() which is a proper node.
    """
    messages = state["messages"]
    last = messages[-1]
    caps = get_performance_caps()
    tool_count = _count_recent_tool_calls(messages)
 
    # Use the combined budget: nav_loops + exec_loops (single agent gets both)
    max_loops = caps["nav_loops"] + caps["exec_loops"]
 
    if isinstance(last, AIMessage) and last.tool_calls:
        # --- Already tried force-stop? ---
        # If the previous message is ALSO an AIMessage with tool_calls
        # (no ToolMessage between them), force-stop was already attempted
        # once and the model ignored the nudge. Give up.
        if len(messages) >= 2 and isinstance(messages[-2], AIMessage) and getattr(messages[-2], "tool_calls", None):
            return END
 
        # --- Context-aware early stop ---
        ctx_window = config_manager.context_window
        est_tokens = _estimate_tokens(messages)
        fill_pct = est_tokens / ctx_window if ctx_window > 0 else 0
        if fill_pct > 0.75:
            # Route back to marjak_node; it will detect the dangling tool call
            # and inject compressed findings + nudge
            return "marjak"
 
        # --- Total tool-call depth check (since last HumanMessage) ---
        tool_turns_since_human = 0
        for m in reversed(messages):
            if isinstance(m, HumanMessage):
                break
            if isinstance(m, AIMessage) and m.tool_calls:
                tool_turns_since_human += 1
        if tool_turns_since_human >= 4:
            # Route back to marjak_node for force-summary
            return "marjak"
 
        # Normal case: let the tool execute
        return "tools"
 
    # --- Budget exhaustion safeguard ---
    if tool_count >= max_loops:
        return END
 
    # --- Empty response handling ---
    if isinstance(last, AIMessage) and not last.content and not last.tool_calls:
        # Check if we already retried (avoid infinite loop)
        if len(messages) >= 2 and isinstance(messages[-2], SystemMessage):
            return END  # Already nudged via marjak_node, give up
        # Route back to marjak_node; it will detect empty response and add nudge
        return "marjak"
 
    return END
 
# ---------------------------------------------------------------------------
# Construct the Master Graph (Single Agent)
# ---------------------------------------------------------------------------
 
master_workflow = StateGraph(AgentState)
 
master_workflow.add_node("marjak", marjak_node)
master_workflow.add_node("tools", tool_node_with_repair)
 
master_workflow.set_entry_point("marjak")
 
master_workflow.add_conditional_edges("marjak", should_continue)
master_workflow.add_edge("tools", "marjak")  # Always loop back after tool execution
 
master_memory = MemorySaver()
master_app = master_workflow.compile(checkpointer=master_memory)
 
