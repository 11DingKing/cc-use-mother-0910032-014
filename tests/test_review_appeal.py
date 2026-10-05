"""活动评价异常申诉后端的行为测试。"""
from __future__ import annotations

import sys
import unittest
from dataclasses import FrozenInstanceError, asdict, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from review_appeal import (
    AnomalyRule,
    CaseError,
    CaseEventType,
    CaseService,
    CaseStatus,
    DecisionType,
    DetectionEngine,
    DocentAPI,
    IdentityVerification,
    InMemoryStore,
    PublishingService,
    PublishError,
    Review,
    ReviewerIdentity,
    ReviewSource,
    RuleKind,
)

BASE = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)


def make_review(
    review_id: str,
    activity_id: str = "act-1",
    docent_id: str = "docent-1",
    *,
    score: float,
    version: str = "v1",
    source: ReviewSource = ReviewSource.SCHOOL_PORTAL,
    verification: IdentityVerification = IdentityVerification.VERIFIED,
    account: str | None = None,
    fingerprint: str | None = None,
    name: str = "张华",
    contact: str = "13800001111",
    seq: int = 0,
) -> Review:
    account = account or f"acct-{review_id}"
    return Review(
        review_id=review_id,
        activity_id=activity_id,
        docent_id=docent_id,
        source=source,
        identity_verification=verification,
        rating_version=version,
        score=score,
        comment=f"评价 {review_id}",
        reviewer=ReviewerIdentity(account_id=account, display_name=name, contact=contact),
        submitted_at=BASE + timedelta(hours=seq),
        fingerprint=fingerprint or f"fp-{account}",
    )


def build_store() -> InMemoryStore:
    """讲解员发现低分来自重复提交与异常账号的典型场景。"""
    store = InMemoryStore()
    store.rules["dup"] = AnomalyRule(
        "dup", RuleKind.DUPLICATE_SUBMISSION, "重复提交", "同一指纹多次提交", {"min_occurrences": 2}
    )
    store.rules["abnormal"] = AnomalyRule(
        "abnormal", RuleKind.ABNORMAL_ACCOUNT, "异常账号", "未实名账号跨活动刷评", {"max_activities": 3}
    )
    # 正常评价：实名、学校门户，百分制折合 100 与 90
    store.reviews["r1"] = make_review("r1", score=5.0, account="acct-school-1", seq=1)
    store.reviews["r2"] = make_review("r2", score=9.0, version="v2", account="acct-school-2", seq=2)
    # 重复提交：同一指纹两条匿名 1 分
    store.reviews["r3"] = make_review(
        "r3", score=1.0, source=ReviewSource.PUBLIC_WEB,
        verification=IdentityVerification.ANONYMOUS, account="acct-dup", fingerprint="fp-dup", seq=3,
    )
    store.reviews["r4"] = make_review(
        "r4", score=1.0, source=ReviewSource.PUBLIC_WEB,
        verification=IdentityVerification.ANONYMOUS, account="acct-dup", fingerprint="fp-dup", seq=4,
    )
    # 异常账号：匿名账号跨 3 个活动刷低分
    store.reviews["r5"] = make_review(
        "r5", score=1.0, source=ReviewSource.PUBLIC_WEB,
        verification=IdentityVerification.ANONYMOUS, account="acct-bot", seq=5,
    )
    store.reviews["x1"] = make_review(
        "x1", activity_id="act-2", docent_id="docent-9", score=1.0,
        verification=IdentityVerification.ANONYMOUS, account="acct-bot", seq=6,
    )
    store.reviews["x2"] = make_review(
        "x2", activity_id="act-3", docent_id="docent-9", score=1.0,
        verification=IdentityVerification.ANONYMOUS, account="acct-bot", seq=7,
    )
    return store


def build_services(store: InMemoryStore) -> tuple[CaseService, PublishingService, DocentAPI]:
    clock = lambda: BASE  # noqa: E731 - 测试用固定时钟
    cases = CaseService(store, clock)
    publishing = PublishingService(store, cases, clock)
    return cases, publishing, DocentAPI(store, cases, publishing)


