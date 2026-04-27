# config_manager.py — Mārjak Global Configuration Manager
import os
import json

CONFIG_PATH = os.path.expanduser("~/.marjak/config.json")

class ConfigManager:
    """Manages system-wide settings, AI providers, and API keys."""

    def __init__(self):
        self.config = self._load()

    def _load(self) -> dict:
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH) as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                return self._default_config()
        return self._default_config()

    def _default_config(self) -> dict:
        return {
            "version": "1.1.0",
            "active_provider": "ollama",
            "active_model": "gemma4",
            "preset": "Pro",
            "providers": {
                "ollama": {"model": "gemma4", "context_window": 131072},
                "openai": {"model": "gpt-4.1-nano", "api_key": "", "context_window": 1047576},
                "gemini": {"model": "gemini-2.0-flash-lite", "api_key": "", "context_window": 1048576},
                "claude": {"model": "claude-3-5-haiku-20241022", "api_key": "", "context_window": 200000},
                "groq": {"model": "llama-3.3-70b-versatile", "api_key": "", "context_window": 131072},
                "openrouter": {
                    "model": "google/gemini-2.0-flash-lite-001", 
                    "api_key": "",
                    "base_url": "https://openrouter.ai/api/v1",
                    "context_window": 1048576
                }
            },
            "performance": {
                "Eco": {
                    "nav_loops": 5, "exec_loops": 2, "tree_chars": 2500,
                    "max_children": 8, "search_limit": 15,
                    "memory_hotspots": 5, "memory_actions": 5,
                    "summary_interval": 3
                },
                "Pro": {
                    "nav_loops": 10, "exec_loops": 5, "tree_chars": 8000,
                    "max_children": 15, "search_limit": 30,
                    "memory_hotspots": 10, "memory_actions": 10,
                    "summary_interval": 4
                },
                "Expert": {
                    "nav_loops": 20, "exec_loops": 10, "tree_chars": 15000,
                    "max_children": 25, "search_limit": 50,
                    "memory_hotspots": 15, "memory_actions": 15,
                    "summary_interval": 6
                }
            }
        }

    def save(self):
        try:
            os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
            tmp = CONFIG_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.config, f, indent=4)
            os.replace(tmp, CONFIG_PATH)
        except IOError:
            pass

    @property
    def current_provider(self):
        return self.config.get("active_provider", "ollama")

    @property
    def current_model(self):
        return self.config.get("active_model", "gemma4")

    @property
    def api_keys(self):
        return {p: details.get("api_key", "") for p, details in self.config.get("providers", {}).items()}

    # Runtime-detected num_ctx — set by get_llm() after querying Ollama /api/show
    _detected_num_ctx: int = 0

    @property
    def context_window(self) -> int:
        """Returns the effective context window size.

        Priority: detected_num_ctx (from Ollama) > preset override > provider default.
        """
        if self._detected_num_ctx:
            return self._detected_num_ctx
        # Fallback to preset-based num_ctx (matches get_llm logic)
        preset = self.config.get("preset", "Pro")
        preset_ctx = {"Eco": 8192, "Pro": 32768, "Expert": 131072}.get(preset)
        if preset_ctx:
            return preset_ctx
        prov = self.config.get("providers", {}).get(self.current_provider, {})
        return prov.get("context_window", 131072)

    def get_performance_settings(self):
        preset = self.config.get("preset", "Pro")
        return self.config.get("performance", {}).get(preset, self.config["performance"]["Pro"])

    def set_provider(self, provider: str, model: str = None, api_key: str = None):
        self.config["active_provider"] = provider
        if model:
            self.config["active_model"] = model
            self.config["providers"].setdefault(provider, {})["model"] = model
        if api_key:
            self.config["providers"].setdefault(provider, {})["api_key"] = api_key
        self.save()

    def set_preset(self, preset: str):
        if preset in self.config["performance"]:
            self.config["preset"] = preset
            self.save()

# Singleton instance
config_manager = ConfigManager()
