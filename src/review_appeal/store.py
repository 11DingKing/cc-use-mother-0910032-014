"""内存存储。持久化实现可替换，领域服务只依赖本模块的容器接口。"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import AnomalyRule, PublishedReviewSet, Review, ReviewCase


@dataclass
class InMemoryStore:
    reviews: dict[str, Review] = field(default_factory=dict)
    rules: dict[str, AnomalyRule] = field(default_factory=dict)
    cases: dict[str, ReviewCase] = field(default_factory=dict)
    published_sets: dict[str, PublishedReviewSet] = field(default_factory=dict)
    current_set_by_activity: dict[str, str] = field(default_factory=dict)
    case_seq: int = 0

    def next_case_id(self) -> str:
        self.case_seq += 1
        return f"C-{self.case_seq:04d}"

    def reviews_of(self, activity_id: str) -> list[Review]:
        items = [r for r in self.reviews.values() if r.activity_id == activity_id]
        return sorted(items, key=lambda r: (r.submitted_at, r.review_id))

    def cases_of(self, activity_id: str) -> list[ReviewCase]:
        return [c for c in self.cases.values() if c.activity_id == activity_id]

    def enabled_rules(self) -> list[AnomalyRule]:
        return [r for r in self.rules.values() if r.enabled]

    def published_version(self, activity_id: str) -> int:
        return sum(1 for ps in self.published_sets.values() if ps.activity_id == activity_id)
