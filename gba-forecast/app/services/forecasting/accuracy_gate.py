"""Non-fatal accuracy gate: records holdout WAPE and bias per class for the daily refresh."""

from __future__ import annotations

from datetime import date

from app.core.config import get_settings
from app.core.logging import get_logger
from app.data import signals_repository as sig
from app.services import forecast as fc
from app.services.forecasting import holdout, selection

log = get_logger("accuracy_gate")

BASELINE_METHOD = "mean_3"
SERVED_LEAD_MONTHS = 2
SCOPE_BY_GATE = {"clients": selection.SCOPE_CLIENT, "products": selection.SCOPE_PRODUCT}


def dense_closed_series(
    grouped: dict[int, dict[str, float]], labels: list[str]
) -> list[list[float]]:
    return [fc.series_from(history, labels) for history in grouped.values()]


def summarize(evaluation: dict) -> dict:
    overall = evaluation["by_class"].get(holdout.OVERALL)
    service = overall["methods"]["service"] if overall else None
    baseline = overall["methods"][BASELINE_METHOD] if overall else None
    beats_baseline = (
        None
        if service is None or baseline is None or service["wape_pct"] is None
        else service["wape_pct"] <= baseline["wape_pct"]
    )
    gaps = class_gaps(evaluation)
    return {
        **evaluation,
        "baseline_method": BASELINE_METHOD,
        "beats_baseline": beats_baseline,
        "class_gap_points": gaps,
        "classes_behind_baseline": [name for name, gap in gaps.items() if gap > 0],
    }


def class_gaps(evaluation: dict) -> dict[str, float]:
    gaps: dict[str, float] = {}
    for name in holdout.CLASSES:
        methods = evaluation["by_class"].get(name, {}).get("methods", {})
        service = methods.get("service", {}).get("wape_pct")
        baseline = methods.get(BASELINE_METHOD, {}).get("wape_pct")
        if service is not None and baseline is not None:
            gaps[name] = round(service - baseline, 2)
    return gaps


def run(as_of: str, origins: int = 6) -> dict:
    try:
        cfg = get_settings()
        as_of_date = date.fromisoformat(as_of)
        labels = fc.training_labels(as_of_date, cfg)
        scopes = {
            "clients": sig.group_series_by_entity(
                sig.all_client_monthly_series(as_of, cfg.history_months), "cid"
            ),
            "products": sig.group_series_by_entity(
                sig.all_product_monthly_series(as_of, cfg.history_months), "pid"
            ),
        }
        result: dict = {
            "as_of": as_of,
            "model_version": cfg.model_version,
            "first_month": labels[0] if labels else None,
            "last_month": labels[-1] if labels else None,
        }
        flags = []
        for scope, grouped in scopes.items():
            summary = summarize(
                holdout.evaluate(
                    dense_closed_series(grouped, labels),
                    origins=origins,
                    min_non_zero=cfg.min_history_months,
                    scope=SCOPE_BY_GATE[scope],
                    lead=SERVED_LEAD_MONTHS,
                )
            )
            summary["series"] = len(grouped)
            result[scope] = summary
            flags.append(summary["beats_baseline"])
        result["status"] = "ok" if all(flag is True for flag in flags) else "warn"
        log.info("accuracy_gate", status=result["status"])
        return result
    except Exception as exc:  # noqa: BLE001
        log.warning("accuracy_gate_failed", error=str(exc))
        return {"status": "error", "as_of": as_of, "error": str(exc)}
