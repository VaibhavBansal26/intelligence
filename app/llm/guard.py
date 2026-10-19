"""NumberGuard (every number in LLM text must exist in the evidence) and untrusted-text sanitisation."""

from __future__ import annotations

import re
from typing import Any

_NUM = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?\s*(%|k\b|K\b|m\b|M\b)?")
_DIGITS = re.compile(r"\d+(?:\.\d+)?")
_UNTRUSTED = re.compile(r"<untrusted>.*?</untrusted>", re.S)
_CTRL = re.compile(r"[\x00-\x1f\x7f​-‏‪-‮⁦-⁩]")
_PII = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[email]"),
    (re.compile(r"\b[0-9a-f]{32,64}\b", re.I), "[id]"),
    (re.compile(r"\+?\d[\d\s().-]{8,}\d"), "[phone]"),
]


def sanitize_untrusted(text: object, max_len: int = 80) -> str:
    """Make free text from a source app safe to embed as DATA in a prompt: no control chars, no angle
    brackets/backticks (so it cannot close our delimiters), collapsed whitespace, bounded length."""
    s = _CTRL.sub(" ", str(text))
    s = re.sub(r"[<>`{}\[\]]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:max_len]


def wrap_untrusted(text: object) -> str:
    return f"<untrusted>{sanitize_untrusted(text)}</untrusted>"


def redact_pii(text: str) -> str:
    for pat, repl in _PII:
        text = pat.sub(repl, text)
    return text


def _variants(v: float) -> set[float]:
    """Ways a faithful writer may print `v`: rounded, as a percentage (ratios only), or abbreviated (k / M, big values only)."""
    out: set[float] = set()
    scales = [v]
    if abs(v) <= 1.0:
        scales.append(v * 100.0)
    for base in scales:
        for d in (0, 1, 2, 3, 4):
            out.add(round(base, d))
    if abs(v) >= 1_000:
        for div in (1_000.0, 1_000_000.0):
            for d in (0, 1, 2):
                out.add(round(v / div, d))
    return {abs(x) for x in out}


def _walk(obj: Any, nums: set[float]) -> None:
    if isinstance(obj, bool) or obj is None:
        return
    if isinstance(obj, int | float):
        nums |= _variants(float(obj))
    elif isinstance(obj, str):
        # digits inside untrusted names must NOT legitimise numbers (an attacker controls them)
        for d in _DIGITS.findall(_UNTRUSTED.sub(" ", obj)):
            nums |= _variants(float(d))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _walk(k, nums)
            _walk(v, nums)
    elif isinstance(obj, list | tuple):
        for v in obj:
            _walk(v, nums)


class NumberGuard:
    """Rejects narratives that contain numbers not derivable from the structured evidence."""

    def __init__(self, evidence: Any, allow_ranks_up_to: int = 0) -> None:
        self._allowed: set[float] = set()
        _walk(evidence, self._allowed)
        self._allowed |= {float(i) for i in range(1, allow_ranks_up_to + 1)}

    @staticmethod
    def extract(text: str) -> list[float]:
        out: list[float] = []
        for m in _NUM.finditer(text):
            whole = m.group(1).replace(",", "")
            out.append(float(f"{whole}.{m.group(2)}" if m.group(2) else whole))
        return out

    def violations(self, text: str) -> list[float]:
        return [n for n in self.extract(text) if abs(n) not in self._allowed]

    def ok(self, text: str) -> bool:
        return not self.violations(text)
