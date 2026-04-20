# agent.py — Mārjak v4: Hierarchical Context Management
#
# Navigator Agent: read-only exploration (navigate, search, scan, overview)
# Executor Agent:  destructive actions (clean, optimize, move_to_trash)
#
# Both use the same Gemma 4 model with reasoning=True.
# They run sequentially — only 1 Ollama slot used at a time.

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
from config_manager import config_manager

from tools import (
    navigate,
    mole_scan,
    search_system,
    get_system_overview,
    collect_deletable_files,
    run_shell,
    execute_deep_clean,
    run_system_optimization,
    move_to_trash,
    call_executor,
    call_navigator,
    memory as persistent_memory,
    session_book,
)
from prompts import get_navigator_prompt, get_executor_prompt
from guidebook import retrieve_guidebook
import time
import os
import sys

# Top-level console (highlight=False to prevent random blue numbers)
_console = Console(highlight=False)

# Braille spinner frames for thinking indicator
_SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def write_llm_log(agent_name: str, messages: list, response, duration: float):
    """Writes everything the LLM saw and did to runlogs/ for debugging."""
    os.makedirs("runlogs", exist_ok=True)
    filename = f"runlogs/{time.strftime('%Y%m%d-%H%M%S')}-{agent_name}.txt"
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
        """Ensures agents only see their relevant history to prevent cross-agent confusion."""
        nav_tool_names = {"navigate", "search_system", "get_system_overview", "collect_deletable_files", "call_executor", "run_shell"}
        exec_tool_names = {"execute_deep_clean", "run_system_optimization", "move_to_trash", "call_navigator"}
        
        my_tools = nav_tool_names if agent_type == "navigator" else exec_tool_names
        
        filtered = []
        keep_ids = set()
        
        # Pre-pass: Identify tool call IDs we want to keep
        for m in messages:
            if isinstance(m, AIMessage) and m.tool_calls:
                for t in m.tool_calls:
                    if t["name"] in my_tools:
                        keep_ids.add(t["id"])
            # Executor specifically needs the handoff instruction from the Navigator
            if agent_type == "executor" and isinstance(m, AIMessage) and m.tool_calls:
                 for t in m.tool_calls:
                     if t["name"] == "call_executor":
                         keep_ids.add(t["id"])

        for m in messages:
            if isinstance(m, (HumanMessage, SystemMessage)):
                filtered.append(m)
            elif isinstance(m, AIMessage):
                if not m.tool_calls:
                    filtered.append(m)
                else:
                    if any(t["id"] in keep_ids for t in m.tool_calls):
                        # Skip the Navigator's AI message that triggered the handoff
                        if agent_type == "executor" and any(t["name"] == "call_executor" for t in m.tool_calls):
                            continue
                        filtered.append(m)
            elif isinstance(m, ToolMessage):
                if m.tool_call_id in keep_ids:
                    # Convert call_executor result into a System Instruction for the Executor
                    if agent_type == "executor" and m.name == "call_executor":
                        filtered.append(SystemMessage(content=f"COMMAND INSTRUCTION: {m.content}"))
                    else:
                        filtered.append(m)
                        
        return filtered

    @staticmethod
    def summarize_old_tool_results(messages: list[BaseMessage]) -> list[BaseMessage]:
        """Aggressively compress old tool results to prevent context rot.

        Strategy:
        - Last 3 ToolMessages (by position): keep verbatim.
        - Older ToolMessages: keep only the first line (summary).
        - "Already explored" navigate results: replace with short tag.
        - The VFS tree is refreshed every turn, so old results are redundant.
        """
        last_human_idx = -1
        for i, m in enumerate(messages):
            if isinstance(m, HumanMessage):
                last_human_idx = i

        if last_human_idx <= 0:
            return messages

        # Count ToolMessages from end to find the 3 most recent
        tool_indices = [i for i, m in enumerate(messages) if isinstance(m, ToolMessage)]
        recent_tool_set = set(tool_indices[-3:]) if len(tool_indices) > 3 else set(tool_indices)

        out = []
        for i, m in enumerate(messages):
            if isinstance(m, ToolMessage):
                content = m.content if isinstance(m.content, str) else str(m.content)
                # Compress "already explored" results aggressively
                if "Already explored" in content or "scanned" in content.split("\n", 1)[0]:
                    m = m.copy(update={"content": "[VFS up to date]"})
                elif i not in recent_tool_set and i < last_human_idx:
                    # Old tool results before last human message: first line only
                    first_line = content.split("\n", 1)[0]
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
        
        # Always-visible context window gauge (doesn't depend on Ollama response metadata)
        bar_filled = int(pct / 5)   # 20 chars = 100%
        bar_empty  = 20 - bar_filled
        bar = "█" * bar_filled + "░" * bar_empty
        color = "green" if pct < 60 else "yellow" if pct < 85 else "red"
        _console.print(
            f"[dim]🧠 [{agent_type}] Context: [{color}]{bar}[/{color}] "
            f"{tokens_after:,} / {max_tokens:,} est. tokens ({pct}%)"
            + (f" [compressed {tokens_before - tokens_after:,}]" if tokens_before > tokens_after else "")
            + "[/dim]"
        )
        
        with open("context_window_status.log", "a") as f:
            from datetime import datetime
            f.write(
                f"[{datetime.now().strftime('%H:%M:%S')}] {agent_type}: "
                f"{tokens_after}/{max_tokens} ({pct}%) msgs={len(trimmed)}\n"
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
        return ChatOllama(model=model, reasoning=True)
    
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
    
    # Fallback to Ollama
    from langchain_ollama import ChatOllama
    return ChatOllama(model="gemma4", reasoning=True)

def get_performance_caps():
    """Returns loop counts and tree density based on the current preset."""
    return config_manager.get_performance_settings()


class AgentState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], add_messages]
    conversation_summary: str


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

