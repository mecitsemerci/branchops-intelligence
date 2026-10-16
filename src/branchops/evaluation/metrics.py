"""Pure metric functions used by the evaluation and calibration reports (no I/O, deterministic)."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PRF:
    """Precision / recall / F1 over set-valued predictions, from summed true/false positives/negatives."""

    tp: int = 0
    fp: int = 0
    fn: int = 0

    def __add__(self, other: PRF) -> PRF:
        return PRF(self.tp + other.tp, self.fp + other.fp, self.fn + other.fn)

    @property
    def precision(self) -> float | None:
        return _ratio(self.tp, self.tp + self.fp)

    @property
    def recall(self) -> float | None:
        return _ratio(self.tp, self.tp + self.fn)

    @property
    def f1(self) -> float | None:
        p, r = self.precision, self.recall
        if p is None or r is None or p + r == 0:
            return None if p is None or r is None else 0.0
        return 2 * p * r / (p + r)

    def as_dict(self) -> dict[str, float | int | None]:
        return {
            "tp": self.tp,
            "fp": self.fp,
            "fn": self.fn,
            "precision": _round(self.precision),
            "recall": _round(self.recall),
            "f1": _round(self.f1),
        }


def set_prf(predicted: Iterable[Hashable], expected: Iterable[Hashable]) -> PRF:
    p, e = set(predicted), set(expected)
    return PRF(tp=len(p & e), fp=len(p - e), fn=len(e - p))


def accuracy(pairs: Sequence[tuple[Hashable, Hashable]]) -> float | None:
    """Share of ``(predicted, expected)`` pairs that are equal; ``None`` for an empty input."""
    return _ratio(sum(1 for p, e in pairs if p == e), len(pairs))


def macro_f1(pairs: Sequence[tuple[Hashable, Hashable]]) -> float | None:
    """Unweighted mean of per-label F1 over every label that occurs as expected or predicted."""
    labels = {e for _, e in pairs} | {p for p, _ in pairs}
    if not labels:
        return None
    scores: list[float] = []
    for label in labels:
        tp = sum(1 for p, e in pairs if p == label and e == label)
        fp = sum(1 for p, e in pairs if p == label and e != label)
        fn = sum(1 for p, e in pairs if p != label and e == label)
        f1 = PRF(tp, fp, fn).f1
        scores.append(f1 or 0.0)
    return sum(scores) / len(scores)


def confusion_matrix(
    pairs: Sequence[tuple[Hashable, Hashable]], labels: Sequence[Hashable] | None = None
) -> dict[str, dict[str, int]]:
    """``{expected: {predicted: count}}`` with string keys (JSON friendly), in ``labels`` order."""
    order = list(labels) if labels is not None else sorted({str(x) for pair in pairs for x in pair})
    counts = Counter((str(e), str(p)) for p, e in pairs)
    keys = [str(label) for label in order]
    return {e: {p: counts.get((e, p), 0) for p in keys} for e in keys}


def mean(values: Iterable[float]) -> float | None:
    items = list(values)
    return sum(items) / len(items) if items else None


def mean_absolute_error(pairs: Sequence[tuple[float, float]]) -> float | None:
    return mean(abs(p - e) for p, e in pairs)


def percentile(values: Iterable[float], q: float) -> float | None:
    """Nearest-rank percentile (q in 0..100); deterministic, no interpolation."""
    items = sorted(values)
    if not items:
        return None
    rank = max(1, math.ceil(q / 100 * len(items)))
    return items[rank - 1]


def cohen_kappa(
    pairs: Sequence[tuple[Hashable, Hashable]], weights: Mapping[Any, int] | None = None
) -> float | None:
    """Cohen's kappa; with ``weights`` (label -> ordinal) the linearly weighted variant for ordered labels."""
    n = len(pairs)
    if n == 0:
        return None
    labels = sorted({x for pair in pairs for x in pair}, key=str)
    if len(labels) == 1:
        return 1.0
    pred = Counter(p for p, _ in pairs)
    exp = Counter(e for _, e in pairs)

    def disagreement(a: Hashable, b: Hashable) -> float:
        if weights is None:
            return 0.0 if a == b else 1.0
        span = max(weights.values()) - min(weights.values()) or 1
        return abs(weights[a] - weights[b]) / span

    observed = sum(disagreement(p, e) for p, e in pairs) / n
    expected = sum(pred[a] * exp[b] * disagreement(a, b) for a in labels for b in labels) / (n * n)
    if expected == 0:
        return 1.0
    return 1 - observed / expected


def _ratio(num: int, den: int) -> float | None:
    return num / den if den else None


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(value, digits)


def rounded(value: float | None, digits: int = 4) -> float | None:
    return _round(value, digits)
