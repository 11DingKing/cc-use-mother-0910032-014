"""复核案件服务。

所有处置（合并重复、部分采纳、申诉撤回、决定重开）只追加事件、
更新案件状态，原始评价与历史决定全部保留。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Iterable, Sequence

from .models import (
    AnomalySignal,
    CaseEvent,
    CaseEventType,
    CaseStatus,
    DecisionType,
    ReviewCase,
)
from .store import InMemoryStore


class CaseError(ValueError):
    """案件操作违反状态约束。"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CaseService:
    def __init__(self, store: InMemoryStore, clock: Callable[[], datetime] = _utcnow) -> None:
        self._store = store
        self._clock = clock

    # -- 建案 ----------------------------------------------------------

    def open_case(
        self,
        *,
        activity_id: str,
        review_ids: Sequence[str],
        reason: str,
        opened_by: str,
        rule_ids: Sequence[str] = ("manual-appeal",),
    ) -> ReviewCase:
        ids = list(dict.fromkeys(review_ids))
        if not ids:
            raise CaseError("案件至少关联一条评价")
        docent_id: str | None = None
        for review_id in ids:
            review = self._store.reviews.get(review_id)
            if review is None:
                raise CaseError(f"评价不存在：{review_id}")
            if review.activity_id != activity_id:
                raise CaseError(f"评价 {review_id} 不属于活动 {activity_id}")
            docent_id = docent_id or review.docent_id
        case = ReviewCase(
            case_id=self._store.next_case_id(),
            activity_id=activity_id,
            docent_id=docent_id or "",
            review_ids=ids,
            rule_ids=list(dict.fromkeys(rule_ids)),
            reason=reason,
            opened_by=opened_by,
            opened_at=self._clock(),
        )
        case.events.append(CaseEvent(CaseEventType.OPENED, opened_by, case.opened_at, detail=reason))
        self._store.cases[case.case_id] = case
        return case

    def open_cases_for_signals(
        self, signals: Iterable[AnomalySignal], *, opened_by: str = "system:detection"
    ) -> list[ReviewCase]:
        """把检测信号转成复核案件；已被进行中案件覆盖的信号不重复建案。"""
        opened: list[ReviewCase] = []
        for signal in signals:
            review_ids = list(signal.review_ids)
            if not review_ids:
                continue
            activity_id = self._store.reviews[review_ids[0]].activity_id
            if self._covered_by_active_case(activity_id, review_ids):
                continue
            opened.append(self.open_case(
                activity_id=activity_id,
                review_ids=review_ids,
                rule_ids=[signal.rule_id],
                reason=signal.reason,
                opened_by=opened_by,
            ))
        return opened

    def _covered_by_active_case(self, activity_id: str, review_ids: Sequence[str]) -> bool:
        ids = set(review_ids)
        return any(
            case.active and ids <= set(case.review_ids)
            for case in self._store.cases_of(activity_id)
        )

    # -- 处置 ----------------------------------------------------------

    def merge(
        self,
        target_case_id: str,
        source_case_ids: Iterable[str],
        *,
        actor: str,
        note: str = "",
    ) -> ReviewCase:
        """合并重复案件：来源案件保留原值并标记已并入，目标案件吸收其关联。"""
        target = self._require_active(target_case_id)
        for source_id in source_case_ids:
            if source_id == target_case_id:
                raise CaseError("案件不能并入自身")
            source = self._require_active(source_id)
            now = self._clock()
            for review_id in source.review_ids:
                if review_id not in target.review_ids:
                    target.review_ids.append(review_id)
            for rule_id in source.rule_ids:
                if rule_id not in target.rule_ids:
                    target.rule_ids.append(rule_id)
            source.status = CaseStatus.MERGED
            source.events.append(CaseEvent(
                CaseEventType.MERGED_INTO, actor, now,
                detail=note or f"并入案件 {target_case_id}",
                related_case_id=target_case_id,
            ))
            target.events.append(CaseEvent(
                CaseEventType.ABSORBED, actor, now,
                detail=f"吸收案件 {source_id}",
                related_case_id=source_id,
            ))
        return target

    def decide(
        self,
        case_id: str,
        *,
        actor: str,
        decision: DecisionType,
        excluded_review_ids: Sequence[str] = (),
        note: str = "",
    ) -> ReviewCase:
        """复核决定。部分采纳只把评价排除出后续发布集，原始评分保持不变。"""
        case = self._require_active(case_id)
        excluded = tuple(dict.fromkeys(excluded_review_ids))
        if decision is DecisionType.PARTIAL_ADOPT:
            if not excluded:
                raise CaseError("部分采纳必须指定要排除的评价")
            unknown = sorted(set(excluded) - set(case.review_ids))
            if unknown:
                raise CaseError("只能排除本案关联的评价：" + "、".join(unknown))
        elif decision is DecisionType.REJECT:
            if excluded:
                raise CaseError("驳回决定不能排除评价")
        else:
            raise CaseError(f"不支持的决定类型：{decision}")
        case.status = CaseStatus.DECIDED
        case.events.append(CaseEvent(
            CaseEventType.DECIDED, actor, self._clock(),
            detail=note, decision=decision, excluded_review_ids=excluded,
        ))
        return case

    def withdraw(self, case_id: str, *, actor: str, note: str = "") -> ReviewCase:
        """申诉撤回：仅申诉发起人可撤回，案件与历史完整保留。"""
        case = self._require_active(case_id)
        if actor != case.opened_by:
            raise CaseError("只有申诉发起人可以撤回")
        case.status = CaseStatus.WITHDRAWN
        case.events.append(CaseEvent(CaseEventType.WITHDRAWN, actor, self._clock(), detail=note))
        return case

    def reopen(self, case_id: str, *, actor: str, reason: str) -> ReviewCase:
        """决定重开：原决定事件保留在历史中，但不再生效，等待再次复核。"""
        case = self._require(case_id)
        if case.status is not CaseStatus.DECIDED:
            raise CaseError("只有已决定的案件可以重开")
        case.status = CaseStatus.REOPENED
        case.events.append(CaseEvent(CaseEventType.REOPENED, actor, self._clock(), detail=reason))
        return case

    # -- 查询 ----------------------------------------------------------

    def effective_exclusions(self, activity_id: str) -> set[str]:
        """当前生效的排除集合：已决定且未重开的部分采纳决定之并集。"""
        excluded: set[str] = set()
        for case in self._store.cases_of(activity_id):
            if case.status is not CaseStatus.DECIDED:
                continue
            last_decision = next(
                (e for e in reversed(case.events) if e.event_type is CaseEventType.DECIDED),
                None,
            )
            if last_decision and last_decision.decision is DecisionType.PARTIAL_ADOPT:
                excluded.update(last_decision.excluded_review_ids)
        return excluded

    def _require(self, case_id: str) -> ReviewCase:
        case = self._store.cases.get(case_id)
        if case is None:
            raise CaseError(f"案件不存在：{case_id}")
        return case

    def _require_active(self, case_id: str) -> ReviewCase:
        case = self._require(case_id)
        if not case.active:
            raise CaseError(f"案件 {case_id} 当前状态为 {case.status.value}，不能执行该操作")
        return case
