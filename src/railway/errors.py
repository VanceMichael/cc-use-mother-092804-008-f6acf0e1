"""领域错误类型。"""

from __future__ import annotations


class DomainError(Exception):
    """所有领域规则违反的基类。"""


class ValidationError(DomainError):
    """受理资料不完整或引用不一致。"""


class PolicyViolation(DomainError):
    """服务资格、物种尺寸、件数路线或有效期等硬性约束不满足。"""


class ConflictError(DomainError):
    """当前状态不允许该操作（任务已交接、链路未到、时间窗已过等）。"""
