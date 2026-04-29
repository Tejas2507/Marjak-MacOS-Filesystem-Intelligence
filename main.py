# main.py - Thin wrapper that calls the real CLI from src/marjak/cli.py
#
# All features live in src/marjak/cli.py. This file exists so that
# `uv run python main.py` keeps working as before.
 
import sys
import os
 
# Ensure src/ is on the path so `marjak` package is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
 
from marjak.cli import main
 
if __name__ == "__main__":
    main()