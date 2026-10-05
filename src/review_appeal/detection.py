"""异常检测引擎：规则命中只产生信号，交案件服务建复核案件，绝不自动改分。"""
from __future__ import annotations

from collections import defaultdict

from .models import AnomalyRule, AnomalySignal, IdentityVerification, Review, RuleKind
from .store import InMemoryStore


class DetectionEngine:
    """对单个活动的评价执行已启用规则。"""

    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    def evaluate(self, activity_id: str) -> list[AnomalySignal]:
        reviews = self._store.reviews_of(activity_id)
        signals: list[AnomalySignal] = []
        for rule in self._store.enabled_rules():
            if rule.kind is RuleKind.DUPLICATE_SUBMISSION:
                signals.extend(self._duplicate_signals(rule, reviews))
            elif rule.kind is RuleKind.ABNORMAL_ACCOUNT:
                signals.extend(self._abnormal_account_signals(rule, reviews))
        return signals

    @staticmethod
    def _duplicate_signals(rule: AnomalyRule, reviews: list[Review]) -> list[AnomalySignal]:
        """同一提交指纹（缺省时退化为账号）出现多次，疑似重复提交。"""
        threshold = int(rule.params.get("min_occurrences", 2))
        groups: dict[str, list[Review]] = defaultdict(list)
        for review in reviews:
            groups[review.fingerprint or review.reviewer.account_id].append(review)
        signals = []
        for key in sorted(groups):
            group = sorted(groups[key], key=lambda r: (r.submitted_at, r.review_id))
            if len(group) >= threshold:
                signals.append(AnomalySignal(
                    rule_id=rule.rule_id,
                    kind=rule.kind,
                    review_ids=tuple(r.review_id for r in group),
                    reason=f"同一提交指纹在本活动出现 {len(group)} 次，疑似重复提交",
                ))
        return signals

    def _abnormal_account_signals(self, rule: AnomalyRule, reviews: list[Review]) -> list[AnomalySignal]:
        """未实名账号跨活动集中提交，疑似异常账号。"""
        max_activities = int(rule.params.get("max_activities", 3))
        activities_by_account: dict[str, set[str]] = defaultdict(set)
        for review in self._store.reviews.values():
            activities_by_account[review.reviewer.account_id].add(review.activity_id)
        flagged: dict[str, list[Review]] = defaultdict(list)
        for review in reviews:
            if review.identity_verification is IdentityVerification.VERIFIED:
                continue
            if len(activities_by_account[review.reviewer.account_id]) >= max_activities:
                flagged[review.reviewer.account_id].append(review)
        return [
            AnomalySignal(
                rule_id=rule.rule_id,
                kind=rule.kind,
                review_ids=tuple(
                    r.review_id for r in sorted(group, key=lambda r: (r.submitted_at, r.review_id))
                ),
                reason=f"未实名账号跨 {len(activities_by_account[account])} 个活动集中提交评价，疑似异常账号",
            )
            for account, group in sorted(flagged.items())
        ]
