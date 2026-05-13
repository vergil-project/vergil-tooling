"""Label registry: load the canonical label set from ``data/labels.json``."""

from __future__ import annotations

import json
from importlib import resources
from typing import Any


def load_labels() -> dict[str, Any]:
    """Return the parsed label registry.

    The returned dict has two keys:

    * ``labels`` — list of dicts with *name*, *color*, and *description*.
    * ``delete`` — list of label names to remove.
    """
    ref = resources.files("standard_tooling.data").joinpath("labels.json")
    text = ref.read_text(encoding="utf-8")
    data: dict[str, Any] = json.loads(text)
    return data
