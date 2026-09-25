"""异常层次。

每个异常都携带 ``error_class``，因为**错误分类决定重试策略** ——
不可重试的错误直接进死信，不该浪费三次 LLM 调用。

Worker 的顶层 handler 靠这个属性把异常翻译成 ``WorkerResult.error_class``。
"""

from __future__ import annotations

from sfly_shared.contracts import ErrorClass


class SflyError(Exception):
    """所有项目异常的基类。"""

    error_class: ErrorClass = ErrorClass.TRANSIENT
    retryable: bool = True


# -- 可重试 ---------------------------------------------------------------- #


class TransientError(SflyError):
    """网络抖动、连接池耗尽。重试就好。"""

    error_class = ErrorClass.TRANSIENT


class LlmTimeoutError(SflyError):
    error_class = ErrorClass.LLM_TIMEOUT


class LlmHttpError(SflyError):
    """LLM 返回 5xx 或 429。"""

    error_class = ErrorClass.LLM_HTTP_ERROR


class DatabaseUnavailableError(SflyError):
    """Postgres 连不上。

    Worker 遇到它时**不要 ack** —— 消息留在 PEL 里，等 Postgres 恢复后
    被 XAUTOCLAIM 回收重跑。这样结果既不在 PEL 丢失也不在数据库丢失。
    """

    error_class = ErrorClass.DB_UNAVAILABLE


class StreamEntryTrimmedError(SflyError):
    """``XAUTOCLAIM`` 取回的消息 payload 为空 —— 条目已被 XTRIM 裁掉。

    这不是错误，是预期情况。消费者应该把它当「已处理」直接 ack 掉，
    绝不能让 ``None`` 流进解析器。单独定义一个类型是为了让调用点显式处理，
    而不是靠 ``except Exception`` 吞掉。
    """

    error_class = ErrorClass.TRANSIENT
    retryable = False


# -- 不可重试 -------------------------------------------------------------- #


class SchemaUnrecoverableError(SflyError):
    """修复阶梯走到 L4 还是解析不出合法 JSON。"""

    error_class = ErrorClass.SCHEMA_UNRECOVERABLE
    retryable = False


class DiffTooLargeError(SflyError):
    error_class = ErrorClass.DIFF_TOO_LARGE
    retryable = False


class RepoNotFoundError(SflyError):
    error_class = ErrorClass.REPO_NOT_FOUND
    retryable = False


class AuthRevokedError(SflyError):
    """GitHub token 被撤销或权限不足。重试一万次也一样。"""

    error_class = ErrorClass.AUTH_REVOKED
    retryable = False


# -- 业务控制流 ------------------------------------------------------------ #


class DuplicateRunError(SflyError):
    """幂等键已存在。**不是故障**，是正常的重放路径。

    api 层捕获它并返回 ``{"status": "duplicate"}`` 而不是 500 ——
    GitHub 在超时后会重投同一个 webhook，那是预期行为。
    """

    retryable = False


class BudgetExceededError(SflyError):
    """单 run 或单日的 token/费用预算耗尽。

    触发后 run 以 ``status=partial`` 收尾而不是失败 —— 访客仍应看到
    已经产出的部分结果。线上限流也用这个异常。
    """

    error_class = ErrorClass.TRANSIENT
    retryable = False


class PublishError(SflyError):
    """GitHub 评论发布失败。

    报告此时**已经落库**，不会丢失；``review_runs.status`` 变成
    ``publish_failed``，提供重新发布入口。
    """

    error_class = ErrorClass.TRANSIENT


def classify(exc: BaseException) -> ErrorClass:
    """把任意异常翻译成 ``ErrorClass``。供 Worker 的顶层 handler 使用。"""
    if isinstance(exc, SflyError):
        return exc.error_class
    return ErrorClass.TRANSIENT


def is_retryable(exc: BaseException) -> bool:
    return bool(getattr(exc, "retryable", True))
