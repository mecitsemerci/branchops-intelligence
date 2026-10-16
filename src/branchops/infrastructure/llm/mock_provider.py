"""Deterministic rule-based model provider for local development and CI.

It understands the prompts produced by the agents (delimited JSON blocks) and answers with schema-valid
JSON derived from Turkish keyword rules. It is NOT a substitute for the real model and is refused by the
settings validator in production.
"""

from __future__ import annotations

import json
import re
from typing import Any

from branchops.agents.prompts import parse_issue_data_block, parse_json_block
from branchops.application.interfaces.model_provider import (
    CompletionRequest,
    CompletionResult,
    ModelHealth,
    TokenUsage,
)
from branchops.config.settings import ModelCapabilities

MOCK_MODEL_ID = "mock-rule-based"

_CATEGORY_RULES: list[tuple[str, str, tuple[str, ...]]] = [
    ("PERIPHERAL_DEVICE", "CARD_READER", ("kart okuyucu", "kart okumuyor", "card reader")),
    ("PERIPHERAL_DEVICE", "PASSBOOK_PRINTER", ("pasbook", "hesap cüzdanı yazıcı")),
    ("PERIPHERAL_DEVICE", "PRINTER", ("yazıcı", "printer", "dekont basılmıyor")),
    ("PERIPHERAL_DEVICE", "CASH_RECYCLER", ("para sayma", "recycler", "kasa cihazı", "banknot")),
    ("PERIPHERAL_DEVICE", "SIGNATURE_PAD", ("imza tableti", "imza pedi")),
    ("PERIPHERAL_DEVICE", "SCANNER", ("tarayıcı", "scanner")),
    ("AUTHENTICATION", "ACCOUNT_LOCKED", ("kilitlendi", "kilitli", "bloke oldu")),
    ("AUTHENTICATION", "PASSWORD_RESET", ("şifre sıfırla", "şifremi unuttum", "şifre süresi")),
    ("AUTHENTICATION", "TOKEN_OTP", ("otp", "token", "sms kodu")),
    ("AUTHENTICATION", "AUTHORIZATION_ROLE", ("yetki", "yetkisi yok", "rol tanım")),
    (
        "AUTHENTICATION",
        "LOGIN_FAILURE",
        ("giriş yapılamıyor", "giriş yapamıyor", "oturum açılamıyor", "login"),
    ),
    ("NETWORK", "VPN", ("vpn",)),
    ("NETWORK", "WIFI", ("wifi", "kablosuz")),
    (
        "NETWORK",
        "CONNECTIVITY_LOSS",
        ("bağlantı yok", "bağlantı koptu", "ağ bağlantısı", "internet yok", "network"),
    ),
    ("NETWORK", "SLOWNESS", ("ağ yavaş", "hat yavaş")),
    ("INTEGRATION", "CREDIT_BUREAU", ("kkb", "kredi büro")),
    ("INTEGRATION", "CUSTOMER_REGISTRY", ("mernis", "kimlik paylaşım", "kps")),
    ("INTEGRATION", "PAYMENT_SYSTEM", ("swift", "tcmb", "eft sistemi")),
    (
        "INTEGRATION",
        "CORE_BANKING",
        ("ana bankacılık", "core banking", "servis cevap vermiyor", "servis zaman aşımı"),
    ),
    ("TRANSACTION", "EFT_FAST", ("eft", "fast", "havale")),
    ("TRANSACTION", "CASH_OPERATION", ("nakit", "para çekme", "para yatırma", "vezne")),
    ("TRANSACTION", "FX", ("döviz", "kur ")),
    ("TRANSACTION", "LOAN", ("kredi kullandır", "kredi başvuru", "taksit")),
    ("TRANSACTION", "ACCOUNT_OPENING", ("hesap açılış", "hesap açma", "müşteri kaydı")),
    ("TRANSACTION", "CARD_TRANSACTION", ("kart işlem", "kart limit")),
    ("APPLICATION", "CRASH", ("donuyor", "kapanıyor", "çöküyor", "yanıt vermiyor")),
    ("APPLICATION", "PERFORMANCE", ("çok yavaş", "geç açılıyor")),
    ("APPLICATION", "DATA_DISPLAY", ("görünmüyor", "yanlış görünüyor", "listelenmiyor")),
    ("APPLICATION", "SCREEN_ERROR", ("ekran", "menü", "buton")),
]

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
_APP_RE = re.compile(
    r"(?:uygulama(?:sı)?\s*[:=]\s*(?P<a>[^\n.,;]{2,60}))|"
    r"(?P<b>[A-ZÇĞİÖŞÜ][\wÇĞİÖŞÜçğıöşü-]+(?:\s[A-ZÇĞİÖŞÜ][\wÇĞİÖŞÜçğıöşü-]+){0,2})\s+(?:uygulaması|ekranı|programı)"
)
_FIELD_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    "branch_code": [re.compile(r"şube(?:\s*kodu)?\s*[:#]?\s*(\d{3,5})\b", re.IGNORECASE)],
    "application_version": [
        re.compile(r"(?:sürüm|versiyon|version)\s*[:=]?\s*v?(\d+(?:\.\d+){1,3})", re.IGNORECASE)
    ],
    "error_details": [
        re.compile(r"hata\s*(?:kodu|mesajı)?\s*[:=]\s*([^\n]{3,300})", re.IGNORECASE),
        re.compile(r"([^\n.]*\b(?:[A-Z]{2,10}[-_]?\d{2,6}|0x[0-9A-Fa-f]{4,8})\b[^\n.]*)"),
    ],
    "occurrence_time": [
        re.compile(r"(\d{1,2}[./]\d{1,2}[./]\d{4}(?:\s+(?:saat\s+)?\d{1,2}[:.]\d{2})?)"),
        re.compile(
            r"((?:bugün|dün|bu sabah|öğleden sonra)[^\n.]{0,40}(?:saat\s*\d{1,2}[:.]\d{2})?)", re.IGNORECASE
        ),
        re.compile(r"(saat\s*\d{1,2}[:.]\d{2})", re.IGNORECASE),
    ],
    "frequency": [
        re.compile(
            r"([^\n.]*\b(?:her işlemde|her seferinde|sürekli|aralıklı|ara ara|ilk kez|günde \d+|tüm işlemlerde)\b[^\n.]*)",
            re.IGNORECASE,
        )
    ],
    "environment": [
        re.compile(
            r"([^\n.]*\b(?:terminal|bilgisayar adı|ip adresi|windows|cihaz modeli|seri no)\b[^\n.]*)",
            re.IGNORECASE,
        )
    ],
    "troubleshooting_steps": [
        re.compile(
            r"([^\n.]*\b(?:yeniden başlat\w*|tekrar denendi|kontrol edildi|denedik|denendi|kapatıp açtık|değiştirildi)\b[^\n.]*)",
            re.IGNORECASE,
        )
    ],
    "business_impact": [
        re.compile(
            r"([^\n.]*\b(?:müşteri\w*|işlem yapılamıyor|kuyruk|bekliyor|mağdur|işlemler durdu)\b[^\n.]*)",
            re.IGNORECASE,
        )
    ],
    "business_process": [
        re.compile(
            r"([^\n.]*\b(?:eft|fast|havale|hesap açılış\w*|nakit\s+\w+|döviz\s+\w+|kredi\s+\w+|para yatırma|para çekme)\b[^\n.]*)",
            re.IGNORECASE,
        )
    ],
    "evidence": [
        re.compile(
            r"([^\n.]*\b(?:ekran görüntüsü|ekte|log dosyası|referans no|dekont no)\w*\b[^\n.]*)",
            re.IGNORECASE,
        )
    ],
}
_STEPS_RE = re.compile(r"((?:^|\n)\s*\d[.)]\s*[^\n]+(?:\n\s*\d[.)]\s*[^\n]+)+)")


