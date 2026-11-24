"""Root's tests: python3 -m unittest

Importing this package first points HOME at a throwaway folder, so no test ever touches the real
~/.rootcode, and turns colour off so output is plain text.
"""

import os
import tempfile

HOME = tempfile.mkdtemp(prefix="rootcode-test-home-")
os.environ["HOME"] = HOME
os.environ["NO_COLOR"] = "1"
os.environ["COLUMNS"] = "100"
for name in ("ROOTCODE_PROVIDER", "ROOTCODE_MODEL", "ROOTCODE_BASE_URL", "ROOTCODE_THINK",
             "ROOTCODE_COMPACT_TOKENS", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY",
             "ROOTCODE_LOCAL_KEY", "OLLAMA_HOST"):
    os.environ.pop(name, None)
