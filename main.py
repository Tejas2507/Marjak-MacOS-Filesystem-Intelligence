# main.py — Mārjak: Two-Agent TUI with Handoff Logic
#
# Navigator handles all exploration. Executor handles all actions.
# main.py detects whether the user is confirming a prior proposal
# and routes to the correct agent.

import sys
import re
import uuid
import time
import platform
from datetime import datetime
from langchain_core.messages import HumanMessage, AIMessage, AIMessageChunk
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

from agent import master_app, persistent_memory, get_performance_caps
from tools import session_book
from config_manager import config_manager

console = Console(highlight=False)

# ANSI escape sequence pattern — strips terminal garbage like cursor position
# reports (^[[38;1R) and bracketed paste markers (^[[200~) that leak in
# when subprocess calls corrupt the terminal state.
_ANSI_ESCAPE = re.compile(r"(\x9B|\x1B\[)[0-?]*[ -/]*[@-~]|\x1B[\x40-\x5F]|[\x00-\x08\x0E-\x1F]")

# Markdown patterns to strip from model output
_MD_BOLD_ITALIC = re.compile(r"\*{1,3}(.+?)\*{1,3}")
_MD_HEADING = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_MD_TABLE_ROW = re.compile(r"^\|.+\|$", re.MULTILINE)
_MD_TABLE_SEP = re.compile(r"^\|[-| :]+\|$", re.MULTILINE)
_MD_CODE_FENCE = re.compile(r"```[a-z]*\n?")
_MD_INLINE_CODE = re.compile(r"`([^`]+)`")
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")


def _sanitize_input(text: str) -> str:
    """Strip ANSI escapes and non-printable control characters from user input."""
    return _ANSI_ESCAPE.sub("", text).strip()


def _clean_model_output(text: str) -> str:
    """
    Transforms raw model markdown into clean terminal-friendly plain text.

    - Strips **bold**, *italic*, ***bold-italic*** markers
    - Removes markdown headings (# ## ###)
    - Strips table separator rows
    - Converts inline code backticks to plain text
    - Converts markdown links to just the label
    - Removes code fence markers
    """
    # Strip bold/italic markers, keep content
    text = _MD_BOLD_ITALIC.sub(r"\1", text)
    # Strip heading markers
    text = _MD_HEADING.sub("", text)
    # Remove table separator rows (|---|---|)
    text = _MD_TABLE_SEP.sub("", text)
    # Strip inline code backticks
    text = _MD_INLINE_CODE.sub(r"\1", text)
    # Strip code fence markers
    text = _MD_CODE_FENCE.sub("", text)
    # Strip markdown table data rows
    text = _MD_TABLE_ROW.sub("", text)
    # Convert links to label only
    text = _MD_LINK.sub(r"\1", text)
    # Collapse triple+ blank lines to double
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
    feat.append("Navigator", style="cyan")
    feat.append("  ·  ", style="dim")
    feat.append("Executor", style="cyan")
    feat.append("  ·  ", style="dim")
    feat.append("Memory", style="cyan")
    feat.append("  ·  ", style="dim")
    feat.append("AI Reasoning", style="cyan")

    # ── Command reference — two plain centered lines ---------------
    cmd1 = Text(justify="center")
    cmd1.append("/scan",       style="cyan"); cmd1.append(" waste preview", style="dim")
    cmd1.append("   ")
    cmd1.append("/deep_clean", style="cyan"); cmd1.append(" purge caches", style="dim")
    cmd1.append("   ")
    cmd1.append("/optimize",   style="cyan"); cmd1.append(" tune macOS", style="dim")
    cmd1.append("   ")
    cmd1.append("/config",     style="cyan"); cmd1.append(" setup AI", style="dim")

    cmd2 = Text(justify="center")
    cmd2.append("/playbook",   style="cyan"); cmd2.append(" filesystem map", style="dim")
    cmd2.append("   ")
    cmd2.append("/wipe",       style="cyan"); cmd2.append(" nuclear reset", style="dim")
    cmd2.append("   ")
    cmd2.append("/quit",       style="cyan"); cmd2.append(" exit", style="dim")


    # ── Session status line ─────────────────────────────────────────
    if is_fresh:
        status = Text("Fresh session — no prior filesystem data", style="dim", justify="center")
    else:
        status = Text(justify="center")
        status.append("✔ ", style="green")
        status.append(f"{node_count} filesystem nodes restored", style="dim")

    # ── Host / AI / time strip ───────────────────────────────────────────
    prov_str = f"{config_manager.current_provider.upper()} ({config_manager.current_model})"
    meta = Text(f"{hostname}   AI: {prov_str}   {now}", style="dim", justify="center")

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
        cmd1,
        cmd2,
        Text(""),
        Rule(style="dim cyan"),
        Text(""),
        status,
        meta,
        Text(""),
    )

    console.print()
    console.print(Panel(
        content,
        border_style="cyan",
        padding=(0, 4),
        expand=True,
    ))
    console.print()


