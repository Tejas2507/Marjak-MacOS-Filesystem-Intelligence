class Marjak < Formula
  desc "AI-powered macOS filesystem intelligence and maintenance agent"
  homepage "https://github.com/tejas/marjak"
  url "https://github.com/tejas/marjak/archive/refs/tags/v2.0.0.tar.gz"
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

    # Install the marjak package (src layout) into the venv
    system venv/"bin/pip", "install", "--no-cache-dir", "--no-deps", "."

    # Create a wrapper script that runs `python -m marjak`
    (bin/"marjak").write_env_script venv/"bin/python",
      :args => ["-m", "marjak"]
  end

  def caveats
    <<~EOS
      Mārjak requires a running LLM backend. For local inference:

        brew install ollama
        ollama serve &
        ollama pull gemma4

      Then run:  marjak

      On first launch, use /config to set up your AI provider.
    EOS
  end

  test do
    # Verify the package can be imported
    system libexec/"venv/bin/python", "-c", "import marjak; print(marjak.__version__)"
  end
end
