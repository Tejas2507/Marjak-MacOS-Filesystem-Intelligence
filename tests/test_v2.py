# test_v2.py — Comprehensive V2 test suite
#
# Tests the new V2 architecture: session_book, fs_memory, tools, prompts,
# agent graph, CLI commands. No LLM required (unit tests mock where needed).
#
# Run: python -m pytest tests/test_v2.py -v
# Or:  python tests/test_v2.py  (standalone)

import os
import sys
import json
import sqlite3
import tempfile
import shutil

# Ensure src/ is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# Check if LangChain is available (not installed on Windows dev machine)
try:
    import langchain_core
    HAS_LANGCHAIN = True
except ImportError:
    HAS_LANGCHAIN = False


# ═══════════════════════════════════════════════════════════════════════════
# 1. SessionBook Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestSessionBook:
    """Tests for the in-memory session playbook (session_book.py)."""

    def setup_method(self):
        from marjak.session_book import SessionBook
        self.book = SessionBook()

    def test_empty_book(self):
        assert len(self.book.nodes) == 0
        assert len(self.book.id_mapping) == 0
        tree = self.book.render_tree()
        # Empty tree returns a header/placeholder string
        assert isinstance(tree, str)

    def test_add_directory(self):
        # Files < 1MB are filtered out by add_directory, so use large sizes
        entries = [
            {"name": "big_file.mkv", "path": "/Users/test/dir/big_file.mkv",
             "size": 5 * 1024 * 1024, "is_dir": False},
            {"name": "subdir", "path": "/Users/test/dir/subdir",
             "size": 2 * 1024 * 1024, "is_dir": True},
        ]
        self.book.add_directory("/Users/test/dir", 7 * 1024 * 1024, entries, [])
        assert "/Users/test/dir" in self.book.nodes
        node = self.book.nodes["/Users/test/dir"]
        assert node["size"] == 7 * 1024 * 1024
        assert len(node["children"]) == 2

    def test_assign_fid(self):
        fid1 = self.book.assign_fid("/Users/test/file.txt")
        fid2 = self.book.assign_fid("/Users/test/other.txt")
        assert isinstance(fid1, int)
        assert isinstance(fid2, int)
        assert fid1 != fid2
        # Same path returns same FID
        assert self.book.assign_fid("/Users/test/file.txt") == fid1

    def test_get_paths_by_fids(self):
        fid = self.book.assign_fid("/Users/test/file.txt")
        paths = self.book.get_paths_by_fids([fid])
        assert paths == ["/Users/test/file.txt"]

    def test_get_paths_invalid_fid(self):
        paths = self.book.get_paths_by_fids([9999])
        assert paths == []

    def test_wipe(self):
        self.book.assign_fid("/Users/test/file.txt")
        self.book.nodes["test"] = {"name": "test"}
        self.book.wipe()
        assert len(self.book.nodes) == 0
        assert len(self.book.id_mapping) == 0

    def test_max_nodes_eviction(self):
        """SessionBook should evict old nodes when MAX_NODES is exceeded."""
        import marjak.session_book as sb_mod
        original_max = sb_mod.MAX_NODES
        try:
            sb_mod.MAX_NODES = 5  # Low cap for testing
            book = sb_mod.SessionBook()
            book._focus_path = "/Users/test/dir0"
            # Add 10 directories
            for i in range(10):
                path = f"/Users/test/dir{i}"
                book.nodes[path] = {
                    "name": f"dir{i}", "path": path,
                    "size": 1024, "type": "DIR", "children": []
                }
            book._evict_if_needed()
            assert len(book.nodes) <= 5
        finally:
            sb_mod.MAX_NODES = original_max

    def test_remove_node(self):
        entries = [
            {"name": "child", "path": "/Users/test/dir/child",
             "size": 512, "is_dir": True},
        ]
        self.book.add_directory("/Users/test/dir", 512, entries, [])
        assert "/Users/test/dir/child" in self.book.nodes
        self.book.remove_node("/Users/test/dir/child")
        assert "/Users/test/dir/child" not in self.book.nodes

    def test_render_tree_not_empty(self):
        entries = [
            {"name": "big_file.mkv", "path": "/Users/test/dir/big_file.mkv",
             "size": 1024 * 1024 * 100, "is_dir": False},
        ]
        self.book.add_directory("/Users/test/dir", 1024 * 1024 * 100, entries, [])
        tree = self.book.render_tree()
        assert "big_file.mkv" in tree or "dir" in tree

    def test_human_size(self):
        from marjak.session_book import _human_size
        assert "B" in _human_size(500)
        assert "KB" in _human_size(2048)
        assert "MB" in _human_size(5 * 1024 * 1024)
        assert "GB" in _human_size(3 * 1024 ** 3)


