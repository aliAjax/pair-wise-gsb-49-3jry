"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from . import events
from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, text
from .repository import Repository
from .rules import DomainRules


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def _ensure_event_role(self, actor: Actor, action: str) -> None:
        if not self.rules.role_can_event_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")

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
        data = dict(data or {})
        if action == "calculate":
            self._ensure_not_pending_evidence(record_id)
        if action == "submit_claim":
            self._precheck_event_report(data)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data)
        result = self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data, "from": record["state"], "to": new_state},
        )
        self._sync_event_after_action(result, actor, action, data)
        return result

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()

    # ---- 巨灾事件通知台账 ----

    def report_event(self, actor: Actor, data: Dict[str, Any]) -> Dict[str, Any]:
        """第一次报案：登记发生时刻、预估总损失和报送人。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._ensure_event_role(actor, "report_event")
        report = events.validate_event_report(data)
        reported_by = report["reported_by"] or actor.user_id
        return self.repository.create_event(
            report["event_id"], report["occurred_at"], report["estimated_total_loss"], reported_by
        )

    def list_event_summaries(self, actor: Actor) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return [self._summarize_event(event) for event in self.repository.list_events()]

    def get_event_summary(self, actor: Actor, event_id: str) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self._summarize_event(self.repository.get_event(event_id))

    def append_event_claim(self, actor: Actor, event_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """后续赔案追加到已有事件；预估总额超容量时留在待补证。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._ensure_event_role(actor, "append_event_claim")
        event = self.repository.get_event(event_id)
        append = events.validate_claim_append(data)
        record = self.repository.get(append["record_id"])
        if record["state"] not in ("claim_submitted", "calculated", "settled"):
            raise Conflict("当前状态不允许追加到事件台账")
        payload = record["payload"]
        claim_number = append.get("claim_number") or payload.get("claim_number") or ("CLM-%s" % record["id"])
        estimated_loss = append.get("estimated_loss")
        if estimated_loss is None:
            estimated_loss = float(payload.get("loss_amount", 0.0))
        capacity = events.layer_capacity(payload)
        status = events.claim_status_for(event["estimated_total_loss"], capacity)
        return self.repository.append_event_claim(event["event_id"], record["id"], claim_number, estimated_loss, status)

    def supplement_event(self, actor: Actor, event_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """补证下调预估总损失，容量内的待补证赔案恢复核定。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._ensure_event_role(actor, "supplement_event")
        event = self.repository.get_event(event_id)
        new_total = events.validate_supplement(data, event["estimated_total_loss"])
        self.repository.update_event_estimate(event_id, new_total)
        for claim in self.repository.list_event_claims(event_id):
            if claim["status"] != "pending_evidence":
                continue  # 已结算/已拒绝等赔案保持原样
            record = self.repository.get(claim["record_id"])
            if events.claim_status_for(new_total, events.layer_capacity(record["payload"])) == "admitted":
                self.repository.set_event_claim_status(claim["id"], "admitted")
        return self._summarize_event(self.repository.get_event(event_id))

    def _summarize_event(self, event: Dict[str, Any]) -> Dict[str, Any]:
        claims = self.repository.list_event_claims(event["event_id"])
        records = {claim["record_id"]: self.repository.get(claim["record_id"]) for claim in claims}
        return events.summarize_event(event, claims, records)

    def _ensure_not_pending_evidence(self, record_id: int) -> None:
        for claim in self.repository.event_claims_for_record(record_id):
            if claim["status"] == "pending_evidence":
                raise Conflict("赔案处于待补证状态，不能进入核定")

    def _precheck_event_report(self, data: Dict[str, Any]) -> None:
        event_id = data.get("event_id")
        if not isinstance(event_id, str) or not event_id.strip():
            return
        if self.repository.find_event(event_id.strip()) is not None:
            return
        if "occurred_at" not in data and "estimated_total_loss" not in data:
            return
        events.validate_event_report(data)

    def _sync_event_after_action(self, record: Dict[str, Any], actor: Actor, action: str, data: Dict[str, Any]) -> None:
        if action == "submit_claim":
            self._sync_event_claim(record, actor, data)
        elif action == "settle":
            self._mark_event_claims(record["id"], "settled")
        elif action == "reject":
            self._mark_event_claims(record["id"], "rejected")

    def _sync_event_claim(self, record: Dict[str, Any], actor: Actor, data: Dict[str, Any]) -> None:
        event_id = record["payload"].get("claim_event_id", "")
        if not event_id:
            return
        if self.repository.find_event(event_id) is None:
            if "occurred_at" not in data and "estimated_total_loss" not in data:
                return  # 事件未登记，保持原有逐案流程
            self.report_event(actor, {
                "event_id": event_id,
                "occurred_at": data.get("occurred_at"),
                "estimated_total_loss": data.get("estimated_total_loss"),
                "reported_by": data.get("reported_by") or actor.user_id,
            })
        try:
            self.append_event_claim(actor, event_id, {"record_id": record["id"]})
        except Conflict:
            pass  # 赔案已追加过，保持幂等

    def _mark_event_claims(self, record_id: int, status: str) -> None:
        for claim in self.repository.event_claims_for_record(record_id):
            if claim["status"] in events.CLAIM_OPEN_STATUSES:
                self.repository.set_event_claim_status(claim["id"], status)
