"""Row-formatting helpers shared by the read-side query modules."""

from __future__ import annotations

from typing import Any

from branchops.infrastructure.database.models import Analysis


def _r(v: Any) -> float | None:
    return round(float(v), 2) if v is not None else None


def _analysis_dict(a: Analysis) -> dict[str, Any]:
    return {
        "analysis_id": str(a.id),
        "status": a.status,
        "started_at": a.started_at.isoformat(),
        "completed_at": a.completed_at.isoformat() if a.completed_at else None,
        "duration_ms": a.duration_ms,
        "template_id": a.template_id,
        "template_version": a.template_version,
        "scoring_policy_version": a.scoring_policy_version,
        "prompt_version": a.prompt_version,
        "model_id": a.model_id,
        "completeness_score": _r(a.completeness_score),
        "quality_score": _r(a.quality_score),
        "classification": a.classification,
        "canonical_issue": a.canonical_issue,
        "field_scores": a.field_scores or [],
        "assessments": a.assessments or [],
        "missing_fields": a.missing_fields or [],
        "insufficient_fields": a.insufficient_fields or [],
        "unknown_fields": a.unknown_fields or [],
        "missing_required_fields": a.missing_required_fields or [],
        "recommendations": a.recommendations or [],
        "similar_issues": a.similar_issues or [],
        "recurring": a.recurring,
        "recurring_count": a.recurring_count,
        "security_flags": a.security_flags or [],
        "errors": a.errors or [],
        "prompt_tokens": a.prompt_tokens,
        "completion_tokens": a.completion_tokens,
    }


def _last_outputs(a: Analysis | None) -> dict[str, str]:
    """Short human-readable result of each node for the most recently finished analysis."""
    if a is None:
        return {}
    key = a.issue_key
    out: dict[str, str] = {}
    cls = a.classification or {}
    if cls:
        out["classify"] = (
            f"{key}: {cls.get('category')}/{cls.get('subcategory') or '—'} ({cls.get('confidence')})"
        )
    if a.template_id:
        out["select_template"] = f"{key}: {a.template_id}@{a.template_version}"
    fields = (a.canonical_issue or {}).get("fields", {})
    if fields:
        found = sum(1 for f in fields.values() if f.get("state") in ("SUFFICIENT", "INSUFFICIENT"))
        out["extract"] = f"{key}: {found}/{len(fields)} alan bulundu"
    if a.quality_score is not None:
        out["assess_quality"] = f"{key}: {len(a.assessments or [])} alan değerlendirildi"
    if a.completeness_score is not None:
        out["score"] = f"{key}: tamlık {a.completeness_score:.0f} · kalite " + (
            f"{a.quality_score:.0f}" if a.quality_score is not None else "—"
        )
    sims = a.similar_issues or []
    out["similarity"] = f"{key}: {len(sims)} benzer kayıt" + (" · tekrarlayan" if a.recurring else "")
    out["recommend"] = f"{key}: {len(a.recommendations or [])} öneri"
    out["persist"] = f"{key}: {a.status}"
    if a.security_flags:
        out["normalize"] = f"{key}: {len(a.security_flags)} güvenlik uyarısı"
    else:
        out["normalize"] = f"{key}: temiz"
    return out
