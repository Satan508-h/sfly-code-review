"""sfly_shared —— 领域契约、配置、ID、日志、异常。

导入约定：**具体模块直接导入，不要从包根导入**（避免循环导入）。

    from sfly_shared.contracts import Finding, TaskMessage, WorkerResult
    from sfly_shared.config import get_settings
    from sfly_shared.logging import get_logger, setup_logging

这里只做少量便利再导出，覆盖最高频的几个名字。
"""

from sfly_shared.config import Settings, get_settings
from sfly_shared.contracts import (
    SEVERITY_PRIOR,
    SEVERITY_RANK,
    AggregatedFinding,
    BootstrapMessage,
    ConflictRecord,
    ErrorClass,
    FilePatch,
    Finding,
    ResultStatus,
    ReviewReport,
    Rule,
    RunEvent,
    RunRow,
    RunStatus,
    RunTotals,
    Severity,
    TaskMessage,
    WorkerResult,
    WorkerType,
    idempotency_key_for,
    normalize_path,
    stable_hash,
)
from sfly_shared.errors import SflyError, classify, is_retryable
from sfly_shared.ids import new_id, new_task_id
from sfly_shared.logging import bind_task, get_logger, setup_logging

__all__ = [
    "SEVERITY_PRIOR",
    "SEVERITY_RANK",
    "AggregatedFinding",
    "BootstrapMessage",
    "ConflictRecord",
    "ErrorClass",
    "FilePatch",
    "Finding",
    "ResultStatus",
    "ReviewReport",
    "Rule",
    "RunEvent",
    "RunRow",
    "RunStatus",
    "RunTotals",
    "Settings",
    "Severity",
    "SflyError",
    "TaskMessage",
    "WorkerResult",
    "WorkerType",
    "bind_task",
    "classify",
    "get_logger",
    "get_settings",
    "idempotency_key_for",
    "is_retryable",
    "new_id",
    "new_task_id",
    "normalize_path",
    "setup_logging",
    "stable_hash",
]