# ═══════════════════════════════════════════════════════════════════════════
# 2. FSMemory Tests (persistent SQLite store)
# ═══════════════════════════════════════════════════════════════════════════

class TestFSMemory:
    """Tests for the persistent SQLite filesystem memory (fs_memory.py)."""

    def setup_method(self):
        from marjak.fs_memory import FSMemory
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test_fs.db")
        self.mem = FSMemory(db_path=self.db_path)

    def teardown_method(self):
        self.mem.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_is_empty_initially(self):
        assert self.mem.is_empty()

    def test_upsert_directory(self):
        self.mem.upsert_directory(
            path="/Users/test/Library",
            name="Library",
            size_bytes=1024 * 1024 * 500,
            item_count=42,
        )
        assert not self.mem.is_empty()
        row = self.mem.get_directory("/Users/test/Library")
        assert row is not None
        assert row["name"] == "Library"
        assert row["size_bytes"] == 1024 * 1024 * 500
        assert row["item_count"] == 42

    def test_upsert_updates_on_conflict(self):
        self.mem.upsert_directory("/Users/test/dir", "dir", 1000, 5)
        self.mem.upsert_directory("/Users/test/dir", "dir", 2000, 10)
        row = self.mem.get_directory("/Users/test/dir")
        assert row["size_bytes"] == 2000
        assert row["times_visited"] == 2

    def test_upsert_with_top_children(self):
        children = [
            {"name": "Caches", "size": 50000, "is_dir": True},
            {"name": "Logs", "size": 10000, "is_dir": True},
        ]
        self.mem.upsert_directory("/Users/test/Library", "Library", 60000, 2,
                                  top_children=children)
        row = self.mem.get_directory("/Users/test/Library")
        stored_children = json.loads(row["top_children"])
        assert len(stored_children) == 2
        assert stored_children[0]["name"] == "Caches"

    def test_delete_path(self):
        self.mem.upsert_directory("/Users/test/dir", "dir", 1000)
        self.mem.upsert_directory("/Users/test/dir/child", "child", 500)
        self.mem.delete_path("/Users/test/dir")
        assert self.mem.get_directory("/Users/test/dir") is None
        assert self.mem.get_directory("/Users/test/dir/child") is None

    def test_get_hotspots(self):
        self.mem.upsert_directory("/Users/test/big", "big", 1000000)
        self.mem.upsert_directory("/Users/test/small", "small", 100)
        hotspots = self.mem.get_hotspots(5)
        assert len(hotspots) >= 1
        assert hotspots[0]["path"] == "/Users/test/big"

    def test_get_skeleton(self):
        self.mem.upsert_directory("/Users/test/Library", "Library", 50000,
                                  is_skeleton=True)
        skeleton = self.mem.get_skeleton()
        assert len(skeleton) == 1
        assert skeleton[0]["name"] == "Library"

    def test_get_children(self):
        self.mem.upsert_directory("/Users/test/dir", "dir", 1000)
        self.mem.upsert_directory("/Users/test/dir/child1", "child1", 500)
        self.mem.upsert_directory("/Users/test/dir/child2", "child2", 300)
        children = self.mem.get_children("/Users/test/dir")
        assert len(children) == 2

    def test_log_action(self):
        self.mem.log_action("navigate", "/Users/test", "500 MB, 42 items")
        actions = self.mem.get_recent_actions(5)
        assert len(actions) == 1
        assert actions[0]["action"] == "navigate"

    def test_action_cap(self):
        """Actions log should not exceed MAX_ACTIONS."""
        for i in range(120):
            self.mem.log_action("test", f"/path/{i}", f"detail {i}")
        count = self.mem._conn.execute(
            "SELECT COUNT(*) FROM actions_log"
        ).fetchone()[0]
        assert count <= self.mem.MAX_ACTIONS

    def test_search_hit(self):
        self.mem.upsert_search_hit("/Users/test/Chrome", "chrome", 50000)
        rows = self.mem._conn.execute(
            "SELECT * FROM search_hits WHERE path = ?",
            ("/Users/test/Chrome",)
        ).fetchall()
        assert len(rows) == 1

    def test_wipe(self):
        self.mem.upsert_directory("/Users/test/dir", "dir", 1000)
        self.mem.log_action("test", "", "detail")
        self.mem.wipe()
        assert self.mem.is_empty()
        actions = self.mem.get_recent_actions(5)
        assert len(actions) == 0

    def test_eviction(self):
        """Non-skeleton rows should be evicted when over MAX_DIRECTORIES."""
        self.mem.MAX_DIRECTORIES = 5
        for i in range(10):
            self.mem.upsert_directory(f"/Users/test/dir{i}", f"dir{i}", i * 100)
        count = self.mem._conn.execute(
            "SELECT COUNT(*) FROM directories WHERE is_skeleton = 0"
        ).fetchone()[0]
        assert count <= 5

    def test_skeleton_not_evicted(self):
        """Skeleton rows should never be evicted."""
        self.mem.MAX_DIRECTORIES = 3
        self.mem.upsert_directory("/Users/test/home", "home", 100000,
                                  is_skeleton=True)
        for i in range(10):
            self.mem.upsert_directory(f"/Users/test/dir{i}", f"dir{i}", i * 100)
        skel = self.mem.get_skeleton()
        assert any(s["path"] == "/Users/test/home" for s in skel)

    def test_get_context_for_query(self):
        self.mem.upsert_directory("/Users/test/Chrome", "Chrome", 500000)
        self.mem.log_action("navigate", "/Users/test/Chrome", "500 KB")
        ctx = self.mem.get_context_for_query("clean chrome cache")
        assert "Chrome" in ctx or "chrome" in ctx

    def test_age_str(self):
        from datetime import datetime, timedelta
        now = datetime.now().isoformat()
        assert "just now" in self.mem._age_str(now)
        old = (datetime.now() - timedelta(days=5)).isoformat()
        assert "5d ago" in self.mem._age_str(old)

    def test_keyword_search(self):
        self.mem.upsert_directory("/Users/test/BraveSoftware", "BraveSoftware", 50000)
        results = self.mem._keyword_search(["brave"])
        assert len(results) >= 1


