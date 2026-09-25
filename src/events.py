"""巨灾事件通知台账：事件汇总、余额计算与状态评估。

纯函数模块，不直接访问数据库。保存见 repository.py，用例编排见
service.py，详情页见 static/event.html。
"""
from typing import Any, Dict, List

from .domain import ValidationError, number, optional_text, text


CLAIM_OPEN_STATUSES = ("pending_evidence", "admitted")
CLAIM_CLOSED_STATUSES = ("settled", "rejected")
CLAIM_STATUSES = CLAIM_OPEN_STATUSES + CLAIM_CLOSED_STATUSES
CAPACITY_TOLERANCE = 0.01


def validate_event_report(payload: Dict[str, Any]) -> Dict[str, Any]:
    """首次报案登记：发生时刻、预估总损失和报送人。"""
    data = dict(payload or {})
    return {
        "event_id": text(data, "event_id"),
        "occurred_at": text(data, "occurred_at"),
        "estimated_total_loss": number(data, "estimated_total_loss", 0),
        "reported_by": optional_text(data, "reported_by"),
    }


def validate_claim_append(payload: Dict[str, Any]) -> Dict[str, Any]:
    """追加赔案到已有事件。"""
    data = dict(payload or {})
    record_id = data.get("record_id")
    if isinstance(record_id, bool) or not isinstance(record_id, int):
        raise ValidationError("record_id必须是整数")
    append = {"record_id": record_id}
    if data.get("claim_number") is not None:
        append["claim_number"] = text(data, "claim_number")
    if data.get("estimated_loss") is not None:
        append["estimated_loss"] = number(data, "estimated_loss", 0)
    return append


def validate_supplement(payload: Dict[str, Any], current_total: float) -> float:
    """补证只允许下调预估总损失。"""
    new_total = number(dict(payload or {}), "estimated_total_loss", 0)
    if new_total >= float(current_total):
        raise ValidationError("补证必须下调预估总损失")
    return new_total


def layer_capacity(record_payload: Dict[str, Any]) -> float:
    """合约层容量 = 层宽 × 分保比例。"""
    width = float(record_payload.get("layer_width", 0.0))
    cession = float(record_payload.get("cession_pct", 0.0))
    return round(width * cession, 2)


def claim_status_for(estimated_total_loss: float, capacity: float) -> str:
    """预估总额超过合约层容量时赔案留在待补证，否则可进入核定。"""
    if float(estimated_total_loss) > float(capacity) + CAPACITY_TOLERANCE:
        return "pending_evidence"
    return "admitted"


def summarize_event(event: Dict[str, Any], claims: List[Dict[str, Any]], records_by_id: Dict[int, Dict[str, Any]]) -> Dict[str, Any]:
    """事件汇总：赔案清单、未结金额和合约层余额。"""
    lines = []
    for claim in claims:
        record = records_by_id.get(claim["record_id"]) or {}
        payload = record.get("payload") or {}
        capacity = layer_capacity(payload)
        recoverable = round(float(payload.get("recoverable_amount", 0.0)), 2)
        lines.append({
            "claim_id": claim["id"],
            "record_id": claim["record_id"],
            "reference": record.get("reference", ""),
            "record_state": record.get("state", "missing"),
            "claim_number": claim["claim_number"],
            "estimated_loss": round(float(claim["estimated_loss"]), 2),
            "status": claim["status"],
            "recoverable_amount": recoverable,
            "layer_capacity": capacity,
            "created_at": claim["created_at"],
        })
    active = [line for line in lines if line["status"] != "rejected"]
    open_lines = [line for line in lines if line["status"] in CLAIM_OPEN_STATUSES]
    settled = [line for line in lines if line["status"] == "settled"]
    balances = []
    for line in lines:
        used = line["recoverable_amount"] if line["status"] != "rejected" else 0.0
        balances.append({
            "record_id": line["record_id"],
            "reference": line["reference"],
            "capacity": line["layer_capacity"],
            "used": round(used, 2),
            "remaining": round(line["layer_capacity"] - used, 2),
        })
    return {
        "event_id": event["event_id"],
        "occurred_at": event["occurred_at"],
        "estimated_total_loss": round(float(event["estimated_total_loss"]), 2),
        "reported_by": event["reported_by"],
        "version": event["version"],
        "created_at": event["created_at"],
        "updated_at": event["updated_at"],
        "claims": lines,
        "claim_count": len(lines),
        "pending_evidence_count": len([line for line in lines if line["status"] == "pending_evidence"]),
        "estimated_claims_total": round(sum(line["estimated_loss"] for line in active), 2),
        "recoverable_total": round(sum(line["recoverable_amount"] for line in active), 2),
        "settled_total": round(sum(line["recoverable_amount"] for line in settled), 2),
        "outstanding_total": round(sum(line["recoverable_amount"] for line in open_lines), 2),
        "balances": balances,
    }
