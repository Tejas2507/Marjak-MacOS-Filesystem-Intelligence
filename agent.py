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
from config_manager import config_manager

from tools import (
    navigate,
    mole_scan,
    search_system,
    get_system_overview,
    execute_deep_clean,
    run_system_optimization,
    move_to_trash,
    call_executor,
    call_navigator,
    memory as persistent_memory,
    session_book,
)
from prompts import NAVIGATOR_PROMPT, EXECUTOR_PROMPT
import time
import os

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
        nav_tool_names = {"navigate", "mole_scan", "search_system", "get_system_overview", "call_executor"}
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
    def get_optimized_messages(messages: list[BaseMessage], agent_type: str, max_tokens: int = 120000) -> list[BaseMessage]:
        """Master entry point — prune, isolate, strip ghosts, then trim."""
        ctx = list(messages)
        ctx = ContextManager.prune_thinking(ctx)
        ctx = ContextManager.strip_ghost_messages(ctx)
        ctx = ContextManager.isolate_for_agent(ctx, agent_type)
        
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


# ---------------------------------------------------------------------------
# Thinking Indicator — smooth animated line, rate-limited to 150 ms updates
# ---------------------------------------------------------------------------

class ThinkingIndicator:
    """Single-line animated thinking indicator driven by a background thread.

    Key design decisions:
    - The background thread refreshes the display every REFRESH_MS milliseconds.
      Token arrivals only write to a buffer — they never trigger a display update.
      This prevents the jittery token-by-token flicker.
    - The indicator doesn't appear until MIN_CHARS characters have accumulated,
      so the user never sees a 1-2 word flash at the very start of reasoning.
    - transient=True on the Live erases the line completely when stop() is called.
    """
    REFRESH_MS  = 150    # display update interval in milliseconds
    MIN_CHARS   = 80     # don't show indicator until this many chars accumulated
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

    def feed(self, text: str):
        """Append reasoning text to the buffer.

        Automatically starts the display once MIN_CHARS have accumulated.
        Never touches the Live object directly — safe to call from the main thread.
        """
        if not text:
            return
        with self._lock:
            self._buffer += text
            ready = len(self._buffer) >= self.MIN_CHARS

        if ready and not self._active:
            self._active = True
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def stop(self):
        """Stop the indicator and erase the line."""
        if self._active:
            self._stop_event.set()
            if self._thread:
                self._thread.join(timeout=1.0)
            self._active  = False
            self._thread  = None
            # Reset buffer for next use
            with self._lock:
                self._buffer = ""


# ---------------------------------------------------------------------------
# Shared Streaming Helper (DRY)
# ---------------------------------------------------------------------------

def _stream_and_log(bound_llm, trimmed: list, config: RunnableConfig, agent_name: str):
    """Streams LLM response to console with reasoning frames, logs result, shows token usage."""
    start = time.time()
    response_chunk = None
    in_reasoning = False
    thinking = ThinkingIndicator()

    for chunk in bound_llm.stream(trimmed, config=config):
        if hasattr(chunk, "additional_kwargs") and "reasoning_content" in chunk.additional_kwargs:
            r = chunk.additional_kwargs["reasoning_content"]
            if r:
                if not in_reasoning:
                    in_reasoning = True
                thinking.feed(r)   # just write to buffer, never touch display directly

        if chunk.content:
            if in_reasoning:
                thinking.stop()    # erases the indicator line
                in_reasoning = False
            _console.print(chunk.content, end="", style="bright_white")

        if response_chunk is None:
            response_chunk = chunk
        else:
            response_chunk += chunk

    if in_reasoning:
        thinking.stop()

    _console.print()
    response = response_chunk
    duration = time.time() - start

    # Token count: prefer Ollama's exact prompt_eval_count from the final streaming chunk.
    prompt_tokens = 0
    if hasattr(response, "response_metadata") and response.response_metadata:
        prompt_tokens = response.response_metadata.get("prompt_eval_count", 0)
    if not prompt_tokens and hasattr(response, "usage_metadata") and response.usage_metadata:
        prompt_tokens = response.usage_metadata.get("input_tokens", 0)

    if prompt_tokens:
        MAX_CTX = 131072
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

nav_tools = [navigate, search_system, get_system_overview, call_executor]
nav_tool_node = ToolNode(nav_tools)


def navigator_node(state: AgentState, config: RunnableConfig):
    messages = list(state["messages"])
    mem_ctx = persistent_memory.get_context_for_prompt()
    book_view = session_book.render_tree()
    
    active_prov = f"{config_manager.current_provider.upper()} ({config_manager.current_model})"
    full_prompt = NAVIGATOR_PROMPT + f"\n\n[SYSTEM STATE]\nProvider: {active_prov}\n\nSession Memory:\n{mem_ctx}\n\n<vfs_playbook>\n{book_view}\n</vfs_playbook>"

    if not messages or not isinstance(messages[0], SystemMessage):
        messages = [SystemMessage(content=full_prompt)] + messages
    else:
        messages[0] = SystemMessage(content=full_prompt)

    caps = get_performance_caps()
    trimmed = ContextManager.get_optimized_messages(messages, "navigator")
    
    # Dynamic LLM binding with tools
    llm_instance = get_llm().bind_tools(nav_tools)
    response = _stream_and_log(llm_instance, trimmed, config, "navigator")
    return {"messages": [response]}


# ---------------------------------------------------------------------------
# Executor Agent — Destructive actions
# ---------------------------------------------------------------------------

exec_tools = [execute_deep_clean, run_system_optimization, move_to_trash, call_navigator]
exec_tool_node = ToolNode(exec_tools)

def executor_node(state: AgentState, config: RunnableConfig):
    messages = list(state["messages"])
    mem_ctx = persistent_memory.get_context_for_prompt()
    book_view = session_book.render_tree()
    
    active_prov = f"{config_manager.current_provider.upper()} ({config_manager.current_model})"
    full_prompt = EXECUTOR_PROMPT + f"\n\n[SYSTEM STATE]\nProvider: {active_prov}\n\nSession Memory:\n{mem_ctx}\n\n<vfs_playbook>\n{book_view}\n</vfs_playbook>"

    if not messages or not isinstance(messages[0], SystemMessage):
        messages = [SystemMessage(content=full_prompt)] + messages
    else:
        messages[0] = SystemMessage(content=full_prompt)

    caps = get_performance_caps()
    trimmed = ContextManager.get_optimized_messages(messages, "executor")
    
    # Dynamic LLM binding with tools
    llm_instance = get_llm().bind_tools(exec_tools)
    response = _stream_and_log(llm_instance, trimmed, config, "executor")
    return {"messages": [response]}


# ---------------------------------------------------------------------------
# Core Workflow Logic
# ---------------------------------------------------------------------------

def nav_should_continue(state: AgentState):
    messages = state["messages"]
    last = messages[-1]
    tool_count = sum(1 for m in messages if hasattr(m, "type") and m.type == "tool")
    caps = get_performance_caps()
    if tool_count >= caps["nav_loops"]:
        return END
    if last.tool_calls:
        return "nav_tools"
    return END

def exec_should_continue(state: AgentState):
    messages = state["messages"]
    last = messages[-1]
    tool_count = sum(1 for m in messages if hasattr(m, "type") and m.type == "tool")
    caps = get_performance_caps()
    if tool_count >= (caps["nav_loops"] + caps["exec_loops"]):
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