"""活动评价异常申诉后端。

- 评价保存来源、身份验证级别、评分版本，异常规则命中只建复核案件；
- 合并重复、部分采纳、申诉撤回、决定重开均保留原值；
- 等级计算绑定发布后冻结的评价集；
- 讲解员接口展示影响解释并裁剪评价者敏感信息。
"""
from .api import CaseSummary, DocentAPI, DocentReviewView, ImpactExplanation
from .cases import CaseError, CaseService
from .detection import DetectionEngine
from .models import (
    AnomalyRule,
    AnomalySignal,
    CaseEvent,
    CaseEventType,
    CaseStatus,
    DecisionType,
    IdentityVerification,
    PublishedEntry,
    PublishedReviewSet,
    Review,
    ReviewCase,
    ReviewerIdentity,
    ReviewSource,
    RuleKind,
)
from .publishing import GradeReport, PublishingService, PublishError
from .store import InMemoryStore

__all__ = [
    "AnomalyRule",
    "AnomalySignal",
    "CaseError",
    "CaseEvent",
    "CaseEventType",
    "CaseService",
    "CaseStatus",
    "CaseSummary",
    "DecisionType",
    "DetectionEngine",
    "DocentAPI",
    "DocentReviewView",
    "GradeReport",
    "IdentityVerification",
    "ImpactExplanation",
    "InMemoryStore",
    "PublishedEntry",
    "PublishedReviewSet",
    "PublishingService",
    "PublishError",
    "Review",
    "ReviewCase",
    "ReviewSource",
    "ReviewerIdentity",
    "RuleKind",
]
