"""Loads the decision question and gives it a stable hash for the decision log and evals."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PATH = Path(__file__).parent / "prompts" / "decision.yaml"
OPTIONS = ("CALL", "PUT", "HOLD")


@dataclass(frozen=True)
class DecisionPrompt:
    question: dict[str, Any]
    version: int
    hash: str


def load_prompt(path: Path = DEFAULT_PATH) -> DecisionPrompt:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    question = doc["question"]
    if question.get("type") != "choice" or set(question.get("criteria", {})) != set(OPTIONS):
        raise ValueError(f"{path}: question must be a choice over exactly {OPTIONS}")
    digest = hashlib.sha256(json.dumps(question, sort_keys=True).encode()).hexdigest()[:12]
    return DecisionPrompt(question=question, version=int(doc.get("version", 0)), hash=digest)
