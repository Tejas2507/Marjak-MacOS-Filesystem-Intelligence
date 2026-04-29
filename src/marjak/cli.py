# cli.py — Mārjak v2: Single-Agent TUI
#
# One unified agent handles exploration, analysis, and cleanup.
# Safety gates are in the tools themselves (Prompt.ask confirmation).

import sys
import re
import uuid
import time
import platform
from datetime import datetime
from langchain_core.messages import HumanMessage, AIMessage
from rich.console import Console, Group
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text
from rich.rule import Rule
from rich.align import Align
from rich.padding import Padding
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.formatted_text import HTML

from marjak.agent import master_app, persistent_memory, get_performance_caps, init_session_logging, try_quick_mode
from marjak.tools import session_book
from marjak.config import config_manager
from marjak.fs_memory import fs_memory

console = Console(highlight=False)

# ANSI escape sequence pattern — strips terminal garbage like cursor position
# reports (^[[38;1R) and bracketed paste markers (^[[200~) that leak in
# when subprocess calls corrupt the terminal state.
_ANSI_ESCAPE = re.compile(r"(\x9B|\x1B\[)[0-?]*[ -/]*[@-~]|\x1B[\x40-\x5F]|[\x00-\x08\x0E-\x1F]")

# Markdown patterns to strip from model output
_FID_PATTERN = re.compile(r"\[FID:\s*\d+\]")


def _sanitize_input(text: str) -> str:
    """Strip ANSI escapes and non-printable control characters from user input."""
    return _ANSI_ESCAPE.sub("", text).strip()


def _clean_model_output(text: str) -> str:
    """
    Light cleanup for tool results displayed directly (not LLM streaming).
    Strips leaked FID references and collapses excessive blank lines.
    """
    text = _FID_PATTERN.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def print_banner(node_count: int = 0, is_fresh: bool = False):
    """Single-panel premium startup banner."""
    hostname = platform.node().replace(".local", "")
    now = datetime.now().strftime("%d %b %Y  %H:%M")

    # ── Title ──────────────────────────────────────────────────────
    title = Text(justify="center")
    title.append("\n")
    title.append("◆  ", style="bold cyan")
    title.append("Mārjak", style="bold bright_cyan")
    title.append("  ◆", style="bold cyan")

    tagline = Text("macOS Filesystem Intelligence", style="dim cyan", justify="center")

    # ── Feature row ────────────────────────────────────────────────
    feat = Text(justify="center")
    feat.append("Explorer", style="cyan")
    feat.append("  ·  ", style="dim")
    feat.append("Cleaner", style="cyan")
    feat.append("  ·  ", style="dim")
    feat.append("Memory", style="cyan")
    feat.append("  ·  ", style="dim")
    feat.append("AI Reasoning", style="cyan")

    # ── Command grid — 4 columns per row, no wrapping ─────────────────
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="cyan", no_wrap=True)
    grid.add_column(style="dim",  no_wrap=True)
    grid.add_column(style="cyan", no_wrap=True)
    grid.add_column(style="dim",  no_wrap=True)
    grid.add_column(style="cyan", no_wrap=True)
    grid.add_column(style="dim",  no_wrap=True)
    grid.add_column(style="cyan", no_wrap=True)
    grid.add_column(style="dim",  no_wrap=True)

    grid.add_row("/scan", "waste preview", "/deep_clean", "purge caches", "/optimize", "tune macOS", "/config", "setup AI")
    grid.add_row("/playbook", "session tree", "/memory", "persistent DB", "/wipe", "reset session", "/quit", "exit")

    # ── Session status line ─────────────────────────────────────────
    status = Text(justify="center")
    if is_fresh:
        status.append("Fresh session — system mapping complete", style="dim")
    else:
        status.append("✔ ", style="green")
        status.append(f"Memory: {node_count} directories tracked", style="dim")

    # ── Host / AI / time strip ───────────────────────────────────────────
    from marjak import __version__
    prov_str = f"{config_manager.current_provider.upper()} ({config_manager.current_model})"
    meta = Text(f"{hostname}   v{__version__}   AI: {prov_str}   {now}", style="dim", justify="center")

    # ── Compose everything into one panel ───────────────────────────
    content = Group(
        title,
        Text(""),
        tagline,
        Text(""),
        feat,
        Text(""),
        Rule(style="dim cyan"),
        Text(""),
        Align.center(grid),
        Text(""),
        Rule(style="dim cyan"),
        Text(""),
        status,
        meta,
        Text(""),
    )

    console.print()
    # Centering a fixed-width panel to "extend" it horizontally
    console.print(Align.center(Panel(
        content,
        border_style="cyan",
        width=104,
        padding=(0, 4),
    )))
    console.print()