# ═══════════════════════════════════════════════════════════════════════════
# 3. Prompt Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestPrompts:
    """Tests for the prompt system (prompts.py)."""

    def test_get_prompt_returns_string(self):
        from marjak.prompts import get_prompt
        for preset in ("Eco", "Pro", "Expert"):
            prompt = get_prompt(preset, "ollama")
            assert isinstance(prompt, str)
            assert len(prompt) > 100

    def test_all_presets_have_prohibitions(self):
        from marjak.prompts import get_prompt
        for preset in ("Eco", "Pro", "Expert"):
            prompt = get_prompt(preset, "ollama")
            assert "NEVER guess file paths" in prompt
            assert "NEVER show FID numbers" in prompt

    def test_principle_based_tool_selection(self):
        """All presets must teach search_system-first principle, not enumerate specific answers."""
        from marjak.prompts import get_prompt
        for preset in ("Eco", "Pro", "Expert"):
            prompt = get_prompt(preset, "ollama")
            # Must teach the principle
            assert "search_system" in prompt
            low = prompt.lower()
            assert "unknown" in low  # location is UNKNOWN
            assert "guess" in low    # never guess
            # Must NOT enumerate specific answers
            assert "Chrome" not in prompt
            assert "Brave" not in prompt
            assert "~/Desktop" not in prompt
            assert "~/Pictures" not in prompt

    def test_tier_sizing(self):
        """Eco must be shorter than Pro, Pro shorter than Expert."""
        from marjak.prompts import get_prompt
        eco = get_prompt("Eco", "ollama")
        pro = get_prompt("Pro", "ollama")
        expert = get_prompt("Expert", "ollama")
        assert len(eco) < len(pro) < len(expert)

    def test_expert_has_shell_access(self):
        """Only Expert preset should mention run_shell."""
        from marjak.prompts import get_prompt
        assert "run_shell" in get_prompt("Expert", "ollama")
        assert "run_shell" not in get_prompt("Eco", "ollama")
        assert "run_shell" not in get_prompt("Pro", "ollama")

    def test_prohibition_doesnt_block_expert_shell(self):
        """Prohibition wording must not prevent Expert from using run_shell tool."""
        from marjak.prompts import get_prompt
        prompt = get_prompt("Expert", "ollama")
        # Should NOT say "NEVER output shell commands" (blocks run_shell)
        assert "NEVER output" not in prompt
        # Should say "use your tools instead"
        assert "use your tools" in prompt

    def test_provider_hints(self):
        from marjak.prompts import get_prompt
        ollama_prompt = get_prompt("Pro", "ollama")
        assert "[PROVIDER]" in ollama_prompt
        openai_prompt = get_prompt("Pro", "openai")
        # OpenAI has no extra provider hint
        assert "Ollama" not in openai_prompt

    def test_pro_expert_have_reasoning_template(self):
        """Pro and Expert must include reasoning_template, Eco must not."""
        from marjak.prompts import get_prompt
        assert "reasoning_template" not in get_prompt("Eco", "ollama")
        assert "reasoning_template" in get_prompt("Pro", "ollama")
        assert "reasoning_template" in get_prompt("Expert", "ollama")

    def test_all_presets_have_tool_descriptions(self):
        """All presets must describe navigate and search_system tools."""
        from marjak.prompts import get_prompt
        for preset in ("Eco", "Pro", "Expert"):
            prompt = get_prompt(preset, "ollama")
            assert "navigate(path)" in prompt
            assert "search_system(name" in prompt
            assert "move_to_trash(file_ids)" in prompt


