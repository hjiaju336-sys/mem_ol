from __future__ import annotations

from typing import Literal

ENTITY_TYPES = {
    "character",
    "location",
    "item",
    "faction",
    "event",
    "concept",
    "other",
}

EntityType = Literal[
    "character",
    "location",
    "item",
    "faction",
    "event",
    "concept",
    "other",
]

CHANNELS = {"narrative", "meta", "mixed"}

INTENTS = {
    "action",
    "investigate",
    "social",
    "combat",
    "daily",
    "cultivation",
    "inquiry",
    "default",
}

META_KINDS = {"instruction", "question"}

DIRECTIVE_VALUES = {
    "length": {"short", "medium", "long"},
    "pace": {"fast", "measured", "slow"},
    "focus": {"action", "dialogue", "emotion", "environment", "plot"},
    "options": {"none", "few", "open", "tactical"},
    "tone": {"dark", "light", "humorous", "serious", "warm"},
}

ACTION_KINDS = {
    "attack",
    "defend",
    "move",
    "investigate",
    "social",
    "cultivate",
    "use_item",
    "other",
}