def _stream_and_log(bound_llm, trimmed: list, config: RunnableConfig, agent_name: str):
    """Streams LLM response to console with reasoning frames, renders Markdown output."""
    from rich.markdown import Markdown as RichMarkdown

    start = time.time()
    response_chunk = None
    in_reasoning = False
    content_parts: list[str] = []
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
            if not content_parts:
                thinking.stop()    # erases the indicator line
                _console.file.flush()
                in_reasoning = False
            content_parts.append(chunk.content)

        if response_chunk is None:
            response_chunk = chunk
        else:
            response_chunk += chunk

    # clean up indicator if model produced only reasoning / no content
    thinking.stop()

    # Render accumulated content as styled Markdown
    if content_parts:
        full_text = "".join(content_parts)
        # Strip leaked FID references before display
        full_text = re.sub(r"\[FID:\s*\d+\]", "", full_text)
        _console.print(RichMarkdown(full_text))
    response = response_chunk
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
        _console.print(
            f"[dim]└─ 🧠 [{agent_name}] [{color}]{bar}[/{color}] "
            f"{prompt_tokens:,} / {MAX_CTX:,} tokens (exact, {pct}%)[/dim]"
        )
        with open("context_window_status.log", "a") as f:
            f.write(f"[{datetime.now().strftime('%H:%M:%S')}] {agent_name}: {prompt_tokens} tokens EXACT\n")

    write_llm_log(agent_name, trimmed, response, duration)
    return response



# ---------------------------------------------------------------------------
# Navigator Agent — Read-only exploration
# ---------------------------------------------------------------------------

# Base nav tools (all presets). Expert adds run_shell dynamically.
_nav_tools_base = [navigate, search_system, get_system_overview, collect_deletable_files, call_executor]
_nav_tools_expert = _nav_tools_base + [run_shell]

def _get_nav_tools():
    """Return nav tool list based on active preset."""
    preset = config_manager.config.get("preset", "Pro")
    return _nav_tools_expert if preset == "Expert" else _nav_tools_base

