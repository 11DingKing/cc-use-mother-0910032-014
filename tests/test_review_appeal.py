"""活动评价异常申诉后端的回归测试。"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from review_appeal import (
    AnomalyKind,
    AnomalyRule,
    CaseEventKind,
    CaseStatus,
    DecisionKind,
    IdentityLevel,
    ReviewAppealService,
    ReviewChannel,
    ReviewerProfile,
)


def profile(account: str, device: str, name: str = "李小明", contact: str = "13911112222") -> ReviewerProfile:
    return ReviewerProfile(name=name, contact=contact, account_id=account, device_fingerprint=device)


def make_duplicate_pair(svc: ReviewAppealService, activity_id: str = "ACT-1"):
    """登记两条同账号评价，第二条触发重复提交案件。"""
    first = svc.register_review(
        activity_id=activity_id,
        reviewer=profile("acc-1", "dev-1"),
        channel=ReviewChannel.SCHOOL_FORM,
        identity_level=IdentityLevel.VERIFIED,
        score=5.0,
        comment="讲解清晰",
    )
    dup = svc.register_review(
        activity_id=activity_id,
        reviewer=profile("acc-1", "dev-1"),
        channel=ReviewChannel.ONSITE_QR,
        identity_level=IdentityLevel.BASIC,
        score=1.0,
        comment="重复低分",
    )
    case = svc.list_cases(activity_id)[0]
    return first, dup, case


class IntakeTest(unittest.TestCase):
    def test_intake_saves_source_identity_versions_and_rules(self) -> None:
        svc = ReviewAppealService()
        svc.add_anomaly_rule(AnomalyRule("AR-LOW-1", AnomalyKind.ABNORMAL_ACCOUNT, 3, "自定义异常规则"))
        review = svc.register_review(
            activity_id="ACT-1",
            reviewer=profile("acc-1", "dev-1"),
            channel=ReviewChannel.SCHOOL_FORM,
            identity_level=IdentityLevel.VERIFIED,
            score=4.5,
            comment="讲解清晰",
        )
        self.assertEqual(review.channel, ReviewChannel.SCHOOL_FORM)
        self.assertEqual(review.identity_level, IdentityLevel.VERIFIED)
        self.assertEqual(len(review.versions), 1)
        self.assertEqual(review.versions[0].score, 4.5)
        self.assertGreaterEqual(len(svc.list_rules()), 3)  # 两条默认规则 + 自定义规则

        svc.append_rating_version(review.review_id, score=4.0, comment="补充说明", reason="学校联系人更正")
        self.assertEqual(review.original_score, 4.5)  # 首版保留
        self.assertEqual(review.current_score, 4.0)
        self.assertEqual(len(review.versions), 2)

    def test_score_must_be_in_range(self) -> None:
        svc = ReviewAppealService()
        with self.assertRaises(ValueError):
            svc.register_review(
                activity_id="ACT-1",
                reviewer=profile("acc-1", "dev-1"),
                channel="学校表单",
                identity_level="匿名",
                score=6.0,
                comment="越界评分",
            )


class AnomalyCaseTest(unittest.TestCase):
    def test_suspicious_review_opens_case_without_changing_score(self) -> None:
        svc = ReviewAppealService()
        _, dup, case = make_duplicate_pair(svc)
        self.assertEqual(case.review_id, dup.review_id)
        self.assertEqual(case.rule_kind, AnomalyKind.DUPLICATE_SUBMISSION)
        self.assertEqual(case.status, CaseStatus.PENDING)
        # 立案不改分：低分原样保留
        self.assertEqual(dup.current_score, 1.0)
        self.assertEqual(len(dup.versions), 1)

    def test_abnormal_account_rule_opens_case(self) -> None:
        svc = ReviewAppealService()
        for i in range(2):
            svc.register_review(
                activity_id="ACT-1",
                reviewer=profile(f"acc-{i}", "dev-x"),
                channel="现场扫码",
                identity_level="匿名",
                score=5.0,
                comment="正常评价",
            )
        third = svc.register_review(
            activity_id="ACT-1",
            reviewer=profile("acc-9", "dev-x"),
            channel="现场扫码",
            identity_level="匿名",
            score=1.0,
            comment="异常低分",
        )
        cases = svc.list_cases("ACT-1")
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0].rule_kind, AnomalyKind.ABNORMAL_ACCOUNT)
        self.assertEqual(cases[0].review_id, third.review_id)
        self.assertEqual(third.current_score, 1.0)

    def test_merge_duplicate_preserves_original_values(self) -> None:
        svc = ReviewAppealService()
        first, dup, case = make_duplicate_pair(svc)
        svc.decide_merge_duplicate(case.case_id, target_review_id=first.review_id, operator="活动统筹员")
        self.assertEqual(case.status, CaseStatus.DECIDED)
        self.assertEqual(case.active_decision["decision"], DecisionKind.MERGE_DUPLICATE)
        # 原值保留
        self.assertEqual(dup.original_score, 1.0)
        self.assertEqual(dup.current_score, 1.0)
        self.assertEqual(len(dup.versions), 1)
        # 发布集排除被合并评价
        published = svc.publish_review_set("ACT-1", operator="活动统筹员")
        entry_ids = [e.review_id for e in published.entries]
        self.assertIn(first.review_id, entry_ids)
        self.assertNotIn(dup.review_id, entry_ids)

    def test_partial_adoption_preserves_original_values(self) -> None:
        svc = ReviewAppealService()
        _, dup, case = make_duplicate_pair(svc)
        svc.decide_partial_adoption(case.case_id, adopted_score=3.0, operator="活动统筹员")
        self.assertEqual(case.status, CaseStatus.DECIDED)
        # 原评分不动，采纳值只影响发布取值
        self.assertEqual(dup.original_score, 1.0)
        self.assertEqual(dup.current_score, 1.0)
        published = svc.publish_review_set("ACT-1", operator="活动统筹员")
        entry = next(e for e in published.entries if e.review_id == dup.review_id)
        self.assertEqual(entry.score, 3.0)
        self.assertIn("部分采纳", entry.source_note)

    def test_appeal_withdrawal_preserves_values(self) -> None:
        svc = ReviewAppealService()
        _, dup, case = make_duplicate_pair(svc)
        svc.withdraw_appeal(case.case_id, operator="学校联系人")
        self.assertEqual(case.status, CaseStatus.WITHDRAWN)
        self.assertIsNone(case.active_decision)
        self.assertEqual([e.kind for e in case.events], [CaseEventKind.OPENED, CaseEventKind.APPEAL_WITHDRAWN])
        self.assertEqual(dup.current_score, 1.0)
        # 无生效决定，发布集按原评分收录
        published = svc.publish_review_set("ACT-1", operator="活动统筹员")
        entry = next(e for e in published.entries if e.review_id == dup.review_id)
        self.assertEqual(entry.score, 1.0)

    def test_decision_reopen_preserves_history(self) -> None:
        svc = ReviewAppealService()
        first, dup, case = make_duplicate_pair(svc)
        svc.decide_merge_duplicate(case.case_id, target_review_id=first.review_id, operator="活动统筹员")
        svc.reopen_decision(case.case_id, operator="活动统筹员", reason="学校补充了出勤证明")
        self.assertEqual(case.status, CaseStatus.PENDING)
        self.assertIsNone(case.active_decision)
        # 旧决定仍保留在事件流中
        kinds = [e.kind for e in case.events]
        self.assertEqual(kinds, [CaseEventKind.OPENED, CaseEventKind.DECIDED, CaseEventKind.DECISION_REOPENED])
        self.assertEqual(case.events[1].detail["decision"], DecisionKind.MERGE_DUPLICATE)
        # 重开后旧决定不再生效，发布集恢复按原评分收录
        published = svc.publish_review_set("ACT-1", operator="活动统筹员")
        entry = next(e for e in published.entries if e.review_id == dup.review_id)
        self.assertEqual(entry.score, 1.0)
        # 重开后可重新决定，全部历史保留
        svc.decide_partial_adoption(case.case_id, adopted_score=2.5, operator="活动统筹员")
        self.assertEqual(case.active_decision["decision"], DecisionKind.PARTIAL_ADOPTION)
        self.assertEqual(len(case.events), 4)
        self.assertEqual(dup.current_score, 1.0)

    def test_case_transitions_are_guarded(self) -> None:
        svc = ReviewAppealService()
        first, dup, case = make_duplicate_pair(svc)
        with self.assertRaises(ValueError):
            svc.reopen_decision(case.case_id, operator="活动统筹员", reason="尚未决定")
        with self.assertRaises(ValueError):
            svc.decide_merge_duplicate(case.case_id, target_review_id=dup.review_id, operator="活动统筹员")
        svc.decide_merge_duplicate(case.case_id, target_review_id=first.review_id, operator="活动统筹员")
        with self.assertRaises(ValueError):
            svc.decide_partial_adoption(case.case_id, adopted_score=3.0, operator="活动统筹员")
        with self.assertRaises(ValueError):
            svc.withdraw_appeal(case.case_id, operator="学校联系人")


class GradeBindingTest(unittest.TestCase):
    def test_grade_bound_to_published_set(self) -> None:
        svc = ReviewAppealService()
        svc.register_review(
            activity_id="ACT-1", reviewer=profile("acc-1", "dev-1"),
            channel="学校表单", identity_level="实名验证", score=5.0, comment="很好",
        )
        svc.register_review(
            activity_id="ACT-1", reviewer=profile("acc-2", "dev-2"),
            channel="现场扫码", identity_level="基础验证", score=4.0, comment="不错",
        )
        dup = svc.register_review(
            activity_id="ACT-1", reviewer=profile("acc-1", "dev-1"),
            channel="现场扫码", identity_level="匿名", score=1.0, comment="重复低分",
        )
        first_set = svc.publish_review_set("ACT-1", operator="活动统筹员")
        first_grade = svc.compute_grade(first_set.set_id)
        # 加权平均：(5*1.5 + 4*1.0 + 1*0.5) / 3 = 4.0
        self.assertEqual(first_grade.review_count, 3)
        self.assertAlmostEqual(first_grade.weighted_average, 4.0)
        self.assertEqual(first_grade.grade, "良好")

        # 发布后合并重复低分：已发布评价集冻结，等级不变
        case = svc.list_cases("ACT-1")[0]
        svc.decide_merge_duplicate(case.case_id, target_review_id="RV-0001", operator="活动统筹员")
        frozen_grade = svc.compute_grade(first_set.set_id)
        self.assertAlmostEqual(frozen_grade.weighted_average, 4.0)
        self.assertEqual(frozen_grade.grade, "良好")

        # 下次发布后新等级生效
        second_set = svc.publish_review_set("ACT-1", operator="活动统筹员")
        self.assertNotEqual(first_set.digest, second_set.digest)
        self.assertNotIn(dup.review_id, [e.review_id for e in second_set.entries])
        second_grade = svc.compute_grade(second_set.set_id)
        # 加权平均：(5*1.5 + 4*1.0) / 2.5 = 4.6
        self.assertAlmostEqual(second_grade.weighted_average, 4.6)
        self.assertEqual(second_grade.grade, "优秀")

    def test_grade_requires_published_set(self) -> None:
        svc = ReviewAppealService()
        with self.assertRaises(ValueError):
            svc.compute_grade("PS-9999")


class DocentViewTest(unittest.TestCase):
    def test_view_explains_impact_and_hides_reviewer(self) -> None:
        svc = ReviewAppealService()
        svc.register_review(
            activity_id="ACT-1", reviewer=profile("acc-1", "dev-1", name="李小明", contact="13911112222"),
            channel="学校表单", identity_level="实名验证", score=5.0, comment="很好",
        )
        svc.register_review(
            activity_id="ACT-1", reviewer=profile("acc-2", "dev-2", name="王红", contact="13933334444"),
            channel="现场扫码", identity_level="基础验证", score=4.0, comment="不错",
        )
        dup = svc.register_review(
            activity_id="ACT-1", reviewer=profile("acc-1", "dev-1"),
            channel="现场扫码", identity_level="匿名", score=1.0, comment="重复低分",
        )
        svc.publish_review_set("ACT-1", operator="活动统筹员")
        case = svc.list_cases("ACT-1")[0]
        svc.decide_merge_duplicate(case.case_id, target_review_id="RV-0001", operator="活动统筹员")
        # 再登记一条触发异常账号规则的评价，形成待复核案件
        svc.register_review(
            activity_id="ACT-1", reviewer=profile("acc-7", "dev-1"),
            channel="现场扫码", identity_level="匿名", score=1.0, comment="异常账号低分",
        )

        view = svc.docent_activity_view("ACT-1")

        # 影响解释：合并、待复核与等级依据均有说明
        self.assertEqual(view["pending_case_count"], 1)
        self.assertEqual(view["published_grade"]["grade"], "良好")
        self.assertAlmostEqual(view["published_grade"]["weighted_average"], 4.0)
        explanation = "\n".join(view["impact_explanation"])
        self.assertIn("保留存档", explanation)
        self.assertIn("不会自动改分", explanation)
        self.assertIn("已发布评价集", explanation)

        # 评价条目状态可见
        statuses = {r["review_id"]: r["status"] for r in view["reviews"]}
        self.assertEqual(statuses[dup.review_id], "已合并")
        self.assertEqual(statuses["RV-0004"], "复核中")
        self.assertEqual(statuses["RV-0001"], "有效")

        # 匿名信息裁剪：接口输出不含任何评价者敏感信息
        blob = json.dumps(view, ensure_ascii=False)
        for secret in ("李小明", "王红", "13911112222", "13933334444", "acc-1", "acc-2", "acc-7", "dev-1", "dev-2"):
            self.assertNotIn(secret, blob)
        for entry in view["reviews"]:
            for field in ("reviewer", "name", "contact", "account_id", "device_fingerprint"):
                self.assertNotIn(field, entry)


if __name__ == "__main__":
    unittest.main()
