"""已发布评价集与等级计算。

发布即冻结：等级只读取已发布评价集的快照，新评价与复核决定
都要等下一次发布才会反映到等级上。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from .cases import CaseService
from .models import GRADE_BANDS, RATING_SCALES, PublishedEntry, PublishedReviewSet, Review
from .store import InMemoryStore


class PublishError(ValueError):
    """发布或等级计算的前置条件不满足。"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_score(review: Review) -> float:
    """按评分版本把原始分归一化到百分制，原始分本身不变。"""
    scale = RATING_SCALES[review.rating_version]
    return round(review.score / scale * 100, 4)


def grade_band(average: float) -> str:
    for floor, band in GRADE_BANDS:
        if average >= floor:
            return band
    return GRADE_BANDS[-1][1]


@dataclass(frozen=True)
class GradeReport:
    """一次等级计算结果，绑定具体的已发布评价集版本。"""

    activity_id: str
    set_id: str
    version: int
    review_count: int
    average_score: float
    grade: str


class PublishingService:
    def __init__(
        self,
        store: InMemoryStore,
        case_service: CaseService,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._store = store
        self._cases = case_service
        self._clock = clock

    def publish(self, activity_id: str, *, actor: str) -> PublishedReviewSet:
        """冻结当前有效评价为新一版发布集；生效排除来自已决定的复核案件。"""
        excluded = self._cases.effective_exclusions(activity_id)
        entries = tuple(
            PublishedEntry(
                review_id=review.review_id,
                rating_version=review.rating_version,
                normalized_score=normalize_score(review),
            )
            for review in self._store.reviews_of(activity_id)
            if review.review_id not in excluded
        )
        if not entries:
            raise PublishError("没有可发布的评价")
        version = self._store.published_version(activity_id) + 1
        published = PublishedReviewSet(
            set_id=f"{activity_id}-v{version}",
            activity_id=activity_id,
            version=version,
            entries=entries,
            published_at=self._clock(),
        )
        self._store.published_sets[published.set_id] = published
        self._store.current_set_by_activity[activity_id] = published.set_id
        return published

    def grade_report(self, activity_id: str) -> GradeReport:
        """活动当前等级，绑定当前已发布评价集；未发布则不能计算。"""
        set_id = self._store.current_set_by_activity.get(activity_id)
        if set_id is None:
            raise PublishError(f"活动 {activity_id} 尚未发布评价集，无法计算等级")
        return self.grade_of_set(set_id)

    def grade_of_set(self, set_id: str) -> GradeReport:
        """历史发布集的等级：冻结集可随时重算且结果不变。"""
        published = self._store.published_sets.get(set_id)
        if published is None:
            raise PublishError(f"发布集不存在：{set_id}")
        average = round(sum(e.normalized_score for e in published.entries) / len(published.entries), 2)
        return GradeReport(
            activity_id=published.activity_id,
            set_id=published.set_id,
            version=published.version,
            review_count=len(published.entries),
            average_score=average,
            grade=grade_band(average),
        )
