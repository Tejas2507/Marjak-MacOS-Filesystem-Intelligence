class Marjaka < Formula
  desc "AI-powered macOS filesystem intelligence and maintenance agent"
  homepage "https://github.com/tejas/marjak"
  url "https://github.com/tejas/marjak/archive/refs/tags/v1.0.0.tar.gz"
  sha256 "REPLACE_WITH_ACTUAL_SHA256_AFTER_TAGGING"
  license "MIT"
  head "https://github.com/tejas/marjak.git", branch: "main"

  depends_on "python@3.12"
  depends_on "mole"
  depends_on :macos

  def install
    # Create a self-contained virtualenv inside the Homebrew prefix
    venv = libexec/"venv"
    system "python3.12", "-m", "venv", venv.to_s

    # Install Python dependencies into the venv
    system venv/"bin/pip", "install", "--no-cache-dir",
      "langchain-core>=1.2.28",
      "langchain-ollama>=1.1.0",
      "langchain-openai>=1.1.12",
      "langchain-anthropic>=1.4.0",
      "langchain-google-genai>=4.2.1",
      "langchain-groq>=1.1.2",
      "langgraph>=1.1.6",
      "prompt-toolkit>=3.0.52",
      "pydantic>=2.12.5",
      "pyyaml>=6.0",
      "rich>=13.7.0"

    # Copy the application source files into libexec
    libexec.install "main.py", "agent.py", "tools.py", "prompts.py",
                    "knowledge_book.py", "config_manager.py",
                    "guidebook.py", "macos_guidebook.yaml",
                    "pyproject.toml"

    # Create a wrapper script that runs main.py with the venv Python
    (bin/"marjaka").write_env_script venv/"bin/python",
      "PYTHONPATH" => libexec.to_s,
      :args        => [libexec/"main.py"]
  end

  def caveats
    <<~EOS
      Mārjak requires a running LLM backend. For local inference:

        brew install ollama
        ollama serve &
        ollama pull gemma4

      Then run:  marjaka

      On first launch, use /config to set up your AI provider.
    EOS
  end

  test do
    # Verify the wrapper script exists and Python can import our modules
    assert_match "Mārjak", shell_output("#{bin}/marjaka --help 2>&1", 1)
  end
end
