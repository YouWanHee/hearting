"""Readers for the checked-in, redacted Fleet B canonical fixture."""

import json
from pathlib import Path


FIXTURE_DIR = Path(__file__).with_name("fixtures") / "explorer_b"


def _read(name):
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def load_source():
    return _read("source.json")


def load_screens():
    return _read("screens.json")