def _recommend_preset(provider: str, model: str) -> str:
    """Suggest a preset based on the provider and model name."""
    model_lower = model.lower()

    # Cloud providers with large context windows → default higher
    if provider in ("claude", "openai"):
        if any(k in model_lower for k in ("haiku", "mini", "nano", "flash")):
            return "Pro"
        return "Expert"
    if provider == "gemini":
        if "flash" in model_lower:
            return "Pro"
        return "Expert"
    if provider == "groq":
        # Groq is fast inference but often smaller effective context
        return "Eco"
    if provider == "openrouter":
        return "Pro"

    # Ollama / local: infer from model size in name
    import re as _re
    match = _re.search(r"(\d+)[bB]", model_lower)
    if match:
        param_b = int(match.group(1))
        if param_b <= 12:
            return "Eco"
        elif param_b <= 35:
            return "Pro"
        else:
            return "Expert"
    # Fallback
    return "Pro"


def run_config_wizard():
    """Interactive CLI wizard to configure providers, keys, and models."""
    console.print("\n[bold cyan]⚒  Mārjak Configuration Wizard[/bold cyan]\n")

    # 1. Provider Selection
    providers = ["ollama", "gemini", "openai", "claude", "groq", "openrouter"]
    current = config_manager.current_provider
    console.print(f"Current Provider: [bold cyan]{current}[/bold cyan]")
    
    choice = Prompt.ask(
        "Select AI Provider",
        choices=providers,
        default=current
    )

    # 2. API Key (if not ollama)
    api_key = ""
    if choice != "ollama":
        existing_key = config_manager.api_keys.get(choice, "")
        key_masked = f"{existing_key[:4]}...{existing_key[-4:]}" if len(existing_key) > 8 else "None"
        console.print(f"Existing Key: [dim]{key_masked}[/dim]")
        api_key = Prompt.ask(f"Enter {choice.upper()} API Key", password=True, default=existing_key)

    # 3. Model Name (Manual Entry)
    current_model = config_manager.config["providers"].get(choice, {}).get("model", "default")
    model_name = Prompt.ask(
        f"Enter Model Name for {choice.upper()} (e.g. gemma4, gpt-4o, claude-3-5-sonnet-20240620)", 
        default=current_model
    )

    # 4. Performance Preset with model-aware recommendations
    console.print("\n[bold]Performance Presets:[/bold]")
    console.print("  - [cyan]Eco[/cyan]:    Small/fast models (\u226412B params), quick summaries, minimal context")
    console.print("  - [cyan]Pro[/cyan]:    Mid-range models (12B\u201370B), balanced depth and speed")
    console.print("  - [cyan]Expert[/cyan]: Large models (\u226570B) or cloud APIs, exhaustive analysis\n")

    # Auto-recommend preset based on provider and model
    recommended = _recommend_preset(choice, model_name)
    console.print(f"  [dim]Recommended for {choice}/{model_name}: [bold cyan]{recommended}[/bold cyan][/dim]")
    
    current_preset = config_manager.config.get("preset", "Pro")
    preset = Prompt.ask(
        "Select Performance Preset",
        choices=["Eco", "Pro", "Expert"],
        default=recommended
    )

    # Save
    config_manager.set_provider(choice, model=model_name, api_key=api_key)
    config_manager.set_preset(preset)
    
    console.print(f"\n[bold green]✔ Configuration saved![/bold green]")
    console.print(f"[dim]Provider: {choice} | Model: {model_name} | Preset: {preset}[/dim]\n")


