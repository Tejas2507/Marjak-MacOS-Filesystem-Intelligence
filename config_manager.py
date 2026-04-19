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
            "version": "1.0.0",
            "active_provider": "ollama",
            "active_model": "gemma4",
            "preset": "Pro",
            "providers": {
                "ollama": {"model": "gemma4"},
                "openai": {"model": "gpt-4o-mini", "api_key": ""},
                "gemini": {"model": "gemini-1.5-flash", "api_key": ""},
                "claude": {"model": "claude-3-5-sonnet-20240620", "api_key": ""},
                "groq": {"model": "llama-3.3-70b-versatile", "api_key": ""},
                "openrouter": {
                    "model": "google/gemini-2.0-flash-exp:free", 
                    "api_key": "",
                    "base_url": "https://openrouter.ai/api/v1"
                }
            },
            "performance": {
                "Eco": {"nav_loops": 5, "exec_loops": 2, "tree_chars": 6000},
                "Pro": {"nav_loops": 10, "exec_loops": 5, "tree_chars": 10000},
                "Expert": {"nav_loops": 20, "exec_loops": 10, "tree_chars": 15000}
            }
        }

    def save(self):
        try:
            os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
            with open(CONFIG_PATH, "w") as f:
                json.dump(self.config, f, indent=4)
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