def stream_agent(app, inputs, config, max_loops=15):
    """Streams an agent's logic to the console.
    Token streaming is handled by agent.py natively.
    This loop only renders tool calls and transient tool results.
    """
    from rich.live import Live
    tool_call_count = 0
    _pending_tool_name = None

    for msg_chunk, metadata in app.stream(inputs, config, stream_mode="messages"):

        if msg_chunk.type == "ai":
            if hasattr(msg_chunk, "tool_calls") and msg_chunk.tool_calls:
                for chunk in msg_chunk.tool_calls:
                    if "name" in chunk and chunk["name"]:
                        tool_call_count += 1
                        _pending_tool_name = chunk["name"]
                        console.print(
                            f"[dim cyan]▶  {chunk['name']}[/dim cyan]",
                            end="",
                        )
                    if "args" in chunk and chunk["args"]:
                        args = chunk["args"]
                        # Trim very long arg dumps
                        args_str = str(args)
                        if len(args_str) > 80:
                            args_str = args_str[:77] + "…"
                        console.print(f"  [dim]{args_str}[/dim]", end="")

        elif msg_chunk.type == "tool":
            # Flash a one-line result indicator transiently, then erase it
            result_raw = _clean_model_output(str(msg_chunk.content))
            # Collapse to first non-empty line as the summary
            summary_line = next(
                (l.strip() for l in result_raw.splitlines() if l.strip()), "✔ done"
            )
            if len(summary_line) > 90:
                summary_line = summary_line[:87] + "…"
            with Live(
                Text.from_markup(f"[dim green]✔  {summary_line}[/dim green]"),
                console=console,
                transient=True,
                refresh_per_second=4,
            ):
                import time as _t; _t.sleep(0.6)   # brief flash so user sees it
            # Newline after the tool call label
            console.print()

    console.print()

    # Check for empty response
    final_state = app.get_state(config)
    if final_state and "messages" in final_state.values:
        last_msg = final_state.values["messages"][-1]
        if isinstance(last_msg, AIMessage) and not last_msg.content and not last_msg.tool_calls:
            console.print(
                "[bold yellow]⚠  No response. Try a more specific request.[/bold yellow]\n"
            )


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

    # 4. Performance Preset
    console.print("\n[bold]Performance Presets:[/bold]")
    console.print("  - [cyan]Eco[/cyan]:    Fast, low-intensity analysis (best for quick summaries)")
    console.print("  - [cyan]Pro[/cyan]:    Balanced research and deep folder mapping (Recommended)")
    console.print("  - [cyan]Expert[/cyan]: High-intensity exhaustive search (Thorough but slower)\n")
    
    current_preset = config_manager.config.get("preset", "Pro")
    preset = Prompt.ask(
        "Select Performance Preset",
        choices=["Eco", "Pro", "Expert"],
        default=current_preset
    )

    # Save
    config_manager.set_provider(choice, model=model_name, api_key=api_key)
    config_manager.set_preset(preset)
    
    console.print(f"\n[bold green]✔ Configuration saved![/bold green]")
    console.print(f"[dim]Provider: {choice} | Model: {model_name} | Preset: {preset}[/dim]\n")