def _texts(issue: dict[str, Any]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if issue.get("summary"):
        out.append(("summary", str(issue["summary"])))
    if issue.get("description"):
        out.append(("description", str(issue["description"])))
    for c in issue.get("comments") or []:
        if c.get("body"):
            out.append((f"comment:{c['id']}", str(c["body"])))
    return out


def _sentence_with(text: str, needle: str) -> str:
    low = text.lower()
    idx = low.find(needle.lower())
    if idx < 0:
        return needle
    for sentence in _SENTENCE_SPLIT.split(text):
        if needle.lower() in sentence.lower():
            return sentence.strip()[:200]
    return text[max(0, idx - 40) : idx + 160].strip()


class MockModelProvider:
    def __init__(self, capabilities: ModelCapabilities | None = None) -> None:
        self._capabilities = capabilities or ModelCapabilities(structured_outputs=True, json_mode=True)
        self.calls: list[CompletionRequest] = []

    @property
    def model_id(self) -> str:
        return MOCK_MODEL_ID

    @property
    def capabilities(self) -> ModelCapabilities:
        return self._capabilities

    def health(self) -> ModelHealth:
        return ModelHealth(True, MOCK_MODEL_ID, "mock provider")

    def complete(self, request: CompletionRequest, *, timeout: float | None = None) -> CompletionResult:
        self.calls.append(request)
        prompt = request.messages[-1].content
        handler = {
            "ClassificationOutput": self._classify,
            "ExtractionOutput": self._extract,
            "QualityAssessmentOutput": self._assess,
            "SimilarityOutput": self._similarity,
            "InvestigationOutput": self._investigate,
            "RecommendationOutput": self._recommend,
            "ReportOutput": self._report,
        }.get(request.schema_name or "", lambda _p: {"ok": True})
        payload = handler(prompt)
        content = json.dumps(payload, ensure_ascii=False)
        prompt_tokens = sum(len(m.content) for m in request.messages) // 4
        return CompletionResult(
            content=content,
            model_id=MOCK_MODEL_ID,
            output_mode=request.output_mode,
            usage=TokenUsage(prompt_tokens=prompt_tokens, completion_tokens=len(content) // 4),
            latency_ms=1,
            finish_reason="stop",
        )

    # --- agent handlers -------------------------------------------------------------------------

    def _classify(self, prompt: str) -> dict[str, Any]:
        issue = parse_issue_data_block(prompt) or {}
        taxonomy: dict[str, list[str]] = parse_json_block(prompt, "taxonomy") or {}
        texts = _texts(issue)
        scores: dict[tuple[str, str], float] = {}
        quotes: dict[tuple[str, str], list[dict[str, str]]] = {}
        for rule_category, rule_sub, keywords in _CATEGORY_RULES:
            if rule_category not in taxonomy:
                continue
            key = (rule_category, rule_sub)
            # Generic UI words ("ekran", "menü") are weak signals compared to process/device keywords.
            factor = 0.5 if rule_category == "APPLICATION" else 1.0
            for kw in keywords:
                in_summary = any(kw in t.lower() for src, t in texts if src == "summary")
                in_body = any(kw in t.lower() for src, t in texts if src != "summary")
                weight = (2 if in_summary else 0) + (1 if in_body else 0)
                if weight:
                    scores[key] = scores.get(key, 0) + weight * factor
                    source, text = next((src, t) for src, t in texts if kw in t.lower())
                    quotes.setdefault(key, []).append({"source": source, "quote": _sentence_with(text, kw)})
        evidence: list[dict[str, str]] = []
        category: str
        sub: str | None
        if scores:
            best_key, best = max(scores.items(), key=lambda kv: kv[1])
            category, sub = best_key
            evidence = quotes[best_key][:3]
            confidence = "HIGH" if best >= 3 else "MEDIUM"
        else:
            category, sub, confidence = (
                ("OTHER" if "OTHER" in taxonomy else next(iter(taxonomy))),
                None,
                "LOW",
            )
        if sub not in taxonomy.get(category, []):
            sub = None
        app = self._find_application(texts, issue)
        process = self._first_match(texts, "business_process")
        return {
            "category": category,
            "subcategory": sub,
            "application": app[0] if app else None,
            "business_process": process[1] if process else None,
            "confidence": confidence,
            "evidence": evidence[:3],
            "rationale": "Anahtar kelime kurallarına göre sınıflandırıldı (mock).",
        }

    def _find_application(
        self, texts: list[tuple[str, str]], issue: dict[str, Any]
    ) -> tuple[str, str] | None:
        mapped = (issue.get("source_fields") or {}).get("application")
        if mapped:
            return mapped, "field:application"
        for source, text in texts:
            m = _APP_RE.search(text)
            if m:
                value = (m.group("a") or m.group("b") or "").strip()
                if value:
                    return value, source
        return None

    def _first_match(self, texts: list[tuple[str, str]], field: str) -> tuple[str, str, str] | None:
        """Returns (source, value, quote)."""
        for pattern in _FIELD_PATTERNS.get(field, []):
            for source, text in texts:
                m = pattern.search(text)
                if m:
                    value = m.group(1).strip()
                    return source, value, value[:200]
        return None

    def _extract(self, prompt: str) -> dict[str, Any]:
        issue = parse_issue_data_block(prompt) or {}
        texts = _texts(issue)
        fields_block = prompt[prompt.find("<fields>") : prompt.find("</fields>")]
        names = re.findall(r"^- (\w+):", fields_block, re.MULTILINE)
        out: list[dict[str, Any]] = []
        for name in names:
            item: dict[str, Any] = {"field": name, "status": "MISSING", "value": None, "evidence": []}
            if name == "application":
                app = self._find_application(texts, issue)
                if app:
                    value, source = app
                    item.update(status="PRESENT", value=value, evidence=[{"source": source, "quote": value}])
            elif name == "reproduction_steps":
                for source, text in texts:
                    m = _STEPS_RE.search(text)
                    if m:
                        steps = m.group(1).strip()
                        first_line = steps.splitlines()[0].strip()
                        item.update(
                            status="PRESENT", value=steps, evidence=[{"source": source, "quote": first_line}]
                        )
                        break
            else:
                found = self._first_match(texts, name)
                if found:
                    source, value, quote = found
                    item.update(status="PRESENT", value=value, evidence=[{"source": source, "quote": quote}])
                elif name == "error_details":
                    for source, text in texts:
                        if "hata" in text.lower():
                            quote = _sentence_with(text, "hata")
                            item.update(
                                status="VAGUE", value=quote, evidence=[{"source": source, "quote": quote}]
                            )
                            break
            out.append(item)
        return {"fields": out}

    def _assess(self, prompt: str) -> dict[str, Any]:
        items = parse_json_block(prompt, "fields_to_assess") or []
        result = []
        for item in items:
            value = str(item.get("value") or "")
            words = len(value.split())
            has_digit = any(ch.isdigit() for ch in value)
            if item.get("validation_problems"):
                level = "POOR"
            elif item["field"] == "error_details":
                level = "GOOD" if re.search(r"[A-Z]{2,10}[-_]?\d{2,6}|0x[0-9A-Fa-f]{4,8}", value) else "POOR"
            elif item["field"] == "occurrence_time":
                level = (
                    "GOOD"
                    if re.search(r"\d{1,2}[:.]\d{2}", value) and re.search(r"\d{4}", value)
                    else "ACCEPTABLE"
                )
            elif (
                item["field"] in ("branch_code", "application_version", "application")
                or words >= 8
                or (words >= 4 and has_digit)
            ):
                level = "GOOD"
            elif words >= 3:
                level = "ACCEPTABLE"
            else:
                level = "POOR"
            result.append(
                {
                    "field": item["field"],
                    "level": level,
                    "reason": f"Mock kural: {words} kelime.",
                    "evidence": [],
                }
            )
        return {"assessments": result}

    def _similarity(self, prompt: str) -> dict[str, Any]:
        payload = parse_json_block(prompt, "candidates") or {}
        verdicts = []
        for c in payload.get("candidates", []):
            signals = set(c.get("shared_signals") or [])
            anchored = bool(signals & {"same_application", "same_error_signature", "same_error_code"})
            if {"same_branch", "within_duplicate_window"} <= signals and anchored:
                rel = "DUPLICATE_CANDIDATE"
            elif anchored:
                rel = "RELATED"
            elif {"same_category", "within_duplicate_window"} <= signals and (
                signals & {"same_subcategory", "shared_concept"}
            ):
                rel = "POSSIBLE_COMMON_CAUSE"
            else:
                rel = "UNRELATED"
            verdicts.append(
                {
                    "incident_key": c["incident_key"],
                    "relationship": rel,
                    "rationale": "Ortak sinyaller: "
                    + (", ".join(sorted(signals)) or "yok")
                    + ". Benzerlik aynı kök nedeni kanıtlamaz.",
                    "evidence": [],
                }
            )
        return {"verdicts": verdicts}

    def _investigate(self, prompt: str) -> dict[str, Any]:
        """Look at the first candidate once, then conclude: one quoted observation, one hypothesis."""
        payload = parse_json_block(prompt, "candidates") or {}
        history = parse_json_block(prompt, "history") or []
        candidates = payload.get("candidates") or []
        if candidates and not history and not payload.get("must_conclude"):
            key = candidates[0]["incident_key"]
            return {"action": "call_tool", "tool": "get_issue_summary", "arguments": {"incident_key": key}}
        findings: list[dict[str, Any]] = []
        if candidates:
            first = candidates[0]
            quote = (first.get("summary") or "")[:120]
            findings.append(
                {
                    "statement": f"{first['incident_key']} kaydında benzer belirti bildirilmiş.",
                    "kind": "OBSERVATION",
                    "evidence": [{"source": f"incident:{first['incident_key']}", "quote": quote}]
                    if quote
                    else [],
                    "related_incidents": [first["incident_key"]],
                }
            )
            findings.append(
                {
                    "statement": "Ortak bir altyapı sorunu olabilir; doğrulanmadı.",
                    "kind": "OBSERVATION",  # no evidence: Python stores it as a HYPOTHESIS
                    "evidence": [],
                    "related_incidents": [c["incident_key"] for c in candidates[:3]],
                }
            )
        return {"action": "conclude", "findings": findings}

    def _recommend(self, prompt: str) -> dict[str, Any]:
        gaps = parse_json_block(prompt, "gaps") or []
        return {
            "recommendations": [
                {"field": g["field"], "message": f"Lütfen kayda ekleyin: {g['template_hint']}"} for g in gaps
            ]
        }

    def _report(self, prompt: str) -> dict[str, Any]:
        from branchops.agents.reporting import deterministic_report

        metrics = parse_json_block(prompt, "metrics") or {}
        return deterministic_report(metrics).model_dump()
