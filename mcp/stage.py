#!/usr/bin/env python3
"""Stage the self-contained handler for the OpenClaw MCP bridge release ZIP."""
from pathlib import Path
import shutil
import sys

root = Path(__file__).resolve().parents[1]
target = Path(sys.argv[1]) / "litellm-free"
shutil.copytree(root / "mcp/litellm-free", target, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
for name in ("refresh-models.sh", "refresh_models.py", "refresh_common.py", "openclaw", "opencode", "hermes-agent"):
    destination = target / "lib/ops" / name
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(root / "ops" / name, destination)
shutil.copy2(root / "llm_model_metadata.py", target / "lib/llm_model_metadata.py")
