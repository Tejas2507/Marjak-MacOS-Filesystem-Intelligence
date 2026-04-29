# session_book.py — Mārjak v2: Session-Only Explored Tree
#
# In-memory tree of directories/files explored THIS session.
# No cross-session persistence — the persistent SQLite store (fs_memory.py)
# handles that. This is purely a conversation-scoped context window.

import os


MAX_NODES = 40  # Hard cap — triggers focus-based eviction when exceeded


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

    V2 changes:
    - No save/load — purely in-memory, fresh each session
    - MAX_NODES cap with focus-based eviction
    - Zoom rendering: ancestors collapsed, focus expanded
    - Path display fix: root nodes show ~/relative, children show basename
    """

    def __init__(self):
        self.nodes = {}
        self.id_mapping = {}
        self._path_to_fid = {}
        self.next_fid = 1
        self._dirty = True
        self._cached_tree = ""
        # Focus tracking — updated on each add_directory
        self._focus_path = ""

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
        """Wipes the session tree entirely (in-memory only)."""
        self.nodes = {}
        self.id_mapping = {}
        self._path_to_fid = {}
        self.next_fid = 1
        self._dirty = True
        self._cached_tree = ""
        self._focus_path = ""

    def get_paths_by_fids(self, fids: list) -> list:
        """Translates FIDs back into actual system paths for move_to_trash."""
        return [self.id_mapping.get(int(fid)) for fid in fids if int(fid) in self.id_mapping]

    # ------------------------------------------------------------------
    # Focus-based eviction
    # ------------------------------------------------------------------

    def _evict_if_needed(self):
        """When node count exceeds MAX_NODES, evict leaf nodes farthest from focus."""
        if len(self.nodes) <= MAX_NODES:
            return

        focus_parts = self._focus_path.split("/") if self._focus_path else []

        def _distance(path):
            """Number of path components different from the focus path."""
            parts = path.split("/")
            # Count shared prefix length
            shared = 0
            for a, b in zip(parts, focus_parts):
                if a == b:
                    shared += 1
                else:
                    break
            return (len(parts) - shared) + (len(focus_parts) - shared)

        while len(self.nodes) > MAX_NODES:
            # Find leaf nodes (no children in our tree)
            all_children = set()
            for n in self.nodes.values():
                all_children.update(n.get("children", []))

            leaves = [p for p in self.nodes if p not in all_children or not self.nodes[p].get("children")]
            # Don't evict the focus path itself
            leaves = [p for p in leaves if p != self._focus_path]
            if not leaves:
                break  # Safety — nothing evictable

            # Evict the leaf farthest from focus
            leaves.sort(key=_distance, reverse=True)
            victim = leaves[0]
            del self.nodes[victim]
            # Remove from parent children lists
            for n in self.nodes.values():
                children = n.get("children", [])
                if victim in children:
                    children.remove(victim)
            # Clean FID mapping
            fid = self._path_to_fid.pop(victim, None)
            if fid is not None:
                self.id_mapping.pop(fid, None)

    # ------------------------------------------------------------------
    # Data ingestion
    # ------------------------------------------------------------------

    def add_directory(self, parent_path: str, total_size: int, entries: list, large_files: list = None):
        """Called by navigate() to register a directory's contents."""
        self._dirty = True
        parent_path = os.path.expanduser(parent_path).rstrip("/")
        if not parent_path:
            parent_path = "/"

        # Update focus to the most recently navigated path
        self._focus_path = parent_path

        self.nodes[parent_path] = {
            "name": os.path.basename(parent_path) or parent_path,
            "path": parent_path,
            "size": total_size,
            "type": "DIR",
            "children": [],
        }

        _VFS_FILE_FLOOR = 1024 * 1024  # 1 MB — skip tiny files

        for e in entries:
            child_path = e["path"].rstrip("/")
            node_type = "DIR" if e.get("is_dir") else "FILE"
            if node_type == "FILE" and e.get("size", 0) < _VFS_FILE_FLOOR:
                continue
            self.nodes[parent_path]["children"].append(child_path)
            self.nodes[child_path] = {
                "name": e["name"],
                "path": child_path,
                "size": e["size"],
                "type": node_type,
                "children": [],
            }
            if node_type == "FILE":
                self.nodes[child_path]["fid"] = self.assign_fid(child_path)

        if large_files:
            for lf in large_files:
                lf_path = lf["path"].rstrip("/")
                lf_parent = os.path.dirname(lf_path)
                if lf_parent != parent_path:
                    continue
                if lf_path not in self.nodes[parent_path]["children"]:
                    self.nodes[parent_path]["children"].append(lf_path)
                self.nodes[lf_path] = {
                    "name": lf["name"] + " (Large File)",
                    "path": lf_path,
                    "size": lf["size"],
                    "type": "FILE",
                    "fid": self.assign_fid(lf_path),
                    "children": [],
                }

        self._evict_if_needed()

    def add_scan_result(self, category: str, size_str: str):
        """Called by mole_scan() to add general findings."""
        self._dirty = True
        path = f"Mole Scan: {category}"
        self.nodes[path] = {
            "name": category,
            "path": path,
            "size_str": size_str,
            "type": "CATEGORY",
            "children": [],
        }
        if "Mole Scan Results" not in self.nodes:
            self.nodes["Mole Scan Results"] = {
                "name": "Auto-Cleanable Categories",
                "path": "Mole Scan Results",
                "size": 0,
                "type": "ROOT",
                "children": [],
            }
        if path not in self.nodes["Mole Scan Results"]["children"]:
            self.nodes["Mole Scan Results"]["children"].append(path)

    def remove_node(self, path: str):
        """Removes a path from the knowledge tree (e.g. after deletion)."""
        self._dirty = True
        if path in self.nodes:
            del self.nodes[path]
        for node in self.nodes.values():
            if path in node.get("children", []):
                node["children"].remove(path)

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def render_tree(self, max_chars: int = 8000, max_children_per_dir: int = 20) -> str:
        """Builds a compact ASCII tree for the LLM with zoom rendering.

        Zoom rules (when a focus path is set):
        - Focus node: fully expanded (all children shown)
        - Ancestors of focus: shown as single collapsed line (name + size)
        - Siblings of ancestors: shown only if > 1 GB, collapsed
        - Non-focus branches: collapsed or hidden

        Bug fix (1a): Root nodes (no parent in tree) always show ~/relative
        path as display name. Child nodes with parent in tree show basename.
        """
        if not self._dirty and self._cached_tree:
            return self._cached_tree

        if not self.nodes:
            self._cached_tree = "No directories explored yet. Use navigate('~') to begin mapping."
            self._dirty = False
            return self._cached_tree

        home = os.path.expanduser("~")

        # Identify root nodes (not a child of any tracked node)
        all_children = set()
        for node in self.nodes.values():
            all_children.update(node.get("children", []))
        roots = [p for p in self.nodes if p not in all_children]
        roots.sort(key=lambda p: self.nodes[p].get("size", 0), reverse=True)

        # Build the set of focus ancestors for zoom rendering
        focus_ancestors = set()
        if self._focus_path:
            parts = self._focus_path.split("/")
            for i in range(1, len(parts)):
                focus_ancestors.add("/".join(parts[:i]))

        total_size = sum(n.get("size", 0) for n in self.nodes.values() if n.get("type") == "DIR")
        lines = [
            f"Filesystem map ({len(self.nodes)} nodes | {_human_size(total_size)} mapped). "
            f"Use FULL paths from tool results for navigate(). FIDs are for move_to_trash only."
        ]
        chars_used = [len(lines[0])]

        def _abbreviated_path(path):
            """Show ~/relative path for a given absolute path."""
            try:
                return "~/" + os.path.relpath(path, home)
            except (ValueError, TypeError):
                return path

        def _display_name(node, path, is_root):
            """Bug fix 1a: root nodes show ~/relative, children show basename."""
            raw = node.get("name", "")
            raw = raw.replace(" (Large File)", "")

            if is_root and node.get("type") == "DIR":
                # Root nodes (orphans in tree) — show navigable ~/relative path
                raw = _abbreviated_path(path)
            elif node.get("search_hit"):
                # Search hits not yet explored — show navigable path
                raw = _abbreviated_path(path)

            # Display-only warning for large dirs (>1GB)
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

        def _build_tree_recursive(path, prefix="", is_last=True, is_root=False):
            node = self.nodes.get(path)
            if not node:
                return
            if chars_used[0] >= max_chars:
                return
            # Skip 0-byte empty dirs (noise)
            if node["type"] in ("DIR", "ROOT") and node.get("size", 0) == 0 and not node.get("children"):
                return

            size_display = node.get("size_str") or _human_size(node.get("size", 0))
            icon = "📁" if node["type"] in ("DIR", "ROOT") else "📄"
            if node["type"] == "CATEGORY":
                icon = "🧹"

            display = _display_name(node, path, is_root)

            fid_suffix = ""
            if node["type"] in ("FILE", "CATEGORY") and "fid" in node:
                fid_suffix = f" [FID:{node['fid']}]"

            line = f"{prefix}{icon} {display} [{size_display}]{fid_suffix}"
            lines.append(line)
            chars_used[0] += len(line) + 1

            # --- Zoom: collapse non-focus ancestors ---
            # If this node is a focus ancestor (but not the focus itself),
            # show it as a single line — don't expand children (except the
            # child that leads toward focus).
            children = node.get("children", [])
            children.sort(key=lambda c: self.nodes.get(c, {}).get("size", 0), reverse=True)

            is_focus_ancestor = path in focus_ancestors and path != self._focus_path
            if is_focus_ancestor and children:
                # Only show the child that is on the path to focus, plus siblings >1GB
                focus_child = None
                big_siblings = []
                for c in children:
                    if c == self._focus_path or c in focus_ancestors:
                        focus_child = c
                    elif self.nodes.get(c, {}).get("size", 0) >= 1024 ** 3:
                        big_siblings.append(c)

                show_children = []
                if focus_child:
                    show_children.append(focus_child)
                show_children.extend(big_siblings)
                hidden = len(children) - len(show_children)

                new_prefix = prefix.replace("├── ", "│   ").replace("└── ", "    ")
                for i, child_path in enumerate(show_children):
                    child_is_last = (i == len(show_children) - 1) and (hidden <= 0)
                    pointer = "└── " if child_is_last else "├── "
                    _build_tree_recursive(child_path, new_prefix + pointer, child_is_last)

                if hidden > 0:
                    lines.append(f"{new_prefix}└── ... and {hidden} more items collapsed")
                return

            # --- Normal expansion (focus node or no zoom active) ---
            remaining = max_chars - chars_used[0]
            avg_line_len = 60
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
            _build_tree_recursive(root, prefix="", is_root=True)

        if chars_used[0] >= max_chars:
            est_tokens = chars_used[0] // 4
            lines.append(f"\n[Tree capped at {max_chars} chars (~{est_tokens} tokens). Use navigate() to explore further.]")

        self._cached_tree = "\n".join(lines)
        self._dirty = False
        return self._cached_tree
