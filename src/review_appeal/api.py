"""面向讲解员的接口：展示影响解释，裁剪评价者敏感信息。

讲解员可以看到可疑评价的内容、来源与匿名代号，以及排除后对等级的
影响解释；账号、联系方式、提交指纹等敏感信息不会离开内部服务。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Sequence

from .cases import CaseService
from .models import CaseEvent, Review, ReviewCase
from .publishing import GradeReport, PublishingService, grade_band
from .store import InMemoryStore


def reviewer_alias(account_id: str) -> str:
    """稳定化名：同一账号显示同一代号，但无法反推真实身份。"""
    digest = hashlib.sha256(account_id.encode("utf-8")).hexdigest()
    return f"评价者-{digest[:8]}"


def mask_display_name(name: str) -> str:
    return f"{name[:1]}*" if name else "*"


@dataclass(frozen=True)
class DocentReviewView:
    """讲解员可见的评价视图：不含账号、联系方式与提交指纹。"""

    review_id: str
    source: str
    identity_verification: str
    rating_version: str
    score: float
    comment: str
    submitted_at: str
    reviewer_alias: str
    reviewer_display: str


@dataclass(frozen=True)
class CaseSummary:
    case_id: str
    status: str
    reason: str
    review_count: int
    event_count: int


@dataclass(frozen=True)
class ImpactExplanation:
    """复核案件对等级的影响解释，基于当前已发布评价集估算。"""

    case_id: str
    case_status: str
    current_set_id: str
    current_version: int
    current_average: float
    current_grade: str
    flagged_in_current_set: int
    hypothetical_average: float | None
    hypothetical_grade: str | None
    flagged_reviews: tuple[DocentReviewView, ...]
    summary: str


class DocentAPI:
    def __init__(
        self,
        store: InMemoryStore,
        cases: CaseService,
        publishing: PublishingService,
    ) -> None:
        self._store = store
        self._cases = cases
        self._publishing = publishing

    # -- 申诉入口 ------------------------------------------------------

    def file_appeal(
        self,
        docent_id: str,
        activity_id: str,
        review_ids: Sequence[str],
        reason: str,
    ) -> CaseSummary:
        """讲解员对可疑评价发起申诉，系统建复核案件而不是直接改分。"""
        self._require_activity_access(docent_id, activity_id)
        for review_id in review_ids:
            review = self._store.reviews.get(review_id)
            if (
                review is not None
                and review.activity_id == activity_id
                and review.docent_id != docent_id
            ):
                raise PermissionError("只能对本讲解员负责活动的评价发起申诉")
        case = self._cases.open_case(
            activity_id=activity_id,
            review_ids=review_ids,
            reason=reason,
            opened_by=docent_id,
        )
        return self._summary(case)

    def withdraw_appeal(self, docent_id: str, case_id: str, note: str = "") -> CaseSummary:
        case = self._owned_case(docent_id, case_id)
        return self._summary(self._cases.withdraw(case.case_id, actor=docent_id, note=note))

    # -- 查询 ----------------------------------------------------------

    def list_cases(self, docent_id: str, activity_id: str) -> list[CaseSummary]:
        self._require_activity_access(docent_id, activity_id)
        return [
            self._summary(c)
            for c in self._store.cases_of(activity_id)
            if c.docent_id == docent_id
        ]

    def case_reviews(self, docent_id: str, case_id: str) -> list[DocentReviewView]:
        case = self._owned_case(docent_id, case_id)
        return [self._view(self._store.reviews[rid]) for rid in case.review_ids]

    def case_timeline(self, docent_id: str, case_id: str) -> list[CaseEvent]:
        return list(self._owned_case(docent_id, case_id).events)

    def activity_grade(self, docent_id: str, activity_id: str) -> GradeReport:
        self._require_activity_access(docent_id, activity_id)
        return self._publishing.grade_report(activity_id)

    def impact_explanation(self, docent_id: str, case_id: str) -> ImpactExplanation:
        """若本案可疑评价被排除，当前已发布评价集的等级会如何变化。"""
        case = self._owned_case(docent_id, case_id)
        report = self._publishing.grade_report(case.activity_id)
        published = self._store.published_sets[report.set_id]
        flagged = set(case.review_ids)
        remaining = [e for e in published.entries if e.review_id not in flagged]
        affected = len(published.entries) - len(remaining)
        if remaining:
            hyp_average = round(sum(e.normalized_score for e in remaining) / len(remaining), 2)
            hyp_grade = grade_band(hyp_average)
            summary = (
                f"本案涉及 {len(case.review_ids)} 条可疑评价，其中 {affected} 条在当前已发布评价集 "
                f"v{report.version} 中；若复核采纳并重新发布，平均分将由 {report.average_score}"
                f"（{report.grade} 级）变为 {hyp_average}（{hyp_grade} 级）。"
                "该结果为估算，实际以复核决定并重新发布为准，原始评价不会被修改或删除。"
            )
        else:
            hyp_average = None
            hyp_grade = None
            summary = (
                f"本案涉及当前已发布评价集 v{report.version} 的全部评价，"
                "若全部排除将没有可计算等级的评价，需等待新的有效评价。"
            )
        return ImpactExplanation(
            case_id=case.case_id,
            case_status=case.status.value,
            current_set_id=report.set_id,
            current_version=report.version,
            current_average=report.average_score,
            current_grade=report.grade,
            flagged_in_current_set=affected,
            hypothetical_average=hyp_average,
            hypothetical_grade=hyp_grade,
            flagged_reviews=tuple(self._view(self._store.reviews[rid]) for rid in case.review_ids),
            summary=summary,
        )

    # -- 内部 ----------------------------------------------------------

    def _view(self, review: Review) -> DocentReviewView:
        return DocentReviewView(
            review_id=review.review_id,
            source=review.source.value,
            identity_verification=review.identity_verification.value,
            rating_version=review.rating_version,
            score=review.score,
            comment=review.comment,
            submitted_at=review.submitted_at.isoformat(),
            reviewer_alias=reviewer_alias(review.reviewer.account_id),
            reviewer_display=mask_display_name(review.reviewer.display_name),
        )

    def _summary(self, case: ReviewCase) -> CaseSummary:
        return CaseSummary(
            case_id=case.case_id,
            status=case.status.value,
            reason=case.reason,
            review_count=len(case.review_ids),
            event_count=len(case.events),
        )

    def _owned_case(self, docent_id: str, case_id: str) -> ReviewCase:
        case = self._store.cases.get(case_id)
        if case is None:
            raise KeyError(f"案件不存在：{case_id}")
        if case.docent_id != docent_id:
            raise PermissionError("无权查看其他讲解员的复核案件")
        return case

    def _require_activity_access(self, docent_id: str, activity_id: str) -> None:
        reviews = self._store.reviews_of(activity_id)
        if not any(r.docent_id == docent_id for r in reviews):
            raise PermissionError("无权访问其他讲解员负责的活动")
