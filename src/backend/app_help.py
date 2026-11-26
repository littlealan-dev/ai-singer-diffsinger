"""How-to help for the chat model's `app_help` tool (docs/specs/app-help-tool-spec.md).

The help file has one `## <topic>` section per topic; the topics, in file order, are the tool's
enum. Credit rates are placeholders filled from the pricing code, so the help never quotes a
price the backend doesn't charge.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List

from src.backend import synthesis_pricing

APP_HELP_PATH = Path(__file__).resolve().parent / "config" / "app_help.md"
APP_HELP_SECTION_LIMIT = 1500

_TOPIC = re.compile(r"^[a-z][a-z_]*$")
_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


def _number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def _rates() -> Dict[str, str]:
    return {
        "vocal_credit_seconds": _number(synthesis_pricing.VOCAL_CREDIT_DURATION_SECONDS),
        "instrumental_credit_minutes": _number(synthesis_pricing.INSTRUMENTAL_CREDIT_DURATION_SECONDS / 60),
        "mix_credits_per_file": _number(synthesis_pricing.MIX_CREDITS_PER_FILE),
    }


def load_app_help(path: Path = APP_HELP_PATH) -> Dict[str, str]:
    """Each topic's help text, with its rates filled. A malformed file fails loudly at startup."""
    sections: Dict[str, List[str]] = {}
    topic = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            topic = line[3:].strip()
            if not _TOPIC.match(topic) or topic in sections:
                raise ValueError(f"Bad or repeated help topic: {topic!r}")
            sections[topic] = []
        elif topic is None:
            if line.strip():
                raise ValueError("Help text before the first topic")
        else:
            sections[topic].append(line)

    rates = _rates()

    def fill(match: re.Match) -> str:
        if match.group(1) not in rates:
            raise ValueError(f"Unknown help placeholder: {match.group(0)}")
        return rates[match.group(1)]

    help_text = {name: _PLACEHOLDER.sub(fill, "\n".join(lines).strip()) for name, lines in sections.items()}
    for name, text in help_text.items():
        if not text or len(text) > APP_HELP_SECTION_LIMIT:
            raise ValueError(f"Help topic {name!r} is empty or longer than {APP_HELP_SECTION_LIMIT} characters")
    return help_text


APP_HELP = load_app_help()
APP_HELP_TOPICS = list(APP_HELP)