# ToolNode needs ALL possible tools registered so it can dispatch any of them.
nav_tool_node = ToolNode(_nav_tools_expert)


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

    # 3. Recent actions from session history
    history = persistent_memory.data.get("session_history", [])[-3:]
    if history:
        acts = "; ".join(f"{h['action']}: {h['finding'][:50]}" for h in history)
        parts.append(f"RECENT ACTIONS: {acts}")

    return "\n".join(parts) if parts else ""


def _human_size(nbytes: int) -> str:
    """Format bytes into human-readable string (duplicated here to avoid circular import)."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(nbytes) < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} PB"


def navigator_node(state: AgentState, config: RunnableConfig):
    messages = list(state["messages"])
    caps = get_performance_caps()
    ctx_window = config_manager.context_window

    mem_ctx = persistent_memory.get_context_for_prompt()
    book_view = session_book.render_tree(
        max_chars=caps.get("tree_chars", 8000),
        max_children_per_dir=caps.get("max_children", 15),
    )
    
    active_prov = f"{config_manager.current_provider.upper()} ({config_manager.current_model})"
    preset = config_manager.config.get("preset", "Pro")
    full_prompt = get_navigator_prompt(preset, config_manager.current_provider) + f"\n\n[SYSTEM STATE]\nProvider: {active_prov}"
    if mem_ctx:
        full_prompt += f"\n\nSession Memory:\n{mem_ctx}"

    # Inject conversation summary (context anchor for long sessions)
    summary = state.get("conversation_summary", "")
    if summary:
        full_prompt += f"\n\n[SESSION SUMMARY]\n{summary}"

    if session_book.nodes:
        full_prompt += f"\n\n<vfs_playbook>\n{book_view}\n</vfs_playbook>"

    # Retrieve relevant macOS filesystem knowledge on demand
    user_query = ""
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            user_query = m.content if isinstance(m.content, str) else str(m.content)
            break
    guidebook_text = retrieve_guidebook(user_query, list(session_book.nodes.keys()))
    if guidebook_text:
        full_prompt += f"\n\n<macos_knowledge>\n{guidebook_text}\n</macos_knowledge>"

    if not messages or not isinstance(messages[0], SystemMessage):
        messages = [SystemMessage(content=full_prompt)] + messages
    else:
        messages[0] = SystemMessage(content=full_prompt)

    trimmed = ContextManager.get_optimized_messages(messages, "navigator", max_tokens=ctx_window)
    
    # Dynamic LLM binding — Expert preset gets run_shell, others don't
    active_tools = _get_nav_tools()
    llm_instance = get_llm().bind_tools(active_tools)
    response = _stream_and_log(llm_instance, trimmed, config, "navigator")
    return {"messages": [response]}


# ---------------------------------------------------------------------------
# Executor Agent — Destructive actions
# ---------------------------------------------------------------------------

exec_tools = [execute_deep_clean, run_system_optimization, move_to_trash, call_navigator]
exec_tool_node = ToolNode(exec_tools)

def executor_node(state: AgentState, config: RunnableConfig):
    messages = list(state["messages"])
    caps = get_performance_caps()
    ctx_window = config_manager.context_window

    mem_ctx = persistent_memory.get_context_for_prompt()
    book_view = session_book.render_tree(
        max_chars=caps.get("tree_chars", 8000),
        max_children_per_dir=caps.get("max_children", 15),
    )
    
    active_prov = f"{config_manager.current_provider.upper()} ({config_manager.current_model})"
    preset = config_manager.config.get("preset", "Pro")
    full_prompt = get_executor_prompt(preset, config_manager.current_provider) + f"\n\n[SYSTEM STATE]\nProvider: {active_prov}"
    if mem_ctx:
        full_prompt += f"\n\nSession Memory:\n{mem_ctx}"

    # Inject conversation summary (context anchor for long sessions)
    summary = state.get("conversation_summary", "")
    if summary:
        full_prompt += f"\n\n[SESSION SUMMARY]\n{summary}"

    if session_book.nodes:
        full_prompt += f"\n\n<vfs_playbook>\n{book_view}\n</vfs_playbook>"

    if not messages or not isinstance(messages[0], SystemMessage):
        messages = [SystemMessage(content=full_prompt)] + messages
    else:
        messages[0] = SystemMessage(content=full_prompt)

    trimmed = ContextManager.get_optimized_messages(messages, "executor", max_tokens=ctx_window)
    
    # Dynamic LLM binding with tools
    llm_instance = get_llm().bind_tools(exec_tools)
    response = _stream_and_log(llm_instance, trimmed, config, "executor")
    return {"messages": [response]}


# ---------------------------------------------------------------------------
# Core Workflow Logic
# ---------------------------------------------------------------------------

def _count_recent_tool_calls(messages, since_tool_name: str = None) -> int:
    """Count tool messages since the last agent handoff (call_executor/call_navigator).
    This prevents Navigator's budget from being eaten by prior Executor turns and vice versa."""
    count = 0
    for m in reversed(messages):
        if isinstance(m, ToolMessage):
            if m.name in ("call_executor", "call_navigator"):
                break  # stop counting at the last handoff boundary
            count += 1
    return count

