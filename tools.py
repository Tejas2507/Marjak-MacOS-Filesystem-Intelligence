# tools.py — Mole-Agent v3: LLM Navigates, Python Protects
#
# 7 flexible tools split across two agents:
#   Navigator: navigate, mole_scan, search_system, get_system_overview
#   Executor:  execute_deep_clean, run_system_optimization, move_to_trash

import subprocess
import json
import os
import sys
import time
import shutil
from datetime import datetime
from langchain_core.tools import tool
from rich.console import Console
from rich.prompt import Prompt

from knowledge_book import SessionBook, _human_size

console = Console(highlight=False)

# ---------------------------------------------------------------------------
# Global State 
# ---------------------------------------------------------------------------

session_book = SessionBook()
session_book.load()  # Restore VFS from previous session (validates stale paths automatically)

# ---------------------------------------------------------------------------
# Persistent Memory — Survives across sessions
# ---------------------------------------------------------------------------

class PersistentMemory:
    """Learns the user's system over time. Stored at ~/.mole-agent/memory.json.
    
    Stores:
    - system_profile: known hotspots with sizes and scan dates
    - session_history: last 15 actions (sliding window)
    - user_preferences: learned safe/dangerous paths
    
    Injected into the system prompt as ~150 tokens of compact context.
    """
    PATH = os.path.expanduser("~/.marjak/memory.json")

    def __init__(self):
        self.data = self._load()

    def _load(self) -> dict:
        if os.path.exists(self.PATH):
            try:
                with open(self.PATH) as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                return self._default()
        return self._default()

    @staticmethod
    def _default() -> dict:
        return {
            "system_profile": {"hotspots": []},
            "session_history": [],
            "user_preferences": {"safe_to_ignore": [], "always_flag": []},
        }

    def save(self):
        """Saves current state to JSON."""
        try:
            os.makedirs(os.path.dirname(self.PATH), exist_ok=True)
            with open(self.PATH, "w") as f:
                json.dump(self.data, f, indent=4)
        except IOError:
            pass

    def wipe(self):
        """Wipes all persistent memory explicitly."""
        self.data = self._default()
        self.save()

    def record_scan(self, path: str, size_gb: float):
        """Called by navigate() after each successful scan."""
        hotspots = self.data["system_profile"].setdefault("hotspots", [])
        today = datetime.now().isoformat()[:10]
        for h in hotspots:
            if h["path"] == path:
                h["size_gb"] = size_gb
                h["scanned_at"] = today
                break
        else:
            hotspots.append({"path": path, "size_gb": size_gb, "scanned_at": today})
        hotspots.sort(key=lambda x: x["size_gb"], reverse=True)
        self.data["system_profile"]["hotspots"] = hotspots[:20]

    def record_action(self, action: str, result: str):
        """Called after each tool execution."""
        history = self.data.setdefault("session_history", [])
        history.append({
            "date": datetime.now().isoformat()[:10],
            "action": action,
            "finding": result[:120],
        })
        self.data["session_history"] = history[-15:]

    def get_context_for_prompt(self) -> str:
        """Generates compact context for the system prompt (~150 tokens)."""
        parts = []

        profile = self.data.get("system_profile", {})
        hotspots = profile.get("hotspots", [])[:10]  # Expanded to 10
        if hotspots:
            items = ", ".join(
                f"{os.path.basename(h['path'])} ({h['size_gb']}GB)"
                for h in hotspots
            )
            parts.append(f"KNOWN HOTSPOTS (Long-term): {items}")

        history = self.data.get("session_history", [])[-10:]  # Expanded to 10
        if history:
            items = "; ".join(
                f"[{h['date']}] {h['action']}: {h['finding'][:60]}" for h in history
            )
            parts.append(f"RECENT ACTIVITY (Agent Memory): {items}")

        prefs = self.data.get("user_preferences", {})
        ignores = prefs.get("safe_to_ignore", [])
        if ignores:
            parts.append(f"User ignores: {', '.join(ignores)}")

        return "\n".join(parts) if parts else "First session — no prior data."


# Global instance
memory = PersistentMemory()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_mole_path() -> str:
    """Finds the Mole binary. Checks PATH first, then common Homebrew location."""
    found = shutil.which("mo")
    if found:
        return found
    homebrew_path = "/opt/homebrew/bin/mo"
    if os.path.isfile(homebrew_path):
        return homebrew_path
    return "mo"  # Fallback — let it fail with a clear error