class ReviewRecordTest(unittest.TestCase):
    def test_review_stores_source_verification_and_rating_version(self) -> None:
        review = make_review(
            "r1", score=8.0, version="v2",
            source=ReviewSource.EMAIL_LINK, verification=IdentityVerification.BASIC,
        )
        self.assertEqual(review.source, ReviewSource.EMAIL_LINK)
        self.assertEqual(review.identity_verification, IdentityVerification.BASIC)
        self.assertEqual(review.rating_version, "v2")
        self.assertEqual(review.reviewer.contact, "13800001111")

    def test_review_is_immutable(self) -> None:
        review = make_review("r1", score=4.0)
        with self.assertRaises(FrozenInstanceError):
            review.score = 1.0  # type: ignore[misc]

    def test_score_must_fit_rating_version(self) -> None:
        with self.assertRaises(ValueError):
            make_review("bad", score=6.0)  # v1 满分 5
        with self.assertRaises(ValueError):
            make_review("bad2", score=1.0, version="v9")


class DetectionTest(unittest.TestCase):
    def test_signals_open_cases_without_touching_scores(self) -> None:
        store = build_store()
        cases, _, _ = build_services(store)
        signals = DetectionEngine(store).evaluate("act-1")
        self.assertEqual(len(signals), 2)
        opened = cases.open_cases_for_signals(signals)
        self.assertEqual(len(opened), 2)
        by_rule = {c.rule_ids[0]: c for c in opened}
        self.assertEqual(by_rule["dup"].review_ids, ["r3", "r4"])
        self.assertEqual(by_rule["abnormal"].review_ids, ["r5"])
        # 建案不改分、不动发布集
        self.assertEqual(store.reviews["r3"].score, 1.0)
        self.assertEqual(store.reviews["r5"].score, 1.0)
        self.assertEqual(store.current_set_by_activity, {})

    def test_active_case_prevents_duplicate_opening(self) -> None:
        store = build_store()
        cases, _, _ = build_services(store)
        engine = DetectionEngine(store)
        first = cases.open_cases_for_signals(engine.evaluate("act-1"))
        second = cases.open_cases_for_signals(engine.evaluate("act-1"))
        self.assertEqual(len(first), 2)
        self.assertEqual(second, [])