def stream_agent(app, inputs, config, max_loops=15):
    """Event loop for streaming agent output with reasoning extraction and transient tool UI."""
    from rich.live import Live
    import itertools

    spinner = itertools.cycle(["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"])
    
    with Live(Text(""), console=console, transient=True, refresh_per_second=10) as live:
        last_msg_id = None
        printed_len = 0

        for chunk in app.stream(inputs, config, stream_mode="messages"):
            msg, metadata = chunk
            
            # Avoid doubling: Only use AIMessageChunk for streaming output.
            # Completed AIMessages from nodes are ignored as they've already been streamed.
            is_chunk = isinstance(msg, AIMessageChunk)
            
            # Reset tracking if this is a new message ID
            if hasattr(msg, "id") and msg.id and msg.id != last_msg_id:
                last_msg_id = msg.id
                printed_len = 0

            # Handle tool calls (transient status)
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                tname = msg.tool_calls[0]["name"]
                live.update(Text.from_markup(f"[dim yellow]  ◌  Invoking {tname}…[/dim yellow]"))
                continue

            # Handle tool results (transient flash)
            if hasattr(msg, "type") and msg.type == "tool":
                res = str(msg.content)[:80].replace("\n", " ")
                live.update(Text.from_markup(f"[dim green]  ✔  {res}…[/dim green]"))
                time.sleep(0.1) # Brief pause to show result
                continue

            if not msg.content:
                # Still thinking? Show spinner
                excerpt = ""
                if hasattr(msg, "response_metadata") and "reasoning" in msg.response_metadata:
                    excerpt = msg.response_metadata["reasoning"][-60:].replace("\n", " ")
                frame = next(spinner)
                live.update(Text.from_markup(
                    f"[dim magenta]{frame}  Thinking …[/dim magenta]" + 
                    (f" [dim]({excerpt})[/dim]" if excerpt else "")
                ))
                continue
            
            # Content arrival: If it's a chunk, print the NEW part.
            # If it's a full AIMessage and NOT a chunk, it's redundant (skip).
            if not is_chunk and isinstance(msg, AIMessage):
                continue

            if live.is_started:
                live.stop()
            
            # Extract content (handling both deltas and accumulated strings safely)
            full_content = msg.content
            # If the provider sends deltas, printed_len will stay 0 for each chunk's content
            # If the provider sends accumulated text, printed_len will prevent duplicates
            new_content = full_content[printed_len:]
            
            if new_content:
                clean = _clean_model_output(new_content)
                if clean:
                    sys.stdout.write(clean)
                    sys.stdout.flush()
                
                if not is_chunk: 
                    # If we somehow got a full message, track its length
                    printed_len = len(full_content)
        
        # Ensure a final newline after streaming completes
        print()