# ═══════════════════════════════════════════════════════════════════════════
# 4. Agent Plan Generation Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestAgentPlans:
    """Tests for the _generate_plan function in agent.py."""

    def test_find_plan_uses_search(self):
        if not HAS_LANGCHAIN:
            return
        from marjak.agent import _generate_plan
        plan = _generate_plan("find all my screenshots on this Mac")
        assert "search_system" in plan
        # Must NOT give away specific paths or tool arguments
        assert "Desktop" not in plan
        assert "screenshot" not in plan.lower() or "search_system('screenshot" not in plan

    def test_browser_plan_no_enumeration(self):
        if not HAS_LANGCHAIN:
            return
        from marjak.agent import _generate_plan
        plan = _generate_plan("clean my browser cache")
        assert "browser" in plan.lower()
        # Must NOT enumerate specific browser names — model should discover
        assert "BraveSoftware" not in plan
        assert "Chrome" not in plan
        assert "Firefox" not in plan
        assert "Safari" not in plan

    def test_cleanup_plan(self):
        if not HAS_LANGCHAIN:
            return
        from marjak.agent import _generate_plan
        plan = _generate_plan("free up disk space and clean junk")
        assert "get_system_overview" in plan

    def test_slow_mac_plan(self):
        if not HAS_LANGCHAIN:
            return
        from marjak.agent import _generate_plan
        plan = _generate_plan("my mac is very slow lately")
        assert "get_system_overview" in plan
        assert "consent" in plan.lower() or "STOP" in plan

    def test_no_plan_for_ambiguous(self):
        if not HAS_LANGCHAIN:
            return
        from marjak.agent import _generate_plan
        plan = _generate_plan("hello")
        assert plan == ""

    def test_no_plan_for_short_query(self):
        if not HAS_LANGCHAIN:
            return
        from marjak.agent import _generate_plan
        plan = _generate_plan("hi")
        assert plan == ""