class CaseFlowTest(unittest.TestCase):
    def test_merge_keeps_source_case_and_original_reviews(self) -> None:
        store = build_store()
        cases, _, _ = build_services(store)
        target = cases.open_case(
            activity_id="act-1", review_ids=["r3"], reason="讲解员申诉：疑似重复", opened_by="docent-1"
        )
        source = cases.open_case(
            activity_id="act-1", review_ids=["r4"], reason="重复提交信号", opened_by="system:detection"
        )
        cases.merge(target.case_id, [source.case_id], actor="venue-admin")
        self.assertEqual(target.status, CaseStatus.OPEN)
        self.assertEqual(target.review_ids, ["r3", "r4"])
        self.assertEqual(source.status, CaseStatus.MERGED)
        self.assertEqual(source.events[-1].event_type, CaseEventType.MERGED_INTO)
        self.assertEqual(source.events[-1].related_case_id, target.case_id)
        self.assertEqual(target.events[-1].event_type, CaseEventType.ABSORBED)
        # 原案件与原评价都还在，原值未动
        self.assertIn(source.case_id, store.cases)
        self.assertEqual(store.reviews["r4"].score, 1.0)
        # 已合并案件不能再处置
        with self.assertRaises(CaseError):
            cases.decide(source.case_id, actor="venue-admin", decision=DecisionType.REJECT)

    def test_partial_adopt_preserves_scores_and_applies_to_next_publish(self) -> None:
        store = build_store()
        cases, publishing, _ = build_services(store)
        case = cases.open_case(
            activity_id="act-1", review_ids=["r3", "r4"], reason="重复提交", opened_by="system:detection"
        )
        first = publishing.publish("act-1", actor="venue-admin")
        self.assertEqual(first.version, 1)
        self.assertEqual(publishing.grade_report("act-1").grade, "E")
        cases.decide(
            case.case_id, actor="venue-admin",
            decision=DecisionType.PARTIAL_ADOPT, excluded_review_ids=["r3", "r4"],
            note="采纳重复提交部分",
        )
        # 原值保留
        self.assertEqual(store.reviews["r3"].score, 1.0)
        self.assertEqual(store.reviews["r4"].score, 1.0)
        # 已发布集冻结：等级不变
        self.assertEqual(publishing.grade_report("act-1").grade, "E")
        self.assertEqual(len(store.published_sets[first.set_id].entries), 5)
        # 重新发布后才生效
        second = publishing.publish("act-1", actor="venue-admin")
        self.assertEqual(second.version, 2)
        self.assertEqual([e.review_id for e in second.entries], ["r1", "r2", "r5"])
        report = publishing.grade_report("act-1")
        self.assertEqual(report.average_score, 70.0)  # (100+90+20)/3
        self.assertEqual(report.grade, "C")
        # 历史发布集仍可重算且不变
        self.assertEqual(publishing.grade_of_set(first.set_id).grade, "E")

    def test_partial_adopt_validates_exclusions(self) -> None:
        store = build_store()
        cases, _, _ = build_services(store)
        case = cases.open_case(
            activity_id="act-1", review_ids=["r3"], reason="重复提交", opened_by="system:detection"
        )
        with self.assertRaises(CaseError):
            cases.decide(case.case_id, actor="a", decision=DecisionType.PARTIAL_ADOPT)
        with self.assertRaises(CaseError):
            cases.decide(
                case.case_id, actor="a",
                decision=DecisionType.PARTIAL_ADOPT, excluded_review_ids=["r5"],
            )

    def test_reject_keeps_everything(self) -> None:
        store = build_store()
        cases, _, _ = build_services(store)
        case = cases.open_case(
            activity_id="act-1", review_ids=["r5"], reason="异常账号", opened_by="system:detection"
        )
        cases.decide(case.case_id, actor="venue-admin", decision=DecisionType.REJECT, note="证据不足")
        self.assertEqual(case.status, CaseStatus.DECIDED)
        self.assertEqual(cases.effective_exclusions("act-1"), set())

    def test_withdraw_keeps_case_and_history(self) -> None:
        store = build_store()
        cases, _, api = build_services(store)
        summary = api.file_appeal("docent-1", "act-1", ["r5"], "该账号明显异常")
        # 非发起人不能撤回
        with self.assertRaises(CaseError):
            cases.withdraw(summary.case_id, actor="venue-admin")
        done = api.withdraw_appeal("docent-1", summary.case_id, note="学校补充说明后撤回")
        self.assertEqual(done.status, "withdrawn")
        case = store.cases[summary.case_id]
        self.assertEqual(
            [e.event_type for e in case.events],
            [CaseEventType.OPENED, CaseEventType.WITHDRAWN],
        )
        self.assertEqual(cases.effective_exclusions("act-1"), set())
        # 撤回后不能再决定
        with self.assertRaises(CaseError):
            cases.decide(
                case.case_id, actor="venue-admin",
                decision=DecisionType.PARTIAL_ADOPT, excluded_review_ids=["r5"],
            )

    def test_reopen_keeps_decision_history_and_clears_effect(self) -> None:
        store = build_store()
        cases, _, _ = build_services(store)
        case = cases.open_case(
            activity_id="act-1", review_ids=["r3", "r4"], reason="重复提交", opened_by="system:detection"
        )
        cases.decide(
            case.case_id, actor="venue-admin",
            decision=DecisionType.PARTIAL_ADOPT, excluded_review_ids=["r3", "r4"],
        )
        self.assertEqual(cases.effective_exclusions("act-1"), {"r3", "r4"})
        cases.reopen(case.case_id, actor="venue-admin", reason="学校对排除范围提出异议")
        self.assertEqual(case.status, CaseStatus.REOPENED)
        # 重开后原决定不再生效，但历史完整保留
        self.assertEqual(cases.effective_exclusions("act-1"), set())
        self.assertEqual(
            [e.event_type for e in case.events],
            [CaseEventType.OPENED, CaseEventType.DECIDED, CaseEventType.REOPENED],
        )
        # 再次复核后给出新决定
        cases.decide(
            case.case_id, actor="venue-admin",
            decision=DecisionType.PARTIAL_ADOPT, excluded_review_ids=["r4"],
            note="仅排除第二条重复评价",
        )
        self.assertEqual(cases.effective_exclusions("act-1"), {"r4"})
        self.assertEqual(store.reviews["r3"].score, 1.0)

    def test_only_decided_case_can_reopen(self) -> None:
        store = build_store()
        cases, _, _ = build_services(store)
        case = cases.open_case(
            activity_id="act-1", review_ids=["r3"], reason="重复提交", opened_by="system:detection"
        )
        with self.assertRaises(CaseError):
            cases.reopen(case.case_id, actor="venue-admin", reason="尚未决定")