def stream_agent(app, inputs, config, max_loops=15):
    """Event loop for agent execution.

    Token streaming is handled by agent.py's _stream_and_log natively.
    This loop only renders tool-call invocations and transient tool results;
    it never prints AI content (that would double-print).
    """
    from rich.live import Live

    for msg_chunk, metadata in app.stream(inputs, config, stream_mode="messages"):

        # ── Tool-call invocations ─────────────────────────────────
        if hasattr(msg_chunk, "tool_calls") and msg_chunk.tool_calls:
            for tc in msg_chunk.tool_calls:
                name = tc.get("name")
                if name:
                    args_str = str(tc.get("args", ""))
                    if len(args_str) > 80:
                        args_str = args_str[:77] + "…"
                    console.print(
                        f"  [dim cyan]▶  {name}[/dim cyan]"
                        + (f"  [dim]{args_str}[/dim]" if args_str else "")
                    )
            continue

        # ── Tool results (transient flash) ────────────────────────
        if hasattr(msg_chunk, "type") and msg_chunk.type == "tool":
            result_raw = _clean_model_output(str(msg_chunk.content))
            summary = next(
                (l.strip() for l in result_raw.splitlines() if l.strip()), "done"
            )
            if len(summary) > 90:
                summary = summary[:87] + "…"
            with Live(
                Text.from_markup(f"  [dim green]✔  {summary}[/dim green]"),
                console=console,
                transient=True,
                refresh_per_second=4,
            ):
                time.sleep(0.35)
            continue

        # ── AI content: skip (already printed by _stream_and_log) ─
        # Nothing to do here — agent.py streams tokens to stdout.

    # Check for empty response — synthesize from tool results as fallback
    final_state = app.get_state(config)
    if final_state and "messages" in final_state.values:
        last_msg = final_state.values["messages"][-1]
        if isinstance(last_msg, AIMessage) and not last_msg.content and not last_msg.tool_calls:
            # Try to synthesize useful info from tool results
            tool_summaries = []
            for m in reversed(final_state.values["messages"]):
                if isinstance(m, AIMessage) and m.content:
                    break  # Stop at last real AI response
                if hasattr(m, "type") and m.type == "tool" and m.content:
                    content = str(m.content)
                    first_line = content.split("\n", 1)[0].strip()
                    if first_line and first_line != "[VFS up to date]":
                        tool_summaries.append(first_line)
            if tool_summaries:
                console.print("[bold yellow]Here's what I found before running out of turns:[/bold yellow]")
                for s in tool_summaries[:5]:
                    if len(s) > 120:
                        s = s[:117] + "…"
                    console.print(f"  [dim]• {s}[/dim]")
                console.print("[dim]Try a more specific follow-up to continue.[/dim]")
            else:
                console.print(
                    "[bold yellow]⚠  No response. Try a more specific request.[/bold yellow]"
                )
    console.print()

def _show_persistent_memory():
    """Display everything stored in the persistent SQLite database."""
    import os as _os
    from marjak.session_book import _human_size
    from marjak import __version__

    home = _os.path.expanduser("~")

    def _rel(path):
        try:
            return "~/" + _os.path.relpath(path, home)
        except (ValueError, TypeError):
            return path

    try:
        # 1. Skeleton (system map from first-run scan)
        skeleton = fs_memory.get_skeleton()
        if skeleton:
            table = Table(title="System Map (first-run scan)", border_style="dim cyan", show_lines=False)
            table.add_column("Path", style="cyan", no_wrap=True)
            table.add_column("Size", justify="right")
            table.add_column("Items", justify="right")
            table.add_column("Scanned", style="dim")
            for s in skeleton[:40]:
                table.add_row(
                    _rel(s["path"]),
                    _human_size(s["size_bytes"]),
                    str(s.get("item_count", "")),
                    fs_memory._age_str(s.get("scanned_at")),
                )
            console.print(table)
            console.print()

        # 2. Hotspots (explored directories by size)
        hotspots = fs_memory.get_hotspots(30)
        if hotspots:
            table = Table(title="Explored Hotspots (by size)", border_style="dim yellow", show_lines=False)
            table.add_column("Path", style="yellow", no_wrap=True)
            table.add_column("Size", justify="right")
            table.add_column("Visits", justify="right")
            table.add_column("Last Seen", style="dim")
            for h in hotspots:
                table.add_row(
                    _rel(h["path"]),
                    _human_size(h["size_bytes"]),
                    str(h.get("times_visited", 1)),
                    fs_memory._age_str(h.get("scanned_at")),
                )
            console.print(table)
            console.print()

        # 3. Recent actions
        actions = fs_memory.get_recent_actions(20)
        if actions:
            table = Table(title="Recent Actions", border_style="dim green", show_lines=False)
            table.add_column("When", style="dim")
            table.add_column("Action", style="green")
            table.add_column("Detail")
            for a in actions:
                table.add_row(
                    fs_memory._age_str(a.get("timestamp")),
                    a["action"],
                    (a.get("detail") or "")[:80],
                )
            console.print(table)
            console.print()

        # 4. DB stats
        dir_count = fs_memory._conn.execute("SELECT COUNT(*) FROM directories").fetchone()[0]
        search_count = fs_memory._conn.execute("SELECT COUNT(*) FROM search_hits").fetchone()[0]
        action_count = fs_memory._conn.execute("SELECT COUNT(*) FROM actions_log").fetchone()[0]
        console.print(
            f"  [dim]DB: {dir_count} directories, {search_count} search hits, "
            f"{action_count} actions | {fs_memory._db_path}[/dim]\n"
        )

        if dir_count == 0:
            console.print("  [dim yellow]Persistent memory is empty. Start exploring to build it up.[/dim yellow]\n")

    except Exception as exc:
        console.print(f"  [bold red]⚠ Memory error:[/bold red] [red]{exc}[/red]")
        console.print(f"  [dim]DB path: {fs_memory._db_path} | v{__version__}[/dim]")
        console.print(f"  [dim yellow]Try: marjak --version  or  rm ~/.marjak/fs_memory.db  to reset[/dim yellow]\n")


