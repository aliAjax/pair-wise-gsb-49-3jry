"""巨灾事件通知台账：报案校验、事件汇总与余额计算。"""
from typing import Any, Dict, Iterable, List

from .domain import number, optional_text, text


NORMAL = "normal"
PENDING_SUPPLEMENTATION = "pending_supplementation"
EPSILON = 0.01

REPORT_ROLES = {"claims_officer"}
SUPPLEMENT_ROLES = {"claims_officer"}
OPEN_CLAIM_STATES = {"claim_submitted", "calculated"}


def validate_report(payload: Dict[str, Any]) -> Dict[str, Any]:
    """首次报案：登记事件号、发生时刻、预估总损失，报送人缺省取调用人。"""
    data = dict(payload or {})
    return {
        "event_id": text(data, "event_id"),
        "occurred_at": text(data, "occurred_at"),
        "estimated_total_loss": round(number(data, "estimated_total_loss", 0), 2),
        "reported_by": optional_text(data, "reported_by"),
    }


def validate_supplement(payload: Dict[str, Any]) -> Dict[str, Any]:
    """补证：调整预估总损失，可附说明。"""
    data = dict(payload or {})
    return {
        "estimated_total_loss": round(number(data, "estimated_total_loss", 0), 2),
        "note": optional_text(data, "note"),
    }


def is_contract(record: Dict[str, Any], event_id: str) -> bool:
    return record["state"] != "rejected" and record["payload"].get("event_id") == event_id


def is_claim(record: Dict[str, Any], event_id: str) -> bool:
    payload = record["payload"]
    return bool(payload.get("claim_number")) and payload.get("claim_event_id") == event_id


def layer_capacity(record: Dict[str, Any]) -> float:
    payload = record["payload"]
    return float(payload.get("layer_width", 0)) * float(payload.get("cession_pct", 0))


def event_balance(event: Dict[str, Any], records: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """余额计算：合约层容量、已承诺摊回、容量余额与待补证金额。"""
    records = list(records)
    capacity = sum(layer_capacity(item) for item in records if is_contract(item, event["event_id"]))
    committed = sum(
        float(item["payload"].get("recoverable_amount", 0))
        for item in records
        if is_claim(item, event["event_id"]) and item["state"] != "rejected"
    )
    estimated = float(event["estimated_total_loss"])
    excess = max(0.0, estimated - capacity)
    return {
        "layer_capacity": round(capacity, 2),
        "estimated_total_loss": round(estimated, 2),
        "committed_recoverable": round(committed, 2),
        "remaining_capacity": round(max(0.0, capacity - committed), 2),
        "excess_amount": round(excess, 2),
        "status": PENDING_SUPPLEMENTATION if excess > EPSILON else NORMAL,
    }


def summarize_claims(records: Iterable[Dict[str, Any]], event_id: str) -> Dict[str, Any]:
    """事件汇总：赔案数量、核定总额、未结金额与已结算金额。"""
    claims = [item for item in records if is_claim(item, event_id)]
    open_claims = [item for item in claims if item["state"] in OPEN_CLAIM_STATES]
    settled = [item for item in claims if item["state"] == "settled"]
    approved = [
        item
        for item in claims
        if item["state"] != "rejected" and item["payload"].get("approved_loss") is not None
    ]
    return {
        "claim_count": len(claims),
        "open_claim_count": len(open_claims),
        "settled_claim_count": len(settled),
        "approved_total": round(sum(float(item["payload"]["approved_loss"]) for item in approved), 2),
        "outstanding_amount": round(sum(float(item["payload"].get("recoverable_amount", 0)) for item in open_claims), 2),
        "settled_total": round(sum(float(item["payload"].get("recoverable_amount", 0)) for item in settled), 2),
    }


def claim_view(record: Dict[str, Any]) -> Dict[str, Any]:
    payload = record["payload"]
    return {
        "record_id": record["id"],
        "reference": record["reference"],
        "state": record["state"],
        "claim_number": payload.get("claim_number", ""),
        "approved_loss": payload.get("approved_loss"),
        "recoverable_amount": payload.get("recoverable_amount", 0),
        "payment_reference": payload.get("payment_reference", ""),
    }
