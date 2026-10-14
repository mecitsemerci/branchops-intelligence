"""Heuristic detection of prompt-injection attempts inside Jira content.

Detection is *defence in depth*: the primary defences are that issue content is passed to the model as
delimited JSON data, the model has no tools, outputs are schema-validated and evidence is verified.
Detected attempts are recorded as security flags and force manual review; they never change the prompt.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from branchops.domain.services.evidence import normalize_for_match

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern, re.IGNORECASE))
    for name, pattern in (
        (
            "ignore_instructions_en",
            r"\b(ignore|disregard|forget)\b.{0,40}\b(previous|prior|above|all)\b.{0,40}"
            r"\b(instructions?|prompts?|rules?)\b",
        ),
        (
            "ignore_instructions_tr",
            r"(önceki|yukarıdaki|tüm|bütün)\s.{0,40}(talimat|komut|kural)\w*\s.{0,40}"
            r"(yoksay|görmezden\sgel|unut|dikkate\salma)",
        ),
        ("role_override_en", r"\byou are now\b|\bact as\b.{0,30}\b(system|admin|developer)\b"),
        ("role_override_tr", r"\bartık\s.{0,30}(sistem|yönetici|admin)\s?(sin|olarak)"),
        ("system_prompt_probe", r"system\s*prompt|sistem\s*(istemi|promptu|talimatı)"),
        (
            "score_manipulation",
            r"(score|puan|skor)\w*\s.{0,30}(100|maksimum|maximum|tam)\s?(ver|give|set|olarak)",
        ),
        ("chat_template_tokens", r"<\|(im_start|im_end|system|assistant|user)\|>|\[/?INST\]|<\/?system>"),
        ("tool_invocation", r"\b(execute|run|çalıştır)\b.{0,30}\b(shell|command|komut|sql|script)\b"),
    )
)


def detect_prompt_injection(texts: Iterable[str]) -> list[str]:
    flags: list[str] = []
    for text in texts:
        if not text:
            continue
        normalized = normalize_for_match(text)
        for name, pattern in _PATTERNS:
            if name not in flags and (pattern.search(text) or pattern.search(normalized)):
                flags.append(name)
    return [f"prompt_injection:{f}" for f in flags]