def main():
    # --version flag
    if len(sys.argv) > 1 and sys.argv[1] in ("--version", "-v", "version"):
        from marjak import __version__
        print(f"marjak v{__version__}")
        return
    
    
    # First-run system mapping — silent scan of ~ and ~/Library
    if fs_memory.is_empty():
        console.print("[dim cyan]Mapping your system for the first time…[/dim cyan]")
        fs_memory.run_first_scan()
        dir_count = fs_memory._conn.execute("SELECT COUNT(*) FROM directories").fetchone()[0]
        console.print(f"[dim green]✔ Mapped {dir_count} directories.[/dim green]\n")

    # Gather session state before printing banner
    dir_count = fs_memory._conn.execute("SELECT COUNT(*) FROM directories").fetchone()[0]
    is_fresh = dir_count == 0
    print_banner(node_count=dir_count, is_fresh=is_fresh)

    # Initialize per-session runlog folder
    init_session_logging()

    # Shared global session for the Master App — unique ID per run for fresh slate
    config = {"configurable": {"thread_id": f"session_{uuid.uuid4().hex[:8]}"}}

    # Setup advanced UI completer for dropdown commands
    commands_meta = {
        '/quit':       'Safely shutdown',
        '/scan':       'Run a system waste scan (dry-run preview, deletes nothing)',
        '/deep_clean': 'Forcibly clear heavy system caches and app waste',
        '/optimize':   'Refresh system caches and tune macOS internals',
        '/playbook':   'Show the session exploration tree',
        '/memory':     'Show what Mārjak remembers across sessions (persistent DB)',
        '/wipe':       'Clear current session (persistent memory retained)',
        '/wipe --all': '☠  Nuclear Reset: Shred all agent memory',
        '/forget':     'Remove a specific path from persistent memory',
        '/config':     '⚒  Setup: Change AI providers, API keys, and models'
    }
    command_completer = WordCompleter(
        list(commands_meta.keys()), 
        meta_dict=commands_meta,
        ignore_case=True,
        sentence=True
    )
    prompt_session = PromptSession(completer=command_completer)

    while True:
        try:
            tree = session_book.render_tree()
            
            # Stats for the playbook header
            fid_count = len(session_book.id_mapping)
            
            # Save the playbook to a file instead of flooding the terminal
            with open("VFS_PLAYBOOK.txt", "w") as f:
                f.write("Mārjak  Session Playbook\n")
                f.write("=" * 52 + "\n")
                f.write(f"  Nodes: {len(session_book.nodes)}  |  FIDs: {fid_count}\n")
                f.write("─" * 52 + "\n\n")
                f.write(tree)
                f.write("\n")

            print() # Spacer
            user_input = prompt_session.prompt(HTML("<b><ansicyan>❯ </ansicyan></b>"))
            stripped = _sanitize_input(user_input)

            # /wipe --all MUST be checked BEFORE /wipe (prefix match)
            if stripped.lower() in ("/wipe --all", "wipe --all"):
                console.print("\n[bold red]☠  WARNING: This erases ALL Mārjak data (session + persistent memory).[/bold red]")
                confirm = Prompt.ask("[bold red]Type 'WIPE' to confirm[/bold red]")
                if confirm.strip().upper() == "WIPE":
                    session_book.wipe()
                    fs_memory.wipe()
                    persistent_memory.wipe()
                    config["configurable"]["thread_id"] = f"session_{uuid.uuid4().hex[:8]}"
                    console.print("  [dim green]✔ All memory erased. Fresh session started.[/dim green]\n")
                else:
                    console.print("  [dim]Wipe aborted.[/dim]\n")
                continue

            if stripped.lower() in ("/wipe", "wipe"):
                console.print("\n[bold yellow]⚠  This clears the current session.[/bold yellow]")
                confirm = Prompt.ask("[bold yellow]Confirm? (y/n)[/bold yellow]", choices=["y", "n"], default="n")
                if confirm.strip().lower() == "y":
                    session_book.wipe()
                    config["configurable"]["thread_id"] = f"session_{uuid.uuid4().hex[:8]}"
                    console.print("  [dim green]✔ Session cleared. Persistent memory retained.[/dim green]\n")
                else:
                    console.print("  [dim]Wipe aborted.[/dim]\n")
                continue

            if stripped.lower().startswith("/forget "):
                forget_path = stripped[8:].strip()
                if forget_path:
                    fs_memory.delete_path(forget_path)
                    console.print(f"  [dim green]✔ Removed {forget_path} from persistent memory.[/dim green]\n")
                else:
                    console.print("  [dim]Usage: /forget <path>[/dim]\n")
                continue

            if stripped.lower() in ("/config", "config"):
                run_config_wizard()
                # Refresh session with new thread to ensure clean LLM state
                config["configurable"]["thread_id"] = f"session_{uuid.uuid4().hex[:8]}"
                continue

            if stripped.lower() in ("/playbook", "playbook"):
                fid_count = len(session_book.id_mapping)
                if not session_book.nodes:
                    console.print(Panel(
                        "No directories explored yet this session.\n"
                        "Start chatting to build the playbook, or use /memory to see persistent data.",
                        title="[bold cyan]Mārjak — Session Playbook[/bold cyan]",
                        border_style="dim cyan",
                        padding=(0, 1)
                    ))
                else:
                    header = (
                        f"Session Nodes: {len(session_book.nodes)}  |  "
                        f"FIDs: {fid_count}\n" + "─" * 48 + "\n\n"
                    )
                    console.print(Panel(
                        header + tree,
                        title="[bold cyan]Mārjak — Session Playbook[/bold cyan]",
                        border_style="dim cyan",
                        padding=(0, 1)
                    ))
                continue

            if stripped.lower() in ("/memory", "memory"):
                _show_persistent_memory()
                continue

            if stripped.lower() in ("/quit", "quit", "q", "exit", "/exit", "/q"):
                console.print("  [dim green]✔ Goodbye.[/dim green]\n")
                break

            # Direct-execute commands — bypass LLM entirely for predefined actions
            if stripped.lower() in ("/scan", "scan"):
                console.print("  [dim cyan]Running filesystem waste scan (preview only)…[/dim cyan]")
                from marjak.tools import mole_scan
                result = mole_scan.invoke({})
                console.print(f"  [dim]{_clean_model_output(str(result))}[/dim]\n")
                continue

            if stripped.lower() in ("/deep_clean", "deep_clean"):
                console.print("  [dim cyan]Running deep system clean…[/dim cyan]")
                from marjak.tools import execute_deep_clean
                result = execute_deep_clean.invoke({})
                console.print(f"  [dim]{_clean_model_output(str(result))}[/dim]\n")
                continue

            if stripped.lower() in ("/optimize", "optimize"):
                console.print("  [dim cyan]Running system optimization…[/dim cyan]")
                from marjak.tools import run_system_optimization
                result = run_system_optimization.invoke({})
                console.print(f"  [dim]{_clean_model_output(str(result))}[/dim]\n")
                continue

            # Catch-all: block unknown slash commands before they reach the LLM
            if stripped.startswith("/"):
                from marjak import __version__
                known = list(commands_meta.keys())
                console.print(
                    f"  [dim yellow]Unknown command: '{stripped}'[/dim yellow]\n"
                    f"  [dim]Available: {', '.join(known)}[/dim]\n"
                    f"  [dim]v{__version__} — if /memory is missing, reinstall: uv pip install -e .[/dim]"
                )
                continue

            if not stripped:
                continue

            # Quick mode: bypass LLM for trivial single-tool queries
            quick = try_quick_mode(stripped)
            if quick is not None:
                from rich.markdown import Markdown as RichMarkdown
                console.print(RichMarkdown(quick))
                continue

            inputs = {"messages": [HumanMessage(content=stripped)]}
            
            console.print("[dim cyan]  ◌  Processing…[/dim cyan]\n")
            caps = get_performance_caps()
            total_loops = caps["nav_loops"] + caps["exec_loops"]
            stream_agent(master_app, inputs, config, max_loops=total_loops)

        except KeyboardInterrupt:
            console.print("\n  [dim]Interrupted. Type /quit to exit.[/dim]")
            continue
        except Exception as e:
            console.print(f"\n  [bold red]Error:[/bold red] {e}")


if __name__ == "__main__":
    main()