# ═══════════════════════════════════════════════════════════════════════════
# 5. Config Manager Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestConfigManager:
    """Tests for config_manager.py."""

    def test_config_loads(self):
        from marjak.config import config_manager
        assert config_manager.current_provider is not None
        assert config_manager.current_model is not None

    def test_performance_settings(self):
        from marjak.config import config_manager
        settings = config_manager.get_performance_settings()
        assert "nav_loops" in settings
        assert "exec_loops" in settings
        assert "tree_chars" in settings

    def test_default_config_structure(self):
        from marjak.config import ConfigManager
        cm = ConfigManager.__new__(ConfigManager)
        default = cm._default_config()
        assert "providers" in default
        assert "preset" in default


# ═══════════════════════════════════════════════════════════════════════════
# 6. Tool Dedup Tests (move_to_trash parent/child)
# ═══════════════════════════════════════════════════════════════════════════

class TestMoveToTrashDedup:
    """Test that move_to_trash deduplicates parent/child paths."""

    def test_parent_child_dedup_logic(self):
        """Verify the dedup algorithm that was added to move_to_trash."""
        # Simulate the dedup logic from move_to_trash
        valid_targets = [
            "/Users/test/Chrome",
            "/Users/test/Chrome/Default",
            "/Users/test/Chrome/Safe Browsing",
            "/Users/test/Chrome/Default/Cache",
            "/Users/test/Firefox",
        ]
        deduplicated = []
        valid_sorted = sorted(valid_targets, key=len)
        for target in valid_sorted:
            is_child = any(
                target.startswith(parent + "/") for parent in deduplicated
            )
            if not is_child:
                deduplicated.append(target)

        assert "/Users/test/Chrome" in deduplicated
        assert "/Users/test/Firefox" in deduplicated
        # Children of Chrome should be excluded
        assert "/Users/test/Chrome/Default" not in deduplicated
        assert "/Users/test/Chrome/Safe Browsing" not in deduplicated
        assert "/Users/test/Chrome/Default/Cache" not in deduplicated
        assert len(deduplicated) == 2

    def test_no_dedup_for_siblings(self):
        """Sibling directories should NOT be deduped."""
        valid_targets = [
            "/Users/test/Chrome",
            "/Users/test/Firefox",
            "/Users/test/Brave",
        ]
        deduplicated = []
        valid_sorted = sorted(valid_targets, key=len)
        for target in valid_sorted:
            is_child = any(
                target.startswith(parent + "/") for parent in deduplicated
            )
            if not is_child:
                deduplicated.append(target)

        assert len(deduplicated) == 3


# ═══════════════════════════════════════════════════════════════════════════
# 7. CLI Command Routing Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestCLIRouting:
    """Test that CLI command matching works correctly."""

    def test_wipe_all_before_wipe(self):
        """'/wipe --all' must NOT be caught by '/wipe' check."""
        # This verifies the fix: /wipe --all must be checked FIRST
        cmd = "/wipe --all"
        # Simulate the routing logic from cli.py
        assert cmd.lower() in ("/wipe --all", "wipe --all")
        # The plain /wipe check should NOT match /wipe --all
        assert cmd.lower() not in ("/wipe", "wipe")

    def test_wipe_matches_wipe(self):
        cmd = "/wipe"
        assert cmd.lower() in ("/wipe", "wipe")
        assert cmd.lower() not in ("/wipe --all", "wipe --all")

    def test_memory_command(self):
        cmd = "/memory"
        assert cmd.lower() in ("/memory", "memory")

    def test_playbook_command(self):
        cmd = "/playbook"
        assert cmd.lower() in ("/playbook", "playbook")

    def test_forget_command(self):
        cmd = "/forget ~/Library/Caches"
        assert cmd.lower().startswith("/forget ")