def nav_should_continue(state: AgentState):
    messages = state["messages"]
    last = messages[-1]
    caps = get_performance_caps()
    tool_count = _count_recent_tool_calls(messages)

    # Update conversation summary at tier-specific intervals
    interval = caps.get("summary_interval", 4)
    if tool_count > 0 and tool_count % interval == 0:
        state["conversation_summary"] = _build_rule_summary(state)

    if tool_count >= caps["nav_loops"]:
        return END
    if last.tool_calls:
        return "nav_tools"
    return END

def exec_should_continue(state: AgentState):
    messages = state["messages"]
    last = messages[-1]
    caps = get_performance_caps()
    tool_count = _count_recent_tool_calls(messages)

    # Update conversation summary at tier-specific intervals
    interval = caps.get("summary_interval", 4)
    if tool_count > 0 and tool_count % interval == 0:
        state["conversation_summary"] = _build_rule_summary(state)

    if tool_count >= caps["exec_loops"]:
        return END
    if last.tool_calls:
        return "exec_tools"
    return END

def route_after_nav_tools(state: AgentState):
    last = state["messages"][-1]
    if hasattr(last, "name") and last.name == "call_executor":
        _console.print("\n[bold cyan]⚡ Executor agent activated[/bold cyan]")
        return "executor"
    return "navigator"

def route_after_exec_tools(state: AgentState):
    last = state["messages"][-1]
    if hasattr(last, "name") and last.name == "call_navigator":
        _console.print("\n[bold cyan]⚡ Navigator agent reactivated[/bold cyan]")
        return "navigator"
    
    # Short-circuit: if a core action just completed, end the turn immediately
    if hasattr(last, "name") and last.name in ["move_to_trash", "execute_deep_clean", "run_system_optimization"]:
        return END

    return "executor"

# ---------------------------------------------------------------------------
# Construct the Unified Master Graph
# ---------------------------------------------------------------------------

master_workflow = StateGraph(AgentState)

master_workflow.add_node("navigator", navigator_node)
master_workflow.add_node("nav_tools", nav_tool_node)
master_workflow.add_node("executor", executor_node)
master_workflow.add_node("exec_tools", exec_tool_node)

master_workflow.set_entry_point("navigator")

master_workflow.add_conditional_edges("navigator", nav_should_continue)
master_workflow.add_conditional_edges("nav_tools", route_after_nav_tools)

master_workflow.add_conditional_edges("executor", exec_should_continue)
master_workflow.add_conditional_edges("exec_tools", route_after_exec_tools)

master_memory = MemorySaver()
master_app = master_workflow.compile(checkpointer=master_memory)