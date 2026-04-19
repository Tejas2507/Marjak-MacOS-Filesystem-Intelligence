class Marjaka < Formula
  desc "Mārjak: AI-powered macOS filesystem intelligence and maintenance agent"
  homepage "https://github.com/tejas/marjak"
  url "https://github.com/tejas/marjak/archive/refs/tags/v1.0.0.tar.gz"
  sha256 "REPLACE_WITH_ACTUAL_SHA256_AFTER_RELEASE"
  license "MIT"

  depends_on "python@3.12"
  depends_on "mole" # Requires the 'mole' cleaning utility

  def install
    virtualenv_install_with_resources
  end

  test do
    system "#{bin}/marjaka", "--help"
  end
end