class PublishingTest(unittest.TestCase):
    def test_grade_requires_published_set(self) -> None:
        store = build_store()
        _, publishing, _ = build_services(store)
        with self.assertRaises(PublishError):
            publishing.grade_report("act-1")

    def test_rating_versions_normalized(self) -> None:
        store = InMemoryStore()
        store.reviews["a"] = make_review("a", score=4.0)                # 80 分
        store.reviews["b"] = make_review("b", score=7.0, version="v2")  # 70 分
        _, publishing, _ = build_services(store)
        publishing.publish("act-1", actor="venue-admin")
        report = publishing.grade_report("act-1")
        self.assertEqual(report.average_score, 75.0)
        self.assertEqual(report.grade, "C")

    def test_new_reviews_do_not_change_frozen_grade(self) -> None:
        store = InMemoryStore()
        store.reviews["a"] = make_review("a", score=5.0)
        _, publishing, _ = build_services(store)
        first = publishing.publish("act-1", actor="venue-admin")
        self.assertEqual(publishing.grade_report("act-1").grade, "A")
        # 新评价到达后，已发布集冻结，等级不变
        store.reviews["b"] = make_review("b", score=1.0, seq=1)
        self.assertEqual(publishing.grade_report("act-1").grade, "A")
        second = publishing.publish("act-1", actor="venue-admin")
        self.assertEqual(second.version, 2)
        report = publishing.grade_report("act-1")
        self.assertEqual(report.average_score, 60.0)  # (100+20)/2
        self.assertEqual(report.grade, "D")
        self.assertEqual(publishing.grade_of_set(first.set_id).grade, "A")


class DocentAPITest(unittest.TestCase):
    def test_impact_explanation_hides_sensitive_data(self) -> None:
        store = build_store()
        cases, publishing, api = build_services(store)
        opened = cases.open_cases_for_signals(DetectionEngine(store).evaluate("act-1"))
        publishing.publish("act-1", actor="venue-admin")
        dup_case = next(c for c in opened if "dup" in c.rule_ids)
        impact = api.impact_explanation("docent-1", dup_case.case_id)
        self.assertEqual(impact.current_grade, "E")
        self.assertEqual(impact.hypothetical_grade, "C")
        self.assertEqual(impact.flagged_in_current_set, 2)
        self.assertIn("估算", impact.summary)
        self.assertIn("不会被修改或删除", impact.summary)
        # 敏感信息不出现在讲解员视图
        sensitive_field_names = {"account_id", "contact", "fingerprint", "reviewer"}
        for view in impact.flagged_reviews:
            self.assertTrue(sensitive_field_names.isdisjoint(f.name for f in fields(view)))
            payload = str(asdict(view))
            self.assertNotIn("acct-dup", payload)
            self.assertNotIn("13800001111", payload)
            self.assertNotIn("fp-dup", payload)
            self.assertTrue(view.reviewer_alias.startswith("评价者-"))
            self.assertEqual(view.reviewer_display, "张*")

    def test_alias_is_stable_per_account(self) -> None:
        store = build_store()
        cases, _, api = build_services(store)
        case = cases.open_case(
            activity_id="act-1", review_ids=["r3", "r4"], reason="重复提交", opened_by="system:detection"
        )
        views = api.case_reviews("docent-1", case.case_id)
        self.assertEqual(views[0].reviewer_alias, views[1].reviewer_alias)

    def test_impact_when_all_reviews_flagged(self) -> None:
        store = InMemoryStore()
        store.reviews["a"] = make_review("a", score=1.0)
        cases, publishing, api = build_services(store)
        case = cases.open_case(
            activity_id="act-1", review_ids=["a"], reason="唯一评价也可疑", opened_by="docent-1"
        )
        publishing.publish("act-1", actor="venue-admin")
        impact = api.impact_explanation("docent-1", case.case_id)
        self.assertIsNone(impact.hypothetical_grade)
        self.assertIn("没有可计算等级", impact.summary)

    def test_other_docent_cannot_read_or_appeal(self) -> None:
        store = build_store()
        cases, publishing, api = build_services(store)
        case = cases.open_case(
            activity_id="act-1", review_ids=["r3"], reason="重复提交", opened_by="system:detection"
        )
        publishing.publish("act-1", actor="venue-admin")
        with self.assertRaises(PermissionError):
            api.impact_explanation("docent-2", case.case_id)
        with self.assertRaises(PermissionError):
            api.case_reviews("docent-2", case.case_id)
        with self.assertRaises(PermissionError):
            api.activity_grade("docent-2", "act-1")
        with self.assertRaises(PermissionError):
            api.file_appeal("docent-2", "act-1", ["r1"], "越权申诉")


if __name__ == "__main__":
    unittest.main()
