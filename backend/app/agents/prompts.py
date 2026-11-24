"""Load an agent's system prompt from backend/agent_prompts/<name>.md.

A prompt may contain {{placeholders}}:
- {{name}} where agent_prompts/shared/<name>.md exists is replaced by that file (text several agents share);
- {{burst}} is the number of screen calls one turn may chain (MAX_BURST in app/bridge/tools.py);
- {{checkpoint_turns}} is how many turns pass between a phone agent's checkpoints (phone_checkpoint_turns).
An unknown placeholder is an error, so a typo never reaches a model.

The file is read on every call, so an edited prompt applies to the next model call without a restart.
What the tools are and take is not written in prompts: it comes from the tool classes (app/tools).
"""

import re
from pathlib import Path

from ..bridge.tools import MAX_BURST

PROMPTS = Path(__file__).resolve().parents[2] / "agent_prompts"
PLACEHOLDER = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")


def values():
    from ..config import settings

    return {"burst": str(MAX_BURST), "checkpoint_turns": str(settings.phone_checkpoint_turns)}


def fill(text, depth=0):
    def replace(match):
        name = match.group(1)
        shared = PROMPTS / "shared" / f"{name}.md"
        if shared.exists() and depth < 3:
            return fill(shared.read_text(encoding="utf-8").strip(), depth + 1)
        if name in values():
            return values()[name]
        raise ValueError(f"Unknown placeholder {{{{{name}}}}} in agent_prompts")

    return PLACEHOLDER.sub(replace, text)


def load(name):
    return fill((PROMPTS / f"{name}.md").read_text(encoding="utf-8")).strip()
