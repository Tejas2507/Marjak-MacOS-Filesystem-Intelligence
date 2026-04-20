# test_all_tools.py — Isolated tool execution tests (no LLM required)
#
# Tests each tool function directly via .invoke() to verify
# they work without the LangGraph state machine.

import time
from tools import (
    navigate,
    mole_scan,
    search_system,
    get_system_overview,
)


def test_tools_bypassing_llm():
    tools_to_test = [
        (get_system_overview, {}),
        (navigate, {"path": "~"}),
        (search_system, {"name": "MoleDummyTest", "file_type": "any"}),
    ]

    print("\n[System]: Commencing isolated tool execution tests...\n")

    for tool_func, args in tools_to_test:
        print(f"--- [Testing Tool]: {tool_func.name} ---")
        
        start_time = time.time()
        
        try:
            result = tool_func.invoke(args)
            execution_time = time.time() - start_time
            
            print(f"Result:\n{result}\n")
            print(f"Time taken: {execution_time:.2f} seconds\n")
            print("-" * 50 + "\n")
            
        except Exception as e:
            print(f"Error executing {tool_func.name}: {str(e)}\n")


if __name__ == "__main__":
    test_tools_bypassing_llm()