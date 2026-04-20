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
from config_manager import config_manager

console = Console(highlight=False)

# ---------------------------------------------------------------------------
# Waste pattern tags — zero-cost intelligence for the LLM
# ---------------------------------------------------------------------------
_WASTE_PATTERNS = {
    "node_modules":  "[npm deps]",
    ".git":          "[git repo]",
    "__pycache__":   "[py cache]",
    "Caches":        "[cache]",
    "Cache":         "[cache]",
    ".DS_Store":     "[junk]",
    "DerivedData":   "[xcode build]",
    "build":         "[build output]",
    "dist":          "[build output]",
    "target":        "[build output]",
    "Logs":          "[logs]",
    ".Trash":        "[trash]",
    ".ollama":       "[ollama models]",
    "Podfile.lock":  "[cocoapods]",
    "Pods":          "[cocoapods]",
    ".cargo":        "[rust cache]",
    "venv":          "[virtualenv]",
    ".venv":         "[virtualenv]",
}

# Navigate cache TTL (seconds) — skip re-scanning paths scanned within this window
_NAVIGATE_CACHE_TTL = 120  # 2 minutes — short enough to allow recovery from errors

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
        """Saves current state to JSON (atomic write)."""
        try:
            os.makedirs(os.path.dirname(self.PATH), exist_ok=True)
            tmp = self.PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.data, f, indent=4)
            os.replace(tmp, self.PATH)
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
        """Generates compact context for the system prompt.
        Limits are driven by the active performance preset."""
        caps = config_manager.get_performance_settings()
        n_hotspots = caps.get("memory_hotspots", 10)
        n_actions = caps.get("memory_actions", 10)

        parts = []

        profile = self.data.get("system_profile", {})
        hotspots = profile.get("hotspots", [])[:n_hotspots]
        if hotspots:
            items = ", ".join(
                f"{os.path.basename(h['path'])} ({h['size_gb']}GB)"
                for h in hotspots
            )
            parts.append(f"KNOWN HOTSPOTS: {items}")

        history = self.data.get("session_history", [])[-n_actions:]
        if history:
            items = "; ".join(
                f"[{h['date']}] {h['action']}: {h['finding'][:60]}" for h in history
            )
            parts.append(f"RECENT ACTIVITY: {items}")

        prefs = self.data.get("user_preferences", {})
        ignores = prefs.get("safe_to_ignore", [])
        if ignores:
            parts.append(f"User ignores: {', '.join(ignores)}")

        return "\n".join(parts) if parts else ""


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

