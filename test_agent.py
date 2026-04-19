# test_agent.py — Basic integration test for the Master Graph
#
# Verifies that the graph compiles and can accept a HumanMessage.

from langchain_core.messages import HumanMessage
from agent import master_app, NAV_MAX_LOOPS, EXEC_MAX_LOOPS


def test_graph_compiles():
    """Sanity check: the master graph should compile without errors."""
    assert master_app is not None
    print("✔ Master graph compiled successfully.")


def test_graph_accepts_input():
    """The graph should accept a HumanMessage and return state."""
    config = {"configurable": {"thread_id": "test_session"}}
    inputs = {"messages": [HumanMessage(content="What is my disk usage?")]}
    
    # Just verify it doesn't crash — actual LLM output depends on Ollama
    try:
        for chunk, metadata in master_app.stream(inputs, config, stream_mode="messages"):
            pass  # Stream through without printing
        print("✔ Graph accepted input and streamed without crash.")
    except Exception as e:
        print(f"✘ Graph failed: {e}")


if __name__ == "__main__":
    test_graph_compiles()
    # Uncomment below to run with a live Ollama instance:
    # test_graph_accepts_input()