def main():
    # Gather session state before printing banner
    is_fresh = len(session_book.nodes) == 0
    node_count = len(session_book.nodes)
    print_banner(node_count=node_count, is_fresh=is_fresh)

    # Shared global session for the Master App — unique ID per run for fresh slate
    config = {"configurable": {"thread_id": f"session_{uuid.uuid4().hex[:8]}"}}

    # Setup advanced UI completer for dropdown commands
    commands_meta = {
        '/quit':       'Safely shutdown and save memory',
        '/scan':       'Run a system waste scan (dry-run preview, deletes nothing)',
        '/deep_clean': 'Forcibly clear heavy system caches and app waste',
        '/optimize':   'Refresh system caches and tune macOS internals',
        '/playbook':   'Show the Virtual File System knowledge tree',
        '/wipe':       '☠  Nuclear Reset: Shred all agent memory and playbook states',
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
            stale_count = sum(1 for n in session_book.nodes.values() if n.get("stale"))
            fid_count = len(session_book.id_mapping)
            
            # Save the playbook to a file instead of flooding the terminal
            with open("VFS_PLAYBOOK.txt", "w") as f:
                f.write("Mārjak  VFS Playbook\n")
                f.write("=" * 52 + "\n")
                f.write(f"  Nodes: {len(session_book.nodes)}  |  FIDs: {fid_count}  |  Stale: {stale_count}\n")
                f.write("─" * 52 + "\n\n")
                f.write(tree)
                f.write("\n")

            print() # Spacer
            user_input = prompt_session.prompt(HTML("<b><ansicyan>❯ </ansicyan></b>"))
            stripped = _sanitize_input(user_input)

            if stripped.lower() in ("/wipe", "wipe"):
                console.print("\n[bold red]☠  WARNING: This permanently erases all Mārjak memory and VFS data.[/bold red]")
                confirm = Prompt.ask("[bold red]Type 'WIPE' to confirm[/bold red]")
                if confirm.strip().upper() == "WIPE":
                    persistent_memory.wipe()
                    session_book.wipe()
                    session_book.save()  # persist the wipe
                    config_manager.config = config_manager._default_config() # Reset config to defaults
                    config_manager.save()
                    config["configurable"]["thread_id"] = f"session_{uuid.uuid4().hex[:8]}"
                    console.print("  [dim green]✔ Mārjak memory and config erased. Fresh session started.[/dim green]\n")
                else:
                    console.print("  [dim]Wipe aborted.[/dim]\n")
                continue

            if stripped.lower() in ("/config", "config"):
                run_config_wizard()
                # Refresh session with new thread to ensure clean LLM state
                config["configurable"]["thread_id"] = f"session_{uuid.uuid4().hex[:8]}"
                continue

            if stripped.lower() in ("/playbook", "playbook"):
                stale_count = sum(1 for n in session_book.nodes.values() if n.get("stale"))
                fid_count = len(session_book.id_mapping)
                header = (
                    f"Nodes: {len(session_book.nodes)}  |  "
                    f"FIDs: {fid_count}  |  "
                    f"Stale: {stale_count}\n" + "─" * 48 + "\n\n"
                )
                console.print(Panel(
                    header + tree,
                    title="[bold cyan]Mārjak — VFS Playbook[/bold cyan]",
                    border_style="dim cyan",
                    padding=(0, 1)
                ))
                continue

            if stripped.lower() in ("/quit", "quit", "q", "exit", "/exit", "/q"):
                console.print("  [dim]Saving memory and shutting down…[/dim]")
                persistent_memory.save()
                session_book.save()
                console.print("  [dim green]✔ Done. Goodbye.[/dim green]\n")
                break

            # Direct-execute commands — bypass LLM entirely for predefined actions
            if stripped.lower() in ("/scan", "scan"):
                console.print("  [dim cyan]Running filesystem waste scan (preview only)…[/dim cyan]")
                from tools import mole_scan
                result = mole_scan.invoke({})
                console.print(f"  [dim]{_clean_model_output(str(result))}[/dim]\n")
                persistent_memory.save()
                continue

            if stripped.lower() in ("/deep_clean", "deep_clean"):
                console.print("  [dim cyan]Running deep system clean…[/dim cyan]")
                from tools import execute_deep_clean
                result = execute_deep_clean.invoke({})
                console.print(f"  [dim]{_clean_model_output(str(result))}[/dim]\n")
                persistent_memory.save()
                continue

            if stripped.lower() in ("/optimize", "optimize"):
                console.print("  [dim cyan]Running system optimization…[/dim cyan]")
                from tools import run_system_optimization
                result = run_system_optimization.invoke({})
                console.print(f"  [dim]{_clean_model_output(str(result))}[/dim]\n")
                persistent_memory.save()
                continue

            # Catch-all: block unknown slash commands before they reach the LLM
            if stripped.startswith("/"):
                known = list(commands_meta.keys())
                console.print(
                    f"  [dim yellow]Unknown command: '{stripped}'[/dim yellow]\n"
                    f"  [dim]Available: {', '.join(known)}[/dim]"
                )
                continue

            if not stripped:
                continue

            inputs = {"messages": [HumanMessage(content=stripped)]}
            
            console.print("[dim cyan]  ◌  Processing…[/dim cyan]\n")
            caps = get_performance_caps()
            total_loops = caps["nav_loops"] + caps["exec_loops"]
            stream_agent(master_app, inputs, config, max_loops=total_loops)

            # Auto-save memory periodically
            persistent_memory.save()

        except KeyboardInterrupt:
            console.print("\n  [dim]Interrupted. Type /quit to exit.[/dim]")
            continue
        except Exception as e:
            console.print(f"\n  [bold red]Error:[/bold red] {e}")


if __name__ == "__main__":
    main()