def _tag_waste(name: str) -> str:
    """Append a waste-pattern tag to known directory/file names."""
    tag = _WASTE_PATTERNS.get(name)
    return f"{name} {tag}" if tag else name


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

    # Cache hit: skip re-scanning if path was explored recently and is not stale
    existing = session_book.nodes.get(expanded)
    if existing and not existing.get("stale") and existing.get("scan_mtime"):
        age = time.time() - existing["scan_mtime"]
        if age < _NAVIGATE_CACHE_TTL:
            mins = round(age / 60, 1)
            console.print(f"[dim]⚡ Cache hit for {path} (scanned {mins}m ago)[/dim]")
            # Include child directory paths so the model can still drill deeper
            home = os.path.expanduser("~")
            children = existing.get("children", [])
            child_dirs = []
            for cp in children:
                cn = session_book.nodes.get(cp)
                if cn and cn.get("type") == "DIR":
                    try:
                        rel = "~/" + os.path.relpath(cp, home)
                    except (ValueError, TypeError):
                        rel = cp
                    child_dirs.append(f"  {rel} ({_human_size(cn.get('size', 0))})")
            nav_hint = "\nChild dirs:\n" + "\n".join(child_dirs[:5]) if child_dirs else ""
            return f"Already explored (scanned {mins}m ago). Check the VFS tree for contents.{nav_hint}"

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

    # Tag entries with waste patterns before adding to SessionBook
    for e in entries:
        e['name'] = _tag_waste(e['name'])

    # Add the structural data to the SessionBook representing exactly what was found.
    session_book.add_directory(expanded, total_size, entries, large_files)

    # Record in persistent memory
    memory.record_scan(expanded, round(total_size / 1024 ** 3, 2))
    memory.record_action("navigate", f"{path}: {_human_size(total_size)}, {len(entries)} items")
    session_book.save()  # Persist VFS after each navigation

    # Build a richer return value so the LLM can decide next steps without re-reading the full tree
    sorted_entries = sorted(entries, key=lambda e: e.get("size", 0), reverse=True)
    top3 = ", ".join(
        f"{e['name']} {_human_size(e['size'])}" for e in sorted_entries[:3]
    )
    # Collect waste-tagged items
    tagged = [e['name'] for e in entries if any(t in e['name'] for t in _WASTE_PATTERNS.values())]
    waste_note = f" Waste detected: {', '.join(tagged[:5])}." if tagged else ""

    # Include navigable paths for top directories so the LLM doesn't have to reconstruct them
    home = os.path.expanduser("~")
    dir_entries = [e for e in sorted_entries if e.get("is_dir")]
    nav_paths = []
    for e in dir_entries[:5]:
        try:
            rel = "~/" + os.path.relpath(e['path'], home)
        except (ValueError, TypeError):
            rel = e['path']
        nav_paths.append(f"  {rel} ({_human_size(e['size'])})")
    nav_hint = "\nNavigable dirs:\n" + "\n".join(nav_paths) if nav_paths else ""

    # Surface large files with FIDs so the model can use call_executor directly
    file_entries = [e for e in sorted_entries if not e.get("is_dir") and e.get("size", 0) >= 1024 * 1024]
    big_files = []
    for e in file_entries[:8]:
        fid = session_book._path_to_fid.get(e['path'].rstrip('/'))
        try:
            rel = "~/" + os.path.relpath(e['path'], home)
        except (ValueError, TypeError):
            rel = e['path']
        fid_tag = f" [FID:{fid}]" if fid else ""
        big_files.append(f"  {_human_size(e['size']):>10s} | {e['name']}{fid_tag}")
    file_hint = "\nFiles (use call_executor with FIDs to delete):\n" + "\n".join(big_files) if big_files else ""

    console.print(f"[bold red]✔ Explored {path}[/bold red]\n")
    return (
        f"Explored {path} ({_human_size(total_size)}, {len(entries)} items). "
        f"Top: {top3}. {len(large_files)} large files.{waste_note}"
        f"{nav_hint}{file_hint}"
    )


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

    # ── Deduplicate by resolved real path ──
    seen_real = set()
    unique_results = []
    for path in results:
        try:
            real = os.path.realpath(path)
        except (OSError, ValueError):
            real = path
        if real not in seen_real:
            seen_real.add(real)
            unique_results.append(path)
    results = unique_results

    # ── Enrich with sizes, filter zero-byte dirs ──
    enriched = []
    search_limit = config_manager.get_performance_settings().get("search_limit", 30)
    for path in results:
        if len(enriched) >= search_limit:
            break
        try:
            is_dir = os.path.isdir(path)
            if is_dir:
                try:
                    du = subprocess.run(
                        ["du", "-sm", path], capture_output=True, text=True, timeout=3
                    )
                    size_mb = int(du.stdout.split()[0])
                except Exception:
                    size_mb = 0
                # Skip zero-byte directories — they're noise
                if size_mb == 0:
                    continue
                size_bytes = size_mb * 1024 * 1024
                size_str = f"{size_mb} MB"
            else:
                stat = os.stat(path)
                size_bytes = stat.st_size
                size_str = _human_size(size_bytes)

            enriched.append({
                "path": path,
                "is_dir": is_dir,
                "size_bytes": size_bytes,
                "size_str": size_str,
            })
        except Exception:
            pass

    # Sort by size descending so the most relevant results come first
    enriched.sort(key=lambda e: e["size_bytes"], reverse=True)

    # ── Feed results into the VFS tree at their real parent paths ──
    # Only store results >= 1MB in VFS to avoid noise from tiny python packages etc.
    _VFS_MIN_SIZE = 1024 * 1024  # 1 MB
    for item in enriched:
        if item["size_bytes"] < _VFS_MIN_SIZE:
            continue
        path = item["path"]
        node_type = "DIR" if item["is_dir"] else "FILE"
        fid = session_book.assign_fid(path) if node_type == "FILE" else None

        # Abbreviate display: ~/relative/path
        try:
            rel = "~/" + os.path.relpath(path, home)
        except ValueError:
            rel = path

        node_data = {
            "name": os.path.basename(path),
            "path": path,
            "size": item["size_bytes"],
            "size_str": item["size_str"],
            "type": node_type,
            "search_hit": True,
            "children": []
        }
        if fid:
            node_data["fid"] = fid

        session_book.nodes[path] = node_data

        # Attach to actual parent in VFS tree (or create minimal parent chain)
        parent = os.path.dirname(path)
        if parent in session_book.nodes:
            if path not in session_book.nodes[parent]["children"]:
                session_book.nodes[parent]["children"].append(path)
        else:
            # Create a lightweight parent node so the tree stays rooted
            session_book.nodes[parent] = {
                "name": os.path.basename(parent) or parent,
                "path": parent,
                "size": 0,
                "type": "DIR",
                "stale": True,
                "children": [path]
            }

    session_book._dirty = True

    # ── Auto-navigate top 3 largest directories for immediate detail ──
    mo = _get_mole_path()
    auto_navigated = []
    for item in enriched[:3]:
        if not item["is_dir"] or item["size_bytes"] < 100 * 1024 * 1024:
            continue  # Skip files and dirs < 100 MB
        path = item["path"]
        # Skip if already explored recently
        existing = session_book.nodes.get(path)
        if existing and existing.get("scan_mtime") and not existing.get("stale"):
            age = time.time() - existing["scan_mtime"]
            if age < _NAVIGATE_CACHE_TTL:
                continue
        try:
            console.print(f"  [dim]↳ Auto-exploring {os.path.basename(path)}…[/dim]")
            proc = subprocess.run(
                [mo, "analyze", "--json", path],
                capture_output=True, text=True, timeout=15
            )
            if proc.returncode == 0:
                data = json.loads(proc.stdout)
                entries = data.get("entries", [])
                large_files = data.get("large_files", [])
                total_size = data.get("total_size", 0)
                for e in entries:
                    e['name'] = _tag_waste(e['name'])
                session_book.add_directory(path, total_size, entries, large_files)
                auto_navigated.append(path)
        except Exception:
            pass

    session_book.save()

    # ── Build response with actual navigable paths ──
    if not enriched:
        report = f"No results found for '{name}'."
    else:
        lines = [f"Found {len(enriched)} results for '{name}':"]
        for item in enriched[:8]:
            path = item["path"]
            try:
                rel = "~/" + os.path.relpath(path, home)
            except ValueError:
                rel = path
            kind = "DIR " if item["is_dir"] else "FILE"
            lines.append(f"  {kind} {item['size_str']:>10s} | {rel}")
        if len(enriched) > 8:
            lines.append(f"  ... and {len(enriched) - 8} more")
        if auto_navigated:
            lines.append(f"Auto-explored: {', '.join(os.path.basename(p) for p in auto_navigated)}")
        lines.append("Navigate into the largest directories for file-level detail.")
        report = "\n".join(lines)

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
def collect_deletable_files(path: str, min_size_mb: int = 0, name_pattern: str = "", exclude_pattern: str = "") -> str:
    """Query the explored filesystem tree for files under a directory.
    Returns file names, sizes, and FIDs ready to pass to call_executor.

    Use this BEFORE call_executor to get the exact FID list.

    Args:
        path: Directory path to search under (e.g. '~/Library/.../media').
        min_size_mb: Minimum file size in MB (0 = all files with FIDs).
        name_pattern: Only include files whose name contains this substring (case-insensitive). E.g. '_partial', 'telegram-cloud', '.mkv'.
        exclude_pattern: Exclude files whose name contains this substring (case-insensitive). E.g. 'db_sqlite', '.plist'.
    """
    expanded = os.path.expanduser(path).rstrip("/")
    threshold = min_size_mb * 1024 * 1024
    home = os.path.expanduser("~")
    name_filter = name_pattern.lower()
    exclude_filter = exclude_pattern.lower()

    matches = []
    for node_path, node in session_book.nodes.items():
        node_type = node.get("type")
        # Match both files (with FIDs) and directories (navigable, deletable)
        if node_type == "FILE" and "fid" not in node:
            continue
        if node_type not in ("FILE", "DIR"):
            continue
        if not node_path.startswith(expanded + "/") and node_path != expanded:
            continue
        size = node.get("size", 0)
        if size < threshold:
            continue
        fname = node.get("name", os.path.basename(node_path)).lower()
        if name_filter and name_filter not in fname:
            continue
        if exclude_filter and exclude_filter in fname:
            continue
        # Directories need a FID to be deletable — assign one if missing
        if node_type == "DIR" and "fid" not in node:
            node["fid"] = session_book.assign_fid(node_path)
        if "fid" not in node:
            continue
        try:
            rel = "~/" + os.path.relpath(node_path, home)
        except (ValueError, TypeError):
            rel = node_path
        matches.append({
            "fid": node["fid"],
            "name": node.get("name", os.path.basename(node_path)),
            "size": size,
            "size_str": _human_size(size),
            "rel_path": rel,
            "type": node_type,
        })

    matches.sort(key=lambda m: m["size"], reverse=True)

    # Build filter description for the response
    filters = []
    if min_size_mb > 0:
        filters.append(f">= {min_size_mb} MB")
    if name_pattern:
        filters.append(f"name contains '{name_pattern}'")
    if exclude_pattern:
        filters.append(f"excluding '{exclude_pattern}'")
    filter_desc = " (" + ", ".join(filters) + ")" if filters else ""

    if not matches:
        # Check if the target path itself is a known directory in VFS
        target_node = session_book.nodes.get(expanded)
        if target_node and target_node.get("type") == "DIR":
            tsize = target_node.get("size", 0)
            if "fid" not in target_node:
                target_node["fid"] = session_book.assign_fid(expanded)
            try:
                trel = "~/" + os.path.relpath(expanded, home)
            except (ValueError, TypeError):
                trel = expanded
            return (
                f"No individual files{filter_desc} found under {path}, but the directory itself is {_human_size(tsize)}.\n"
                f"To delete the entire directory: call_executor(\"Delete FID {target_node['fid']} — {trel} ({_human_size(tsize)})\")\n"
                f"Or navigate({path}) first to see its contents before deciding."
            )
        return f"No files matching{filter_desc} found under {path}. Navigate into it first with navigate(\"{path}\")."

    fid_list = [m["fid"] for m in matches]
    total = sum(m["size"] for m in matches)
    n_files = sum(1 for m in matches if m["type"] == "FILE")
    n_dirs = sum(1 for m in matches if m["type"] == "DIR")
    type_desc = []
    if n_files:
        type_desc.append(f"{n_files} files")
    if n_dirs:
        type_desc.append(f"{n_dirs} directories")
    lines = [f"Found {' + '.join(type_desc)}{filter_desc} under {path} (total {_human_size(total)}):"]
    for m in matches:
        tag = "DIR" if m["type"] == "DIR" else "FILE"
        lines.append(f"  FID:{m['fid']} | {m['size_str']:>10s} | [{tag}] {m['name']}")
    lines.append(f"\nFID list for call_executor: {fid_list}")
    lines.append(f'call_executor("Delete FIDs {fid_list} — {" + ".join(type_desc)}, {_human_size(total)} total")')

    return "\n".join(lines)


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