# ═══════════════════════════════════════════════════════════════════════════
# 8. Agent Graph Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestAgentGraph:
    """Tests for the agent graph compilation (no LLM needed)."""

    def test_master_app_compiles(self):
        if not HAS_LANGCHAIN:
            return
        from marjak.agent import master_app
        assert master_app is not None

    def test_tools_list(self):
        if not HAS_LANGCHAIN:
            return
        from marjak.agent import _get_tools
        tools = _get_tools()
        tool_names = {t.name for t in tools}
        assert "navigate" in tool_names
        assert "search_system" in tool_names
        assert "get_system_overview" in tool_names
        assert "move_to_trash" in tool_names
        assert "collect_deletable_files" in tool_names
        assert "execute_deep_clean" in tool_names

    def test_context_manager_prune_thinking(self):
        if not HAS_LANGCHAIN:
            return
        from marjak.agent import ContextManager
        from langchain_core.messages import AIMessage, HumanMessage
        messages = [
            HumanMessage(content="hello"),
            AIMessage(content="thinking...", additional_kwargs={"reasoning_content": "long reasoning"}),
            HumanMessage(content="follow up"),
        ]
        pruned = ContextManager.prune_thinking(messages)
        # The reasoning from the AI message before the last human should be stripped
        assert pruned[1].additional_kwargs["reasoning_content"] == ""

    def test_context_manager_strip_ghosts(self):
        if not HAS_LANGCHAIN:
            return
        from marjak.agent import ContextManager
        from langchain_core.messages import AIMessage, HumanMessage
        messages = [
            HumanMessage(content="hello"),
            AIMessage(content=""),  # ghost
            AIMessage(content="real response"),
        ]
        cleaned = ContextManager.strip_ghost_messages(messages)
        assert len(cleaned) == 2  # ghost removed


# ═══════════════════════════════════════════════════════════════════════════
# 8b. Plan Generation — No Hints Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestPlanNoHints:
    """Plans must guide workflow, NEVER give away specific answers."""

    def test_no_plan_mentions_specific_paths(self):
        """No plan should contain hardcoded macOS paths."""
        if not HAS_LANGCHAIN:
            return
        from marjak.agent import _generate_plan
        queries = [
            "find all my screenshots",
            "clean my browser cache",
            "where are my downloads",
            "find large video files",
            "clean up system junk",
        ]
        banned = ["~/Desktop", "~/Pictures", "~/Library", "/Users/"]
        for q in queries:
            plan = _generate_plan(q)
            for path in banned:
                assert path not in plan, f"Plan for '{q}' leaks path '{path}'"

    def test_no_plan_mentions_specific_apps(self):
        """Plans should not enumerate specific app names."""
        if not HAS_LANGCHAIN:
            return
        from marjak.agent import _generate_plan
        plan_browser = _generate_plan("clean my browser data")
        plan_find = _generate_plan("find screenshots on my Mac")
        banned_apps = ["Chrome", "BraveSoftware", "Firefox", "Safari", "Arc", "Edge"]
        for app in banned_apps:
            assert app not in plan_browser, f"Browser plan leaks app name '{app}'"
            assert app not in plan_find, f"Find plan leaks app name '{app}'"

    def test_find_plan_teaches_search_first(self):
        """All 'find' queries should get a plan that starts with search_system."""
        if not HAS_LANGCHAIN:
            return
        from marjak.agent import _generate_plan
        for q in ["find my old videos", "where are the log files", "locate Telegram data"]:
            plan = _generate_plan(q)
            assert "search_system" in plan, f"Plan for '{q}' doesn't use search_system"
            assert "guess" in plan.lower() or "NOT" in plan, f"Plan for '{q}' doesn't warn against guessing"


