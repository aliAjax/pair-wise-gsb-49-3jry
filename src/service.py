"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, DomainError, PermissionDenied, text
from .event_rules import (
    PENDING_SUPPLEMENTATION,
    REPORT_ROLES,
    SUPPLEMENT_ROLES,
    claim_view,
    event_balance,
    is_claim,
    summarize_claims,
    validate_report,
    validate_supplement,
)
from .event_store import EventStore
from .repository import Repository
from .rules import DomainRules


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None, event_store: EventStore = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)
        self.event_store = event_store

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def _ensure_event_role(self, actor: Actor, allowed: set) -> None:
        if actor.role != "admin" and actor.role not in allowed:
            raise PermissionDenied("角色无权执行该事件操作")

    def _require_event_store(self) -> EventStore:
        if self.event_store is None:
            raise DomainError("事件台账未启用")
        return self.event_store

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        if action == "calculate":
            self._ensure_event_calculable(record)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
        )

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()

    def _ensure_event_calculable(self, record: Dict[str, Any]) -> None:
        """核定闸门：事件预估超出合约层容量时，超出部分待补证，不能核定。"""
        if self.event_store is None:
            return
        event_id = record["payload"].get("claim_event_id") or record["payload"].get("event_id")
        if not event_id:
            return
        event = self.event_store.find_by_event_id(event_id)
        if event is None:
            return
        balance = event_balance(event, self.repository.list_records(limit=500))
        if balance["status"] == PENDING_SUPPLEMENTATION:
            raise Conflict(
                "事件%s预估总损失超出合约层容量，待补证金额%s，暂不能核定" % (event_id, balance["excess_amount"])
            )

    def report_event(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        """首次报案：登记发生时刻、预估总损失和报送人；同事件再次登记会被拒绝。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._ensure_event_role(actor, REPORT_ROLES)
        store = self._require_event_store()
        report = validate_report(payload)
        return store.create(
            event_id=report["event_id"],
            occurred_at=report["occurred_at"],
            estimated_total_loss=report["estimated_total_loss"],
            reported_by=report["reported_by"] or actor.user_id,
            actor_id=actor.user_id,
        )

    def list_events(self, actor: Actor, limit: int = 200) -> List[Dict[str, Any]]:
        """事件台账列表：逐事件给出余额计算与赔案汇总。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        store = self._require_event_store()
        records = self.repository.list_records(limit=500)
        items = []
        for event in store.list_events(limit=limit):
            balance = event_balance(event, records)
            item = dict(event)
            item["status"] = balance["status"]
            item["balance"] = balance
            item["summary"] = summarize_claims(records, event["event_id"])
            items.append(item)
        return items

    def get_event(self, actor: Actor, event_pk: int) -> Dict[str, Any]:
        """事件详情：登记信息、余额计算、赔案清单和报案/补证历史。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        store = self._require_event_store()
        event = store.get(event_pk)
        records = self.repository.list_records(limit=500)
        balance = event_balance(event, records)
        detail = dict(event)
        detail["status"] = balance["status"]
        return {
            "event": detail,
            "balance": balance,
            "summary": summarize_claims(records, event["event_id"]),
            "claims": [claim_view(item) for item in records if is_claim(item, event["event_id"])],
            "entries": store.entries(event_pk),
        }

    def supplement_event(self, actor: Actor, event_pk: int, expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        """补证：调整预估总损失；下调到容量内后事件恢复正常，核定自动放行。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._ensure_event_role(actor, SUPPLEMENT_ROLES)
        store = self._require_event_store()
        payload = validate_supplement(data)
        updated = store.mutate(
            event_pk=event_pk,
            expected_version=int(expected_version),
            estimated_total_loss=payload["estimated_total_loss"],
            actor_id=actor.user_id,
            kind="supplement",
            note=payload["note"],
        )
        balance = event_balance(updated, self.repository.list_records(limit=500))
        result = dict(updated)
        result["status"] = balance["status"]
        result["balance"] = balance
        return result
