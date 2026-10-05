"""活动评价异常申诉后端服务。

核心原则：
- 可疑评价只立案复核，系统永不自动改分；
- 合并重复、部分采纳、申诉撤回、决定重开均以追加事件记录，原评分与原决定保留存档；
- 等级计算只接受已发布评价集，发布即冻结；
- 讲解员视图输出影响解释，并裁剪评价者敏感信息。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from .models import (
    IDENTITY_WEIGHTS,
    AnomalyKind,
    AnomalyRule,
    CaseEvent,
    CaseEventKind,
    CaseStatus,
    DecisionKind,
    GradeReport,
    IdentityLevel,
    PublishedEntry,
    PublishedReviewSet,
    RatingVersion,
    Review,
    ReviewCase,
    ReviewChannel,
    ReviewerProfile,
    grade_for,
)

DEFAULT_RULES = (
    AnomalyRule("AR-DUP-1", AnomalyKind.DUPLICATE_SUBMISSION, 1, "同一账号对同一活动重复提交评价"),
    AnomalyRule("AR-DEV-2", AnomalyKind.ABNORMAL_ACCOUNT, 2, "同一设备指纹在同一活动下出现多账号评价"),
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _check_score(score: float) -> float:
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        raise ValueError("评分必须是数字")
    value = float(score)
    if not 0.0 <= value <= 5.0:
        raise ValueError("评分必须介于 0 与 5 之间")
    return value


class ReviewAppealService:
    """评价接收、异常复核、发布冻结与讲解员视图的应用服务。"""

    def __init__(self, rules: tuple[AnomalyRule, ...] = DEFAULT_RULES) -> None:
        self._reviews: dict[str, Review] = {}
        self._cases: dict[str, ReviewCase] = {}
        self._rules: dict[str, AnomalyRule] = {}
        self._sets: dict[str, PublishedReviewSet] = {}
        self._seq = {"review": 0, "case": 0, "set": 0}
        for rule in rules:
            self.add_anomaly_rule(rule)

    # ---- 异常规则 ----

    def add_anomaly_rule(self, rule: AnomalyRule) -> None:
        """保存异常规则；threshold 为同一活动内允许的最大次数，超出即立案。"""
        if rule.rule_id in self._rules:
            raise ValueError(f"异常规则已存在：{rule.rule_id}")
        self._rules[rule.rule_id] = rule

    def list_rules(self) -> list[AnomalyRule]:
        return list(self._rules.values())

    # ---- 评价接收 ----

    def register_review(
        self,
        *,
        activity_id: str,
        reviewer: ReviewerProfile,
        channel: ReviewChannel | str,
        identity_level: IdentityLevel | str,
        score: float,
        comment: str,
        at: datetime | None = None,
    ) -> Review:
        """保存评价来源、身份验证级别与首个评分版本，并按异常规则立案（不改分）。"""
        at = at or _now()
        self._seq["review"] += 1
        review = Review(
            review_id=f"RV-{self._seq['review']:04d}",
            activity_id=activity_id,
            reviewer=reviewer,
            channel=ReviewChannel(channel),
            identity_level=IdentityLevel(identity_level),
            created_at=at,
        )
        review.versions.append(RatingVersion(1, _check_score(score), comment, "首次提交", at))
        self._reviews[review.review_id] = review
        self._flag_anomalies(review, at)
        return review

    def append_rating_version(
        self,
        review_id: str,
        *,
        score: float,
        comment: str,
        reason: str,
        at: datetime | None = None,
    ) -> RatingVersion:
        """追加评分版本，历史版本保留不变。"""
        review = self._require_review(review_id)
        version = RatingVersion(len(review.versions) + 1, _check_score(score), comment, reason, at or _now())
        review.versions.append(version)
        return version

    def get_review(self, review_id: str) -> Review:
        return self._require_review(review_id)

    # ---- 复核案件 ----

    def get_case(self, case_id: str) -> ReviewCase:
        return self._require_case(case_id)

    def list_cases(self, activity_id: str | None = None) -> list[ReviewCase]:
        cases = list(self._cases.values())
        if activity_id is not None:
            cases = [c for c in cases if c.activity_id == activity_id]
        return cases

    def decide_merge_duplicate(
        self,
        case_id: str,
        *,
        target_review_id: str,
        operator: str,
        at: datetime | None = None,
    ) -> ReviewCase:
        """合并重复：被合并评价原评分保留存档，自下次发布起不计入评价集。"""
        case = self._require_pending_case(case_id)
        source = self._reviews[case.review_id]
        target = self._reviews.get(target_review_id)
        if target is None:
            raise ValueError(f"目标评价不存在：{target_review_id}")
        if target.review_id == source.review_id:
            raise ValueError("不能将评价合并到自身")
        if target.activity_id != source.activity_id:
            raise ValueError("只能合并同一活动下的评价")
        target_decision = self._active_decision_for(target.review_id)
        if target_decision and target_decision["decision"] is DecisionKind.MERGE_DUPLICATE:
            raise ValueError("目标评价已被合并，不能作为合并目标")
        self._append_event(case, CaseEventKind.DECIDED, operator, {
            "decision": DecisionKind.MERGE_DUPLICATE,
            "target_review_id": target.review_id,
            "original_score": source.current_score,
        }, at)
        return case

    def decide_partial_adoption(
        self,
        case_id: str,
        *,
        adopted_score: float,
        operator: str,
        at: datetime | None = None,
    ) -> ReviewCase:
        """部分采纳：发布取值调整为采纳值，原始评分保留存档。"""
        case = self._require_pending_case(case_id)
        source = self._reviews[case.review_id]
        self._append_event(case, CaseEventKind.DECIDED, operator, {
            "decision": DecisionKind.PARTIAL_ADOPTION,
            "adopted_score": _check_score(adopted_score),
            "original_score": source.current_score,
        }, at)
        return case

    def withdraw_appeal(
        self,
        case_id: str,
        *,
        operator: str,
        at: datetime | None = None,
    ) -> ReviewCase:
        """申诉撤回：未决案件直接结案，评价维持原评分，记录保留。"""
        case = self._require_pending_case(case_id)
        self._append_event(case, CaseEventKind.APPEAL_WITHDRAWN, operator, {"note": "申诉撤回，维持原评分"}, at)
        return case

    def reopen_decision(
        self,
        case_id: str,
        *,
        operator: str,
        reason: str,
        at: datetime | None = None,
    ) -> ReviewCase:
        """决定重开：旧决定保留存档但不再生效，案件回到待复核。"""
        case = self._require_case(case_id)
        if case.status is not CaseStatus.DECIDED:
            raise ValueError("仅已决定的案件可以重开")
        self._append_event(case, CaseEventKind.DECISION_REOPENED, operator, {
            "reason": reason,
            "note": "原决定保留存档但不再生效",
        }, at)
        return case

    # ---- 发布与等级 ----

    def publish_review_set(
        self,
        activity_id: str,
        *,
        operator: str,
        at: datetime | None = None,
    ) -> PublishedReviewSet:
        """冻结当前有效评价为已发布评价集；生效决定在此刻固化，此后案件变化不影响本集。"""
        reviews = self._reviews_of(activity_id)
        if not reviews:
            raise ValueError("活动暂无评价，无法发布评价集")
        entries: list[PublishedEntry] = []
        for review in reviews:
            decision = self._active_decision_for(review.review_id)
            if decision and decision["decision"] is DecisionKind.MERGE_DUPLICATE:
                continue  # 已合并：原评分保留在档案，不进入发布集
            if decision and decision["decision"] is DecisionKind.PARTIAL_ADOPTION:
                score, note = decision["adopted_score"], "部分采纳值（原评分保留存档）"
            else:
                score, note = review.current_score, "原始评分"
            entries.append(PublishedEntry(
                review_id=review.review_id,
                score=score,
                weight=IDENTITY_WEIGHTS[review.identity_level],
                identity_level=review.identity_level,
                source_note=note,
            ))
        if not entries:
            raise ValueError("活动评价均已合并，无法发布空评价集")
        payload = json.dumps(
            [
                {
                    "review_id": e.review_id,
                    "score": e.score,
                    "weight": e.weight,
                    "identity_level": e.identity_level.value,
                    "source_note": e.source_note,
                }
                for e in entries
            ],
            ensure_ascii=False,
            sort_keys=True,
        )
        self._seq["set"] += 1
        published = PublishedReviewSet(
            set_id=f"PS-{self._seq['set']:04d}",
            activity_id=activity_id,
            entries=tuple(entries),
            published_by=operator,
            published_at=at or _now(),
            digest=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        )
        self._sets[published.set_id] = published
        return published

    def get_published_set(self, set_id: str) -> PublishedReviewSet:
        published = self._sets.get(set_id)
        if published is None:
            raise ValueError(f"评价集不存在或未发布：{set_id}")
        return published

    def compute_grade(self, set_id: str) -> GradeReport:
        """等级计算只绑定已发布评价集，不读取实时评价。"""
        published = self.get_published_set(set_id)
        total_weight = sum(e.weight for e in published.entries)
        if total_weight <= 0:
            raise ValueError("已发布评价集为空，无法计算等级")
        average = round(sum(e.score * e.weight for e in published.entries) / total_weight, 4)
        return GradeReport(
            set_id=published.set_id,
            activity_id=published.activity_id,
            review_count=len(published.entries),
            weighted_average=average,
            grade=grade_for(average),
        )

    # ---- 讲解员视图 ----

    def docent_activity_view(self, activity_id: str) -> dict:
        """讲解员接口：展示异常影响解释，裁剪评价者敏感信息。"""
        reviews = self._reviews_of(activity_id)
        cases = self._cases_of(activity_id)
        latest_set = self._latest_set(activity_id)
        entry_by_review = {e.review_id: e for e in latest_set.entries} if latest_set else {}

        explanations = [self._explain_case(c, latest_set, entry_by_review) for c in cases]
        pending = sum(1 for c in cases if c.status is CaseStatus.PENDING)
        grade = self.compute_grade(latest_set.set_id) if latest_set else None
        if latest_set and grade:
            explanations.append(
                f"当前等级「{grade.grade}」（{grade.weighted_average}）依据已发布评价集 "
                f"{latest_set.set_id} 的 {grade.review_count} 条评价计算；"
                f"待复核案件 {pending} 起，复核结果将在下次发布后生效。"
            )
        else:
            explanations.append(f"尚无已发布评价集，等级未生成；待复核案件 {pending} 起。")

        return {
            "activity_id": activity_id,
            "published_grade": None if grade is None else {
                "set_id": grade.set_id,
                "review_count": grade.review_count,
                "weighted_average": grade.weighted_average,
                "grade": grade.grade,
            },
            "pending_case_count": pending,
            "impact_explanation": explanations,
            "reviews": [self._trim_review(r) for r in reviews],
        }

    # ---- 内部 ----

    def _flag_anomalies(self, review: Review, at: datetime) -> None:
        """按异常规则立案；只建案件，绝不改动评分。"""
        peers = [r for r in self._reviews_of(review.activity_id) if r.review_id != review.review_id]
        for rule in self._rules.values():
            if rule.kind is AnomalyKind.DUPLICATE_SUBMISSION:
                hits = sum(1 for r in peers if r.reviewer.account_id == review.reviewer.account_id)
            else:
                hits = sum(1 for r in peers if r.reviewer.device_fingerprint == review.reviewer.device_fingerprint)
            if hits >= rule.threshold:
                self._open_case(review, rule, at)

    def _open_case(self, review: Review, rule: AnomalyRule, at: datetime) -> ReviewCase:
        for case in self._cases.values():
            if case.review_id == review.review_id and case.rule_id == rule.rule_id:
                return case
        self._seq["case"] += 1
        case = ReviewCase(
            case_id=f"CA-{self._seq['case']:04d}",
            review_id=review.review_id,
            activity_id=review.activity_id,
            rule_id=rule.rule_id,
            rule_kind=rule.kind,
            opened_at=at,
        )
        self._append_event(case, CaseEventKind.OPENED, "系统", {"rule": rule.description}, at)
        self._cases[case.case_id] = case
        return case

    def _append_event(self, case: ReviewCase, kind: CaseEventKind, operator: str, detail: dict, at: datetime | None) -> None:
        case.events.append(CaseEvent(len(case.events) + 1, kind, operator, detail, at or _now()))

    def _require_review(self, review_id: str) -> Review:
        review = self._reviews.get(review_id)
        if review is None:
            raise ValueError(f"评价不存在：{review_id}")
        return review

    def _require_case(self, case_id: str) -> ReviewCase:
        case = self._cases.get(case_id)
        if case is None:
            raise ValueError(f"复核案件不存在：{case_id}")
        return case

    def _require_pending_case(self, case_id: str) -> ReviewCase:
        case = self._require_case(case_id)
        if case.status is not CaseStatus.PENDING:
            raise ValueError(f"案件 {case_id} 当前为「{case.status.value}」，仅待复核案件可执行该操作")
        return case

    def _reviews_of(self, activity_id: str) -> list[Review]:
        return [r for r in self._reviews.values() if r.activity_id == activity_id]

    def _cases_of(self, activity_id: str) -> list[ReviewCase]:
        return [c for c in self._cases.values() if c.activity_id == activity_id]

    def _latest_set(self, activity_id: str) -> PublishedReviewSet | None:
        sets = [s for s in self._sets.values() if s.activity_id == activity_id]
        return sets[-1] if sets else None

    def _active_decision_for(self, review_id: str) -> dict | None:
        """该评价当前生效的复核决定；同一评价多起案件时以最后生效者为准。"""
        active = None
        for case in self._cases.values():
            if case.review_id == review_id and case.active_decision is not None:
                active = case.active_decision
        return active

    def _trim_review(self, review: Review) -> dict:
        """匿名信息裁剪：仅保留渠道与验证级别，不出现任何评价者敏感信息。"""
        decision = self._active_decision_for(review.review_id)
        if decision and decision["decision"] is DecisionKind.MERGE_DUPLICATE:
            status = "已合并"
        elif decision and decision["decision"] is DecisionKind.PARTIAL_ADOPTION:
            status = "部分采纳"
        elif any(c.review_id == review.review_id and c.status is CaseStatus.PENDING for c in self._cases.values()):
            status = "复核中"
        else:
            status = "有效"
        return {
            "review_id": review.review_id,
            "reviewer_ref": f"评价者-{review.review_id}",
            "channel": review.channel.value,
            "identity_level": review.identity_level.value,
            "original_score": review.original_score,
            "current_score": review.current_score,
            "version_count": len(review.versions),
            "status": status,
        }

    def _explain_case(self, case: ReviewCase, latest_set: PublishedReviewSet | None, entry_by_review: dict) -> str:
        review = self._reviews[case.review_id]
        last = case.events[-1]
        rid, cid = case.review_id, case.case_id
        if last.kind is CaseEventKind.OPENED:
            return (
                f"评价 {rid} 触发「{case.rule_kind.value}」规则，案件 {cid} 待复核；"
                f"评分保持原值 {review.current_score}，系统不会自动改分。"
            )
        if last.kind is CaseEventKind.DECISION_REOPENED:
            return (
                f"评价 {rid} 的案件 {cid} 已决定重开（原因：{last.detail.get('reason', '未填写')}），"
                f"等待重新复核；此前决定保留存档但暂不生效，评分维持原值 {review.current_score}。"
            )
        if last.kind is CaseEventKind.APPEAL_WITHDRAWN:
            return f"评价 {rid} 的案件 {cid} 申诉已撤回，未调整任何分数，原评分 {review.current_score} 保留。"
        decision = last.detail["decision"]
        if decision is DecisionKind.MERGE_DUPLICATE:
            target = last.detail["target_review_id"]
            if latest_set is None:
                suffix = "不计入后续发布评价集。"
            elif rid in entry_by_review:
                suffix = f"当前等级仍依据已发布评价集 {latest_set.set_id}（含该评分），自下次发布起不再计入。"
            else:
                suffix = f"已不计入最新发布评价集 {latest_set.set_id}。"
            return (
                f"评价 {rid} 经案件 {cid} 复核认定为重复提交，已合并至 {target}；"
                f"原评分 {last.detail['original_score']} 保留存档，{suffix}"
            )
        adopted = last.detail["adopted_score"]
        if latest_set is None:
            suffix = "新取值将在首次发布后生效。"
        else:
            entry = entry_by_review.get(rid)
            if entry is not None and entry.score != adopted:
                suffix = f"当前已发布评价集 {latest_set.set_id} 仍取 {entry.score}，新取值自下次发布起生效。"
            else:
                suffix = f"最新发布评价集 {latest_set.set_id} 已按 {adopted} 取值。"
        return (
            f"评价 {rid} 经案件 {cid} 复核部分采纳：发布取值 {adopted}，"
            f"原始评分 {last.detail['original_score']} 保留存档；{suffix}"
        )