# ===========================================================================
# EXPERT-ONLY: Direct Shell Access (read-only, sandboxed)
# ===========================================================================

# Allowed command prefixes — read-only system inspection only.
# Destructive commands (rm, mv, sudo, etc.) are never permitted.
_SHELL_ALLOW = {
    "ls", "du", "find", "stat", "file", "cat", "head", "tail", "wc",
    "mdls", "diskutil", "df", "top", "ps", "lsof", "sw_vers",
    "system_profiler", "sysctl", "pmset", "defaults", "plutil",
    "xattr", "ditto", "hdiutil", "codesign", "spctl",
    "mdfind", "mdutil", "tmutil", "log",
}
_SHELL_DENY = {
    "rm", "rmdir", "mv", "cp", "sudo", "osascript", "kill", "killall",
    "launchctl", "chmod", "chown", "chflags", "mkfs", "newfs",
    "dd", "diskutil eraseDisk", "diskutil partitionDisk",
    "curl", "wget", "ssh", "scp", "nc", "ncat", "python", "ruby",
    "perl", "bash", "zsh", "sh", "open", "pbcopy", "pbpaste",
}


@tool
def run_shell(command: str) -> str:
    """Execute a read-only shell command for deep system inspection.
    Only available in Expert tier. Destructive commands are blocked.

    Use this for things the other tools can't do: checking symlinks,
    reading file headers, listing processes, inspecting extended attributes,
    querying Spotlight metadata, checking disk partitions, etc.

    Args:
        command: A shell command to execute (e.g. 'du -sh ~/Library/Caches/*',
                 'find ~/Library -name "*.log" -size +10M', 'diskutil list').
    """
    import shlex

    command = command.strip()
    if not command:
        return "Error: Empty command."

    # Parse first token to check against allow/deny lists
    try:
        tokens = shlex.split(command)
    except ValueError as e:
        return f"Error: Could not parse command: {e}"

    base_cmd = os.path.basename(tokens[0]) if tokens else ""

    # Deny list check (exact match on base command)
    if base_cmd in _SHELL_DENY:
        return f"BLOCKED: '{base_cmd}' is not permitted. Use Executor tools for destructive actions."

    # Allow list check
    if base_cmd not in _SHELL_ALLOW:
        return f"BLOCKED: '{base_cmd}' is not in the allowed command set. Allowed: {', '.join(sorted(_SHELL_ALLOW))}"

    # Extra safety: reject any pipe/redirect to denied commands
    for deny in _SHELL_DENY:
        if f"| {deny}" in command or f"|{deny}" in command:
            return f"BLOCKED: piping to '{deny}' is not permitted."

    console.print(f"[dim cyan]$ {command}[/dim cyan]")

    # Ask user permission before every shell execution
    try:
        confirm = Prompt.ask(
            "[bold yellow]Allow this shell command?[/bold yellow]",
            choices=["y", "n"], default="y"
        )
    except EOFError:
        confirm = "n"

    if confirm.lower() != "y":
        return "User denied shell command execution."

    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=15,
            cwd=os.path.expanduser("~"),
        )
        output = (result.stdout or "") + (result.stderr or "")
        output = output.strip()

        # Cap output to prevent context flooding
        if len(output) > 4000:
            output = output[:3900] + f"\n\n... (truncated, {len(output)} chars total)"

        if result.returncode != 0:
            return f"Command exited with code {result.returncode}:\n{output}" if output else f"Command failed (code {result.returncode})."
        return output if output else "(no output)"
    except subprocess.TimeoutExpired:
        return f"Error: Command timed out (>15s). Try a more specific query."
    except Exception as e:
        return f"Error: {e}"