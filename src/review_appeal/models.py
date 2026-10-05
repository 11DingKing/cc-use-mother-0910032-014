"""活动评价异常申诉的核心领域模型。

对应 domain/contract.json 的四条不变量：
- 评价来源可信度：评价保存来源渠道与身份验证级别；
- 异常复核案件：可疑评价只建复核案件，不自动改分；
- 发布评价集冻结：等级计算绑定发布后冻结的评价集；
- 匿名信息裁剪：面向讲解员的视图隐藏评价者敏感信息。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping


class ReviewSource(str, Enum):
    """评价来源渠道。"""

    SCHOOL_PORTAL = "school_portal"  # 学校门户
    ONSITE_KIOSK = "onsite_kiosk"    # 现场终端
    EMAIL_LINK = "email_link"        # 邮件邀请链接
    PUBLIC_WEB = "public_web"        # 公开网页


class IdentityVerification(str, Enum):
    """评价者身份验证级别，级别越低来源可信度越弱。"""

    VERIFIED = "verified"    # 实名核验
    BASIC = "basic"          # 基础账号
    ANONYMOUS = "anonymous"  # 匿名提交


# 评分版本 -> 满分。不同版本量表在等级计算前统一归一化到百分制。
RATING_SCALES: dict[str, float] = {"v1": 5.0, "v2": 10.0}

# 百分制平均分 -> 等级下限分档，自上而下匹配。
GRADE_BANDS: tuple[tuple[float, str], ...] = (
    (90.0, "A"),
    (80.0, "B"),
    (70.0, "C"),
    (60.0, "D"),
    (0.0, "E"),
)


class RuleKind(str, Enum):
    """异常规则类型。"""

    DUPLICATE_SUBMISSION = "duplicate_submission"  # 重复提交
    ABNORMAL_ACCOUNT = "abnormal_account"          # 异常账号


class CaseStatus(str, Enum):
    """复核案件状态。"""

    OPEN = "open"            # 待复核
    MERGED = "merged"        # 已并入其他案件
    DECIDED = "decided"      # 已决定
    WITHDRAWN = "withdrawn"  # 申诉已撤回
    REOPENED = "reopened"    # 决定重开，等待再次复核


class CaseEventType(str, Enum):
    """案件事件类型，事件只追加不删除。"""

    OPENED = "opened"
    MERGED_INTO = "merged_into"  # 本案件并入目标案件
    ABSORBED = "absorbed"        # 本案件吸收来源案件
    DECIDED = "decided"
    WITHDRAWN = "withdrawn"
    REOPENED = "reopened"


class DecisionType(str, Enum):
    """复核决定类型。"""

    PARTIAL_ADOPT = "partial_adopt"  # 部分采纳：仅排除部分可疑评价
    REJECT = "reject"                # 驳回：维持现有发布集


@dataclass(frozen=True)
class ReviewerIdentity:
    """评价者敏感信息，仅供内部检测使用，不对讲解员暴露。"""

    account_id: str
    display_name: str
    contact: str


@dataclass(frozen=True)
class Review:
    """一条原始评价。创建后不可修改，复核处置只影响发布集，不改原值。"""

    review_id: str
    activity_id: str
    docent_id: str
    source: ReviewSource
    identity_verification: IdentityVerification
    rating_version: str
    score: float
    comment: str
    reviewer: ReviewerIdentity
    submitted_at: datetime
    fingerprint: str  # 提交环境指纹（设备/网络哈希），用于重复与异常账号检测

    def __post_init__(self) -> None:
        if self.rating_version not in RATING_SCALES:
            raise ValueError(f"未知评分版本：{self.rating_version}")
        scale = RATING_SCALES[self.rating_version]
        if not 0.0 <= self.score <= scale:
            raise ValueError(f"评分 {self.score} 超出 {self.rating_version} 量表范围 0-{scale:g}")


@dataclass(frozen=True)
class AnomalyRule:
    """异常规则：命中后只产生信号并建复核案件，不自动改分。"""

    rule_id: str
    kind: RuleKind
    name: str
    description: str
    params: Mapping[str, Any] = field(default_factory=dict)
    enabled: bool = True


@dataclass(frozen=True)
class AnomalySignal:
    """一次规则命中，指向一组可疑评价。"""

    rule_id: str
    kind: RuleKind
    review_ids: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class CaseEvent:
    """案件事件。追加式记录，保证合并、采纳、撤回、重开全程可溯。"""

    event_type: CaseEventType
    actor: str
    at: datetime
    detail: str = ""
    decision: DecisionType | None = None
    excluded_review_ids: tuple[str, ...] = ()
    related_case_id: str | None = None


@dataclass
class ReviewCase:
    """异常复核案件。状态可变、事件只增，关联评价的原始分永不修改。"""

    case_id: str
    activity_id: str
    docent_id: str
    review_ids: list[str]
    rule_ids: list[str]
    reason: str
    opened_by: str
    opened_at: datetime
    status: CaseStatus = CaseStatus.OPEN
    events: list[CaseEvent] = field(default_factory=list)

    @property
    def active(self) -> bool:
        """待复核或重开后待再次复核的案件视为进行中。"""
        return self.status in (CaseStatus.OPEN, CaseStatus.REOPENED)


@dataclass(frozen=True)
class PublishedEntry:
    """发布集中的单条快照，发布时归一化，之后冻结。"""

    review_id: str
    rating_version: str
    normalized_score: float  # 百分制


@dataclass(frozen=True)
class PublishedReviewSet:
    """已发布评价集：发布后冻结，等级计算只绑定它。"""

    set_id: str
    activity_id: str
    version: int
    entries: tuple[PublishedEntry, ...]
    published_at: datetime
