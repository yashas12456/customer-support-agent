import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("SUPPORT_DATA_DIR", ROOT / "data"))
RUNTIME_DIR = Path(os.getenv("SUPPORT_RUNTIME_DIR", ROOT / "runtime"))

MODEL = os.getenv("SUPPORT_AGENT_MODEL", "claude-sonnet-5-5")
MAX_TOOL_ROUNDS = 5          # hard cap on model <-> tool loops per user message
MAX_INPUT_CHARS = 2000       # longer messages are rejected
MAX_HISTORY_TURNS = 12       # conversation memory sent to the model
