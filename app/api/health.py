"""Health / readiness endpoints and lightweight funnel statistics."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import require_admin
from app.config import Settings, get_settings
from app.db.base import get_db
from app.db.models import Lead
from app.domain.enums import LeadStatus

router = APIRouter(tags=["health"])

_STATUSES = [
    LeadStatus.NEW,
    LeadStatus.CONTACTED,
    LeadStatus.IN_PROGRESS,
    LeadStatus.QUALIFIED,
    LeadStatus.HANDED_OFF,
    LeadStatus.OPTED_OUT,
    LeadStatus.FAILED,
]


@router.get("/health")
def health(settings: Settings = Depends(get_settings)) -> dict[str, object]:
    return {
        "status": "ok",
        "service": settings.app_name,
        "environment": settings.environment,
        "dry_run": settings.dry_run,
        "llm_enabled": settings.llm_enabled,
        "meta_configured": settings.meta_configured,
        "admin_number_configured": bool(settings.admin_whatsapp_number),
    }


@router.get("/ready")
def ready(db: Session = Depends(get_db)) -> dict[str, str]:
    db.execute(select(func.count()).select_from(Lead))
    return {"status": "ready"}


@router.get("/api/stats", dependencies=[Depends(require_admin)])
def stats(db: Session = Depends(get_db)) -> dict[str, object]:
    rows = db.execute(select(Lead.status, func.count(Lead.id)).group_by(Lead.status)).all()
    counts = {status.value: 0 for status in _STATUSES}
    for status_value, count in rows:
        counts[str(status_value)] = int(count)
    total = sum(counts.values())
    qualified = counts[LeadStatus.QUALIFIED.value] + counts[LeadStatus.HANDED_OFF.value]
    return {
        "total_leads": total,
        "by_status": counts,
        "qualified": qualified,
        "handed_off": counts[LeadStatus.HANDED_OFF.value],
        "conversion_rate": round(qualified / total, 3) if total else 0.0,
    }