def stream_command(cmd: list[str], task_name: str) -> str:
    """Streams subprocess output to the Rich console in real-time."""
    console.print(f"\n[bold red]⚙ Mārjak: {task_name}[/bold red]")
    try:
        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, text=True, bufsize=1
        )
        output_lines = []
        for line in process.stdout:
            clean = line.strip()
            if clean:
                console.print(f"  [dim red]{clean}[/dim red]")
                output_lines.append(clean)
        process.wait()

        tail = " ".join(output_lines[-5:]) if output_lines else "Done"
        if process.returncode != 0:
            return f"Task '{task_name}' failed (code {process.returncode}). Last output: {tail}"
        return f"Task '{task_name}' completed successfully. Summary: {tail}"
    except Exception as e:
        return f"Error running {task_name}: {e}"


# ===========================================================================
# NAVIGATOR TOOLS — Read-only exploration
# ===========================================================================

@tool
def navigate(path: str) -> str:
    """Explore any directory on the system to see its contents sorted by size.
    Use this to drill down into directories level by level.

    Start from '~' (home) for the big picture, then go deeper into interesting
    directories based on what you see.

    Args:
        path: The directory path to explore (e.g. '~', '~/Library', '~/Library/Caches').
    """
    mo = _get_mole_path()
    expanded = os.path.expanduser(path)

    if not os.path.exists(expanded):
        return f"Error: Path '{expanded}' does not exist."
    if not os.path.isdir(expanded):
        return f"Error: '{expanded}' is a file, not a directory. Navigate to its parent instead."

    console.print(f"\n[bold red]🔍 Navigating: {expanded}[/bold red]")
    try:
        result = subprocess.run(
            [mo, "analyze", "--json", expanded],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            return f"Error: Mole analysis failed for '{expanded}': {result.stderr[:200]}"

        data = json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        return f"Error: Scan of '{expanded}' timed out (>30s). Try a more specific subdirectory."
    except json.JSONDecodeError:
        return f"Error: Could not parse Mole output for '{expanded}'."
    except Exception as e:
        return f"Error exploring '{expanded}': {e}"

    entries = data.get("entries", [])
    large_files = data.get("large_files", [])
    total_size = data.get("total_size", 0)
    total_files = data.get("total_files", 0)

    # Add the structural data to the SessionBook representing exactly what was found.
    session_book.add_directory(expanded, total_size, entries, large_files)

    # Record in persistent memory
    memory.record_scan(expanded, round(total_size / 1024 ** 3, 2))
    memory.record_action("navigate", f"{path}: {_human_size(total_size)}, {len(entries)} items")
    session_book.save()  # Persist VFS after each navigation

    console.print(f"[bold red]✔ Explored {path}[/bold red]\n")
    return f"Explored {path}. Added {len(entries)} child items and {len(large_files)} large files to the Knowledge Tree. Please review the updated tree."


@tool
def mole_scan() -> str:
    """Runs Mole's system cleanup scanner to show what categories of waste exist
    and how much space can be automatically reclaimed by Mole.

    This covers: system caches, browser data, dev tool caches, app leftovers,
    orphaned services, project artifacts, etc.

    discover things Mole doesn't cover (like .ollama models, large videos, etc).
    """
    mo = _get_mole_path()
    console.print("\n[bold red]🔍 Running Mole waste scan (preview only, nothing deleted)...[/bold red]")

    all_output = []
    cats_found = 0
    needs_priv = False

    # Pass 1: Try without privileges with real-time streaming
    try:
        process = subprocess.Popen(
            [mo, "clean", "--dry-run"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, text=True, bufsize=1
        )
        
        for line in process.stdout:
            stripped = line.strip()
            if not stripped:
                continue
            
            # Detect if it's asking for a password or permissions
            if "assword" in stripped or "Permission denied" in stripped:
                needs_priv = True
                process.terminate()
                break
                
            # Stream to console immediately
            console.print(f"  [dim]{stripped}[/dim]")
            all_output.append(stripped)
            
            # Parse for Knowledge Tree updates
            if any(sym in stripped for sym in ["◎", "☞", "✓"]):
                if "|" in stripped or ":" in stripped:
                    cats_found += 1
                    session_book.add_scan_result(stripped, "Auto-Cleanable")

        process.wait()
        # If the process finished quickly with no output, it might have failed silently
        if not all_output and process.returncode != 0 and not needs_priv:
            needs_priv = True

    except Exception:
        needs_priv = True

    # Pass 2: Escalate via macOS native password dialog (osascript)
    if needs_priv:
        console.print("[dim yellow]ℹ Mole needs admin access — a macOS password dialog will appear.[/dim yellow]")
        try:
            osa_result = subprocess.run(
                ["osascript", "-e",
                 f'do shell script "{mo} clean --dry-run" with administrator privileges'],
                capture_output=True, text=True, timeout=120
            )
            output = (osa_result.stdout or "") + (osa_result.stderr or "")
            
            # Process osascript output (non-streaming but immediate after dialog)
            for line in output.strip().split("\n"):
                stripped = line.strip()
                if not stripped: continue
                console.print(f"  [dim]{stripped}[/dim]")
                all_output.append(stripped)
                if any(sym in stripped for sym in ["◎", "☞", "✓"]):
                    if "|" in stripped or ":" in stripped:
                        cats_found += 1
                        session_book.add_scan_result(stripped, "Auto-Cleanable")
                        
        except subprocess.TimeoutExpired:
            return "Password dialog timed out or was dismissed. Run /scan again to retry."
        except Exception as e:
            return f"Mole scan (privileged) failed: {e}"

    if not all_output:
        return "Mole scan returned no output. Try exploring manually with navigate()."

    memory.record_action("mole_scan", f"Previewed {cats_found} cleanable categories")
    console.print("[bold red]✔ Mole scan complete (no changes made).[/bold red]\n")
    return f"Mole scan preview complete. Added {cats_found} auto-cleanable categories to the Knowledge Tree."


@tool
def search_system(name: str, file_type: str = "any") -> str:
    """Search for files or directories matching a name anywhere in the user's
    home directory and Library. Use this when the user mentions a specific app
    or item by name but doesn't know the path.

    Args:
        name: Name to search for (e.g. 'docker', 'xcode', 'cache', 'brave')
        file_type: 'file', 'directory', or 'any'
    """
    console.print(f"\n[bold red]🔎 Searching for '{name}' ({file_type})...[/bold red]")
    home = os.path.expanduser("~")
    results = []
    
    # Source 0: Photographic Memory Check (O(1) accuracy for known playbooks)
    lower_query = name.lower()
    for p, node in session_book.nodes.items():
        if lower_query in os.path.basename(p).lower():
            if file_type == "directory" and node.get("type") != "DIR": continue
            if file_type == "file" and node.get("type") not in ("FILE", "ROOT"): continue
            if p not in results:
                results.append(p)
    
    # Replace spaces with wildcards to catch dotted/underscored filenames (e.g. 'munna bhai' -> '*munna*bhai*')
    fuzzy_name = name.replace(" ", "*")
    if not fuzzy_name.startswith("*"): fuzzy_name = f"*{fuzzy_name}"
    if not fuzzy_name.endswith("*"): fuzzy_name = f"{fuzzy_name}*"

    # Source 1: find in ~/Library (where hidden system files live)
    type_flag = []
    if file_type == "directory":
        type_flag = ["-type", "d"]
    elif file_type == "file":
        type_flag = ["-type", "f"]

    try:
        cmd = ["find", os.path.join(home, "Library"), "-maxdepth", "4",
               "-iname", fuzzy_name] + type_flag
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        for p in result.stdout.strip().split("\n"):
            if p:
                results.append(p)
    except Exception:
        pass

    # Source 2: find universally across Home folder (depth=5), pruning hidden massive traps and Library
    try:
        cmd = [
            "find", home, "-maxdepth", "5", 
            "-type", "d", "(", "-name", ".*", "-o", "-name", "Library", ")", "-prune", 
            "-o", "-iname", fuzzy_name
        ]
        if type_flag:
            cmd.extend(type_flag)
        cmd.append("-print")
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        for p in result.stdout.strip().split("\n"):
            if p and p not in results:
                results.append(p)
    except Exception:
        pass

    # Source 3: mdfind (Spotlight) for broader coverage
    try:
        mdfind_query = f"kMDItemDisplayName == '{fuzzy_name}'cd"
        result = subprocess.run(
            ["mdfind", "-onlyin", home, mdfind_query],
            capture_output=True, text=True, timeout=10
        )
        for p in result.stdout.strip().split("\n"):
            if p and p not in results:
                results.append(p)
    except Exception:
        pass

    # Deduplicate & enrich with sizes
    seen = set()
    enriched = []
    for path in results:
        if path in seen:
            continue
        seen.add(path)
        try:
            stat = os.stat(path)
            is_dir = os.path.isdir(path)
            if is_dir:
                # Use du for directory sizes (quick, 3s timeout)
                try:
                    du = subprocess.run(
                        ["du", "-sm", path], capture_output=True, text=True, timeout=3
                    )
                    size_mb = int(du.stdout.split()[0])
                    size_str = f"{size_mb} MB"
                except Exception:
                    size_str = "? MB"
            else:
                size_str = _human_size(stat.st_size)

            days_old = round((time.time() - stat.st_atime) / 86400)
            kind = "DIR " if is_dir else "FILE"
            enriched.append(f"  {kind} {size_str:>10s} | {days_old}d stale | {path}")
            
            # Feed to session book
            node_type = "FILE" if not is_dir else "DIR"
            fid = session_book.assign_fid(path) if node_type == "FILE" else None
            
            node_data = {
                "name": f"[SEARCH] {os.path.basename(path)}",
                "path": path,
                "size_str": size_str,
                "type": node_type,
                "children": []
            }
            if fid:
                node_data["fid"] = fid
                
            session_book.nodes[path] = node_data
            
            if "Search Results" not in session_book.nodes:
                session_book.nodes["Search Results"] = {
                    "name": "Global Search Results",
                    "path": "Search Results",
                    "size": 0,
                    "type": "ROOT",
                    "children": []
                }
            if path not in session_book.nodes["Search Results"]["children"]:
                session_book.nodes["Search Results"]["children"].append(path)
                
        except Exception:
            pass

    if not enriched:
        report = f"No results found for '{name}'."
    else:
        report = f"Found {len(enriched)} results for '{name}'. They have been added to the Knowledge Tree under 'Search Results'."

    memory.record_action("search_system", f"'{name}': {len(enriched)} results")
    console.print(f"[bold red]✔ Search complete.[/bold red]\n")
    return report


@tool
def get_system_overview() -> str:
    """Get a comprehensive system health overview: CPU, memory, disk, health score,
    and recent Mole activity. Use this to check system status before heavy operations.
    """
    mo = _get_mole_path()
    console.print("\n[bold red]📊 Gathering system overview...[/bold red]")
    parts = []

    # Source 1: mo status --json
    try:
        result = subprocess.run(
            [mo, "status", "--json"],
            capture_output=True, text=True, check=True, timeout=15
        )
        data = json.loads(result.stdout)
        health = data.get("health_score", "?")
        cpu = data.get("cpu", {}).get("usage", "?")
        mem_total = data.get("memory", {}).get("total", 0)
        mem_used = data.get("memory", {}).get("used", 0)
        mem_pct = data.get("memory", {}).get("used_percent", "?")
        uptime = data.get("uptime", "?")
        host = data.get("host", "Mac")

        mem_total_gb = round(mem_total / 1024 ** 3, 1) if mem_total else "?"
        mem_used_gb = round(mem_used / 1024 ** 3, 1) if mem_used else "?"

        parts.append(
            f"Health: {health}/100 | CPU: {cpu}% | "
            f"RAM: {mem_used_gb}/{mem_total_gb}GB ({mem_pct}%) | "
            f"Uptime: {uptime} | Host: {host}"
        )
    except Exception as e:
        parts.append(f"Mole status unavailable: {e}")

    # Source 2: df -h /
    try:
        df = subprocess.run(["df", "-h", "/"], capture_output=True, text=True, timeout=5)
        lines = df.stdout.strip().split("\n")
        if len(lines) >= 2:
            fields = lines[1].split()
            if len(fields) >= 4:
                parts.append(f"Disk: {fields[3]} free of {fields[1]} total ({fields[4]} used)")
    except Exception:
        pass

    # Source 3: Mārjak ops log
    log_path = os.path.expanduser("~/Library/Logs/marjak/operations.log")
    if os.path.exists(log_path):
        try:
            tail = subprocess.run(["tail", "-3", log_path], capture_output=True, text=True, timeout=3)
            if tail.stdout.strip():
                parts.append(f"Recent Mole log:\n{tail.stdout.strip()}")
        except Exception:
            pass

    # Source 4: Persistent memory context
    mem_ctx = memory.get_context_for_prompt()
    if "First session" not in mem_ctx:
        parts.append(mem_ctx)

    report = "System Overview:\n" + "\n".join(f"- {p}" for p in parts)
    memory.record_action("get_system_overview", report.split("\n")[0][:120])
    console.print("[bold red]✔ Overview ready.[/bold red]\n")
    return report


@tool
def call_executor(instructions: str) -> str:
    """Transfer control to the Executor Agent to perform destructive tasks. 
    Use this if the user wants to delete files. Pass the FIDs in the instructions.
    """
    return f"Handing off to Executor with instructions: {instructions}"


@tool
def call_navigator(instructions: str) -> str:
    """Transfer control back to the Navigator Agent to explore more files."""
    return f"Handing off to Navigator with instructions: {instructions}"


# ===========================================================================
# EXECUTOR TOOLS — Destructive actions (user must confirm first)
# ===========================================================================

@tool
def execute_deep_clean() -> str:
    """Run Mole's deep system cleanup. This DELETES system caches, browser data,
    dev tool caches, app leftovers, etc.

    The tool will show a preview first and ask for user confirmation before proceeding.
    """
    mo = _get_mole_path()

    # Phase 1: Preview
    console.print("\n[bold red]🧹 Deep Clean — Phase 1: Preview[/bold red]")
    try:
        preview = subprocess.run(
            [mo, "clean", "--dry-run"],
            capture_output=True, text=True,
            stdin=subprocess.DEVNULL,
            timeout=60
        )
        preview_out = (preview.stdout or "") + (preview.stderr or "")
        if preview_out.strip():
            console.print("[dim]--- Preview ---[/dim]")
            for line in preview_out.strip().split("\n"):
                console.print(f"  [dim]{line.strip()}[/dim]")
            console.print("[dim]--- End preview ---[/dim]\n")
    except Exception as e:
        console.print(f"  [dim yellow]Preview failed: {e}[/dim yellow]\n")

    # Phase 2: Python-level confirmation (bypasses LLM)
    try:
        confirm = Prompt.ask(
            "[bold red]Proceed with deep cleanup?[/bold red]",
            choices=["y", "n"], default="n"
        )
    except EOFError:
        confirm = "n"

    if confirm.lower() != "y":
        result = "User cancelled the deep clean."
        memory.record_action("execute_deep_clean", result)
        return result

    # Phase 3: Execute
    console.print("\n[bold red]🧹 Deep Clean — Phase 2: Executing...[/bold red]")
    result = stream_command([mo, "clean"], "Deep System Cleanup")
    memory.record_action("execute_deep_clean", result)
    return result


@tool
def run_system_optimization() -> str:
    """Run Mārjak's system optimization: refresh caches, reset network services,
    rebuild system databases. Does NOT delete personal files. Streams progress.
    """
    mo = _get_mole_path()
    result = stream_command([mo, "optimize"], "System Optimization")
    memory.record_action("run_system_optimization", result)
    return result


@tool
def move_to_trash(file_ids: list[int]) -> str:
    """Move specific files or directories to the macOS Trash (recoverable).
    Provide the exact integer File IDs [FID: X] from the Knowledge Tree.

    Args:
        file_ids: List of integer FIDs corresponding to the files you want to delete.
    """
    paths = session_book.get_paths_by_fids(file_ids)
    if not paths:
        return f"Error: No valid paths found for the provided File IDs ({file_ids})."

    results = []

    # Protected paths that must never be deleted
    protected = {
        "/", "/System", "/Library", "/usr", "/bin", "/sbin", "/var",
        "/Applications", os.path.expanduser("~"),
        os.path.expanduser("~/Library"),
        os.path.expanduser("~/Documents"),
        os.path.expanduser("~/Desktop"),
    }

    # Validate all paths first and show preview
    valid_targets = []
    for path in paths:
        absolute = os.path.abspath(os.path.expanduser(path))

        if absolute in protected:
            results.append(f"BLOCKED: '{absolute}' is a protected system path.")
            continue

        if not os.path.exists(absolute):
            results.append(f"NOT FOUND: '{absolute}' does not exist.")
            continue
        
        valid_targets.append(absolute)

    if not valid_targets:
        report = "\n".join(results) if results else "No valid targets to delete."
        memory.record_action("move_to_trash", report[:120])
        return report

    # Show preview and ask for confirmation
    console.print("\n[bold yellow]⚠ Files to be moved to Trash:[/bold yellow]")
    for t in valid_targets:
        try:
            size = os.path.getsize(t) if os.path.isfile(t) else 0
            console.print(f"  [dim yellow]• {os.path.basename(t)} [{_human_size(size)}][/dim yellow]")
        except Exception:
            console.print(f"  [dim yellow]• {os.path.basename(t)}[/dim yellow]")

    try:
        confirm = Prompt.ask(
            "[bold yellow]Confirm move to Trash?[/bold yellow]",
            choices=["y", "n"], default="y"
        )
    except EOFError:
        confirm = "n"

    if confirm.lower() != "y":
        result = "User cancelled the deletion."
        memory.record_action("move_to_trash", result)
        return result

    # Execute deletion
    for absolute in valid_targets:
        try:
            subprocess.run(
                ["mv", absolute, os.path.expanduser("~/.Trash/")],
                check=True
            )
            results.append(f"✔ Moved to Trash: {absolute}")
            session_book.remove_node(absolute)
        except subprocess.CalledProcessError as e:
            results.append(f"ERROR: Failed to move '{absolute}': {e}")

    report = "\n".join(results)
    memory.record_action("move_to_trash", report[:120])
    session_book.save()  # Persist VFS after deletions
    return report