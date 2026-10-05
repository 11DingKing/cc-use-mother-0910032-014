"""活动评价异常申诉的领域模型。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class ReviewChannel(str, Enum):
    """评价来源渠道。"""

    SCHOOL_FORM = "学校表单"
    ONSITE_QR = "现场扫码"
    PHONE_CALLBACK = "电话回访"


class IdentityLevel(str, Enum):
    """身份验证级别，决定评价在等级计算中的可信度权重。"""

    ANONYMOUS = "匿名"
    BASIC = "基础验证"
    VERIFIED = "实名验证"


IDENTITY_WEIGHTS = {
    IdentityLevel.ANONYMOUS: 0.5,
    IdentityLevel.BASIC: 1.0,
    IdentityLevel.VERIFIED: 1.5,
}


class AnomalyKind(str, Enum):
    """异常规则类型。"""

    DUPLICATE_SUBMISSION = "重复提交"
    ABNORMAL_ACCOUNT = "异常账号"


@dataclass(frozen=True)
class AnomalyRule:
    """异常规则：threshold 为同一活动内允许的最大次数，超出即触发立案。"""

    rule_id: str
    kind: AnomalyKind
    threshold: int
    description: str


@dataclass(frozen=True)
class ReviewerProfile:
    """评价者敏感信息，仅服务端保存，接口输出时必须裁剪。"""

    name: str
    contact: str
    account_id: str
    device_fingerprint: str


@dataclass(frozen=True)
class RatingVersion:
    """评分版本：每次评分变更追加一条，历史版本永不修改。"""

    version_no: int
    score: float
    comment: str
    reason: str
    recorded_at: datetime


@dataclass
class Review:
    """评价：保存来源、身份验证级别与完整评分版本链。"""

    review_id: str
    activity_id: str
    reviewer: ReviewerProfile
    channel: ReviewChannel
    identity_level: IdentityLevel
    created_at: datetime
    versions: list[RatingVersion] = field(default_factory=list)

    @property
    def original_score(self) -> float:
        return self.versions[0].score

    @property
    def current_score(self) -> float:
        return self.versions[-1].score


class CaseEventKind(str, Enum):
    """复核案件事件类型。"""

    OPENED = "立案"
    DECIDED = "决定"
    APPEAL_WITHDRAWN = "申诉撤回"
    DECISION_REOPENED = "决定重开"


class DecisionKind(str, Enum):
    """复核决定类型。"""

    MERGE_DUPLICATE = "合并重复"
    PARTIAL_ADOPTION = "部分采纳"


@dataclass(frozen=True)
class CaseEvent:
    """案件事件：追加式记录，立案后的每一步都留痕且不可改写。"""

    seq: int
    kind: CaseEventKind
    operator: str
    detail: dict
    at: datetime


class CaseStatus(str, Enum):
    """复核案件状态，由事件流推导。"""

    PENDING = "待复核"
    DECIDED = "已决定"
    WITHDRAWN = "已撤回"


@dataclass
class ReviewCase:
    """异常复核案件：针对可疑评价建立，代替自动改分。"""

    case_id: str
    review_id: str
    activity_id: str
    rule_id: str
    rule_kind: AnomalyKind
    opened_at: datetime
    events: list[CaseEvent] = field(default_factory=list)

    @property
    def status(self) -> CaseStatus:
        last = self.events[-1].kind
        if last is CaseEventKind.DECIDED:
            return CaseStatus.DECIDED
        if last is CaseEventKind.APPEAL_WITHDRAWN:
            return CaseStatus.WITHDRAWN
        return CaseStatus.PENDING  # 立案或决定重开

    @property
    def active_decision(self) -> dict | None:
        """当前生效的决定；重开或撤回后旧决定失效，但仍保留在事件流中。"""
        if self.status is not CaseStatus.DECIDED:
            return None
        return self.events[-1].detail


@dataclass(frozen=True)
class PublishedEntry:
    """已发布评价集条目：发布时冻结的取值快照。"""

    review_id: str
    score: float
    weight: float
    identity_level: IdentityLevel
    source_note: str


@dataclass(frozen=True)
class PublishedReviewSet:
    """已发布评价集：冻结不可变，等级计算的唯一依据。"""

    set_id: str
    activity_id: str
    entries: tuple[PublishedEntry, ...]
    published_by: str
    published_at: datetime
    digest: str


@dataclass(frozen=True)
class GradeReport:
    """等级计算结果，绑定某一已发布评价集。"""

    set_id: str
    activity_id: str
    review_count: int
    weighted_average: float
    grade: str


def grade_for(average: float) -> str:
    """按加权平均分判定等级。"""
    if average >= 4.5:
        return "优秀"
    if average >= 3.5:
        return "良好"
    if average >= 2.5:
        return "合格"
    return "待改进"
