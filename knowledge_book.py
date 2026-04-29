# knowledge_book.py — Mārjak v4: State-Based Context Manager

import os
import json

VFS_PATH = os.path.expanduser("~/.marjak/session_book.json")

def _human_size(size_bytes: int) -> str:
    """Convert bytes to human-readable string."""
    if size_bytes >= 1024 ** 3:
        return f"{round(size_bytes / 1024 ** 3, 2)} GB"
    elif size_bytes >= 1024 ** 2:
        return f"{round(size_bytes / 1024 ** 2, 1)} MB"
    elif size_bytes >= 1024:
        return f"{round(size_bytes / 1024, 1)} KB"
    return f"{size_bytes} B"

class SessionBook:
    """
    Maintains a deterministic tree of directories and files the agent has 
    explored DURING THE CURRENT SESSION. 
    
    Instead of passing heavy tool outputs back to the LLM context limitlessly,
    tools feed data into this Book, which is rendered as an ASCII tree and 
    injected dynamically into the system prompt.
    """
    
    def __init__(self):
        # A dictionary mapping absolute paths to their information.
        # This flat structure makes updating O(1) by path, and we can sort/render later.
        self.nodes = {}
        # Secure mapping of FID -> Absolute Path to protect LLM context overhead
        self.id_mapping = {}
        # Reverse mapping for O(1) path -> FID lookups
        self._path_to_fid = {}
        self.next_fid = 1
        # Dirty flag for render caching
        self._dirty = True
        self._cached_tree = ""

    def assign_fid(self, path: str) -> int:
        """Assigns or returns an existing File ID (FID) for a path. O(1) lookup."""
        if path in self._path_to_fid:
            return self._path_to_fid[path]
        
        fid = self.next_fid
        self.id_mapping[fid] = path
        self._path_to_fid[path] = fid
        self.next_fid += 1
        return fid

    def wipe(self):
        """Wipes the session VFS entirely, including the persisted JSON on disk."""
        self.nodes = {}
        self.id_mapping = {}
        self._path_to_fid = {}
        self.next_fid = 1
        self._dirty = True
        self._cached_tree = ""
        # Delete persisted file so the next session starts fresh
        try:
            if os.path.exists(VFS_PATH):
                os.remove(VFS_PATH)
        except OSError:
            pass

    def save(self):
        """Persists the VFS to disk so it survives session restarts."""
        try:
            os.makedirs(os.path.dirname(VFS_PATH), exist_ok=True)
            data = {
                "nodes": self.nodes,
                "id_mapping": {str(k): v for k, v in self.id_mapping.items()},
                "next_fid": self.next_fid,
            }
            tmp = VFS_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, VFS_PATH)
        except IOError:
            pass

    def load(self):
        """Restores the VFS from disk and intelligently validates all real paths.
        
        Three-pass validation on load:
        1. Deleted externally: path no longer exists -> pruned from VFS.
        2. Modified externally: directory mtime changed -> marked stale=[↻ STALE].
           The agent will see this in the tree and know to re-navigate.
        3. Virtual nodes (Search Results, Mole Scan Results): kept as-is.
        """
        if not os.path.exists(VFS_PATH):
            return
        try:
            with open(VFS_PATH) as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError):
            return

        nodes = data.get("nodes", {})
        id_mapping = {int(k): v for k, v in data.get("id_mapping", {}).items()}
        
        # Pass 1: Prune paths deleted externally by the user
        deleted_paths = set()
        for path in list(nodes.keys()):
            is_real_path = path.startswith("/") or path.startswith("~")
            if is_real_path and not os.path.exists(os.path.expanduser(path)):
                deleted_paths.add(path)

        if deleted_paths:
            for p in deleted_paths:
                nodes.pop(p, None)
            for node in nodes.values():
                node["children"] = [c for c in node.get("children", []) if c not in deleted_paths]
            id_mapping = {fid: p for fid, p in id_mapping.items() if p not in deleted_paths}

        # Pass 2: Detect directories modified externally (new files added by user)
        # Compare stored scan_mtime to current on-disk mtime
        for path, node in nodes.items():
            if node.get("type") != "DIR":
                continue
            abs_path = os.path.expanduser(path)
            if not os.path.isdir(abs_path):
                continue
            stored_mtime = node.get("scan_mtime")
            if stored_mtime is None:
                # Old node without mtime — conservatively mark stale
                node["stale"] = True
                continue
            try:
                current_mtime = os.path.getmtime(abs_path)
                # >1s tolerance avoids false positives from filesystem timestamp rounding
                node["stale"] = abs(current_mtime - stored_mtime) > 1.0
            except OSError:
                node["stale"] = False

        # Pass 3: Auto-correct stale directories
        # For each stale dir, do a fast os.listdir() and prune children
        # that no longer exist on disk. We can fix deletions without the LLM.
        # New files in the dir remain unknown — stale flag kept so the agent re-explores.
        auto_pruned = set()
        for path, node in list(nodes.items()):
            if not node.get("stale"):
                continue
            abs_path = os.path.expanduser(path)
            if not os.path.isdir(abs_path):
                continue
            try:
                live_entries = set(
                    os.path.join(abs_path, e) for e in os.listdir(abs_path)
                )
                dead_children = [
                    c for c in node.get("children", [])
                    if c.startswith("/") and c not in live_entries
                ]
                if dead_children:
                    for dc in dead_children:
                        node["children"].remove(dc)
                        auto_pruned.add(dc)
                
                # Check if disk now has NEW entries we don't know about
                known_children = set(node.get("children", []))
                has_new_entries = bool(live_entries - known_children)
                # If only deletions happened (no new files), clear the stale flag
                if not has_new_entries:
                    node["stale"] = False
            except OSError:
                pass

        # Remove auto-pruned paths from the full node map
        if auto_pruned:
            for p in auto_pruned:
                nodes.pop(p, None)
            id_mapping = {fid: p for fid, p in id_mapping.items() if p not in auto_pruned}

        self.nodes = nodes
        self.id_mapping = id_mapping
        self._path_to_fid = {v: k for k, v in id_mapping.items()}
        self.next_fid = data.get("next_fid", 1)
        self._dirty = True  # Force re-render after load



    def get_paths_by_fids(self, fids: list) -> list:
        """Translates FIDs back into actual system paths for move_to_trash."""
        return [self.id_mapping.get(int(fid)) for fid in fids if int(fid) in self.id_mapping]
        
    def add_directory(self, parent_path: str, total_size: int, entries: list, large_files: list = None):
        """Called by navigate() to register a directory's contents."""
        self._dirty = True
        parent_path = os.path.expanduser(parent_path).rstrip('/')
        if not parent_path:
            parent_path = '/'
            
        # Capture mtime at scan time so we can detect external changes on next load
        try:
            scan_mtime = os.path.getmtime(parent_path)
        except OSError:
            scan_mtime = None

        self.nodes[parent_path] = {
            "name": os.path.basename(parent_path) or parent_path,
            "path": parent_path,
            "size": total_size,
            "type": "DIR",
            "scan_mtime": scan_mtime,
            "stale": False,
            "children": []
        }
        
        # Minimum size for FILE entries to be stored in the VFS tree.
        # Tiny files (< 1 MB) add noise without actionable value — users
        # never delete individual sub-MB files.  Dirs always stored (navigable).
        _VFS_FILE_FLOOR = 1024 * 1024  # 1 MB

        for e in entries:
            child_path = e['path'].rstrip('/')
            node_type = "DIR" if e.get("is_dir") else "FILE"
            # Skip tiny files — they bloat the node count without value
            if node_type == "FILE" and e.get('size', 0) < _VFS_FILE_FLOOR:
                continue
            self.nodes[parent_path]["children"].append(child_path)
            self.nodes[child_path] = {
                "name": e['name'],
                "path": child_path,
                "size": e['size'],
                "type": node_type,
                "children": []
            }
            if node_type == "FILE":
                self.nodes[child_path]["fid"] = self.assign_fid(child_path)
            
        if large_files:
            for lf in large_files:
                lf_path = lf['path'].rstrip('/')
                # Only attach large files that are actual direct children
                # (depth 1). Deep files will appear when user navigates deeper.
                lf_parent = os.path.dirname(lf_path)
                if lf_parent != parent_path:
                    continue
                if lf_path not in self.nodes[parent_path]["children"]:
                    self.nodes[parent_path]["children"].append(lf_path)
                self.nodes[lf_path] = {
                    "name": lf['name'] + " (Large File)",
                    "path": lf_path,
                    "size": lf['size'],
                    "type": "FILE",
                    "fid": self.assign_fid(lf_path),
                    "children": []
                }

    def add_scan_result(self, category: str, size_str: str):
        """Called by mole_scan() to add general findings."""
        self._dirty = True
        path = f"Mole Scan: {category}"
        self.nodes[path] = {
            "name": category,
            "path": path,
            "size_str": size_str,  # Pre-formatted
            "type": "CATEGORY",
            "children": []
        }
        if "Mole Scan Results" not in self.nodes:
            self.nodes["Mole Scan Results"] = {
                "name": "Auto-Cleanable Categories",
                "path": "Mole Scan Results",
                "size": 0,
                "type": "ROOT",
                "children": []
            }
        if path not in self.nodes["Mole Scan Results"]["children"]:
            self.nodes["Mole Scan Results"]["children"].append(path)

    def remove_node(self, path: str):
        """Removes a path from the knowledge tree (e.g. after deletion)."""
        self._dirty = True
        if path in self.nodes:
            del self.nodes[path]
        # Remove from any parent's children list
        for node in self.nodes.values():
            if path in node.get("children", []):
                node["children"].remove(path)

    def render_tree(self, max_chars: int = 8000, max_children_per_dir: int = 20) -> str:
        """Builds the compact ASCII tree representation for the LLM.
        Uses dirty-flag caching to avoid redundant serialization.
        
        Args:
            max_chars: Character budget for the tree (~4 chars per token).
            max_children_per_dir: Hard cap on children shown per directory.
        """
        if not self._dirty and self._cached_tree:
            return self._cached_tree
            
        if not self.nodes:
            self._cached_tree = "No directories explored yet. Use navigate('~') to begin mapping."
            self._dirty = False
            return self._cached_tree

        home = os.path.expanduser("~")

        # Find roots (nodes that are not children of any other node WE track)
        all_children = set()
        for node in self.nodes.values():
            all_children.update(node["children"])
            
        roots = [p for p in self.nodes if p not in all_children]
        
        # Sort roots by size descending
        roots.sort(key=lambda p: self.nodes[p].get("size", 0), reverse=True)
        
        total_size = sum(n.get('size', 0) for n in self.nodes.values() if n.get('type') == 'DIR')
        lines = [f"Filesystem map ({len(self.nodes)} nodes | {_human_size(total_size)} mapped). Paths are navigable with navigate(path). FIDs are for move_to_trash only."]
        
        chars_used = [len(lines[0])]
        
        def _abbreviate_name(node):
            """Build a compact display name for the tree."""
            raw = node.get("name", "")
            # Strip "(Large File)" suffix — size already conveys this
            raw = raw.replace(" (Large File)", "")
            # Display-only warning for large dirs (>1GB) — NOT stored in name
            if node.get("type") == "DIR" and node.get("size", 0) >= 1024 ** 3:
                raw = "⚠ " + raw
            # Truncate hash-like filenames (>40 chars, no spaces)
            if len(raw) > 40 and " " not in raw:
                base, dot, ext = raw.rpartition(".")
                if dot and len(ext) <= 6:
                    raw = base[:16] + "…" + dot + ext
                else:
                    raw = raw[:16] + "…"
            return raw
        
        def _abbreviated_path(node):
            """For search-hit nodes, show ~/relative path so the LLM can navigate."""
            path = node.get("path", "")
            try:
                return "~/" + os.path.relpath(path, home)
            except (ValueError, TypeError):
                return path

        def _build_tree_recursive(path, prefix="", is_last=True):
            node = self.nodes.get(path)
            if not node: return
            if chars_used[0] >= max_chars:
                return
            # Skip 0-byte directories (noise from search results with no actual content)
            if node["type"] in ("DIR", "ROOT") and node.get("size", 0) == 0 and not node.get("children"):
                return
            
            if "size_str" in node and "size" not in node:
                size_display = node["size_str"]
            else:
                size_display = _human_size(node.get("size", 0))
                
            icon = "📁" if node["type"] in ["DIR", "ROOT"] else "📄"
            if node["type"] == "CATEGORY": icon = "🧹"
            
            display_name = _abbreviate_name(node)
            
            # For search-hit nodes not yet explored, show navigable path
            if node.get("search_hit") and not node.get("scan_mtime"):
                display_name = _abbreviated_path(node)
            
            # FID suffix for files
            if node["type"] in ["FILE", "CATEGORY"] and "fid" in node:
                fid_suffix = f" [FID:{node['fid']}]"
            else:
                fid_suffix = ""
            
            # Stale suffix for directories modified externally since last scan
            stale_suffix = " [↻ STALE]" if node.get("stale") else ""
            
            line = f"{prefix}{icon} {display_name} [{size_display}]{fid_suffix}{stale_suffix}"
            lines.append(line)
            chars_used[0] += len(line) + 1  # +1 for newline
            
            children = node.get("children", [])
            children.sort(key=lambda c: self.nodes.get(c, {}).get("size", 0), reverse=True)
            
            # Dynamic child limit: show more children when char budget allows.
            remaining = max_chars - chars_used[0]
            avg_line_len = 60  # conservative avg chars per child line
            max_children_budget = max(5, remaining // avg_line_len)
            effective_max = min(max_children_per_dir, max_children_budget)
            display_children = children[:effective_max]
            hidden = len(children) - len(display_children)
            
            new_prefix = prefix.replace("├── ", "│   ").replace("└── ", "    ")
            
            for i, child_path in enumerate(display_children):
                child_is_last = (i == len(display_children) - 1) and (hidden <= 0)
                pointer = "└── " if child_is_last else "├── "
                _build_tree_recursive(child_path, new_prefix + pointer, child_is_last)
                
            if hidden > 0:
                lines.append(f"{new_prefix}└── ... and {hidden} more items hidden")

        for root in roots:
            _build_tree_recursive(root, prefix="")
        
        if chars_used[0] >= max_chars:
            est_tokens = chars_used[0] // 4
            lines.append(f"\n[Tree capped at {max_chars} chars (~{est_tokens} tokens). Use navigate() to explore further.]")
        self._cached_tree = "\n".join(lines)
        self._dirty = False
        return self._cached_tree