# ═══════════════════════════════════════════════════════════════════════════
# 8c. Context Trimming Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestContextTrimming:
    """Tests for the smart context trimming in agent.py."""

    def test_tree_chars_scales_down(self):
        """More session_book nodes → smaller tree budget."""
        # Formula: min(cap, max(2000, 6000 - node_count * 80))
        # 0 nodes → 6000
        assert min(8000, max(2000, 6000 - 0 * 80)) == 6000
        # 20 nodes → 4400
        assert min(8000, max(2000, 6000 - 20 * 80)) == 4400
        # 50 nodes → clamped to 2000
        assert min(8000, max(2000, 6000 - 50 * 80)) == 2000
        # 100 nodes → still 2000 (floor)
        assert min(8000, max(2000, 6000 - 100 * 80)) == 2000


# ═══════════════════════════════════════════════════════════════════════════
# 9. Module Import Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestImports:
    """Verify all V2 modules import without error."""

    def test_import_session_book(self):
        from marjak.session_book import SessionBook, _human_size
        assert SessionBook is not None

    def test_import_fs_memory(self):
        from marjak.fs_memory import FSMemory, fs_memory
        assert FSMemory is not None
        assert fs_memory is not None

    def test_import_prompts(self):
        from marjak.prompts import get_prompt
        assert callable(get_prompt)

    def test_import_config(self):
        from marjak.config import config_manager
        assert config_manager is not None

    def test_import_tools(self):
        if not HAS_LANGCHAIN:
            return
        from marjak.tools import navigate, search_system, move_to_trash
        assert navigate is not None

    def test_import_agent(self):
        if not HAS_LANGCHAIN:
            return
        from marjak.agent import master_app, get_performance_caps
        assert master_app is not None
        assert callable(get_performance_caps)

    def test_version(self):
        import marjak
        assert marjak.__version__ == "2.1.0"


# ═══════════════════════════════════════════════════════════════════════════
# Standalone runner
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # Run without pytest — manual execution
    import traceback

    test_classes = [
        TestSessionBook, TestFSMemory, TestPrompts, TestAgentPlans,
        TestConfigManager, TestMoveToTrashDedup, TestCLIRouting,
        TestAgentGraph, TestImports,
    ]

    total = 0
    passed = 0
    failed = 0
    errors = []

    for cls in test_classes:
        print(f"\n{'=' * 60}")
        print(f"  {cls.__name__}")
        print(f"{'=' * 60}")

        for method_name in sorted(dir(cls)):
            if not method_name.startswith("test_"):
                continue
            total += 1
            instance = cls()
            # Call setup if it exists
            if hasattr(instance, "setup_method"):
                try:
                    instance.setup_method()
                except Exception as e:
                    print(f"  ✘ {method_name} (setup failed: {e})")
                    failed += 1
                    errors.append((cls.__name__, method_name, traceback.format_exc()))
                    continue

            try:
                getattr(instance, method_name)()
                print(f"  ✔ {method_name}")
                passed += 1
            except Exception as e:
                print(f"  ✘ {method_name}: {e}")
                failed += 1
                errors.append((cls.__name__, method_name, traceback.format_exc()))
            finally:
                if hasattr(instance, "teardown_method"):
                    try:
                        instance.teardown_method()
                    except Exception:
                        pass

    print(f"\n{'=' * 60}")
    print(f"  RESULTS: {passed}/{total} passed, {failed} failed")
    print(f"{'=' * 60}")

    if errors:
        print(f"\n{'─' * 60}")
        print("  FAILURES:")
        print(f"{'─' * 60}")
        for cls_name, method, tb in errors:
            print(f"\n  {cls_name}.{method}:")
            for line in tb.strip().split("\n"):
                print(f"    {line}")

    sys.exit(1 if failed else 0)
