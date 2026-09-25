"""测试用的领域对象工厂。

这些工厂原本住在 ``tests/contracts/queue_contract.py`` 里 —— 队列契约是最早需要
它们的地方。M4 起有三层都要用同一批对象（队列契约、仓储往返、M5 的图），
所以它们被提到这里：**同一个 ``BootstrapMessage`` 在三种测试里必须是同一个形状**，
否则「图能处理它，但仓储存不下」这种不一致只能靠人记得去比对。

放在 ``tests/`` 下而不是某层目录里，理由和 ``tests/contracts/`` 一样：
它不属于任何一层。（``tests/conftest.py`` 里那行 ``sys.path`` 让各层都能
``from factories import ...``。）

写法约定：``**over`` 覆盖默认值，默认值本身要是一个**合法的**对象 ——
需要「坏数据」的测试自己传，而不是在这里堆积假的坏值。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sfly_agent.diff import parse_unified_diff
from sfly_shared.contracts import (
    AggregatedFinding,
    BootstrapMessage,
    ConflictRecord,
    FilePatch,
    Finding,
    ResultStatus,
    ReviewReport,
    Rule,
    RunTotals,
    Severity,
    TaskMessage,
    WorkerResult,
    WorkerType,
    idempotency_key_for,
)

#: 默认的 run id。用 ULID 的字面形态（26 位 Crockford base32），
#: 因为 ``list_runs`` 的排序依赖它的字典序 == 时间序（见 sfly_shared/ids.py）。
DEFAULT_TASK_ID = "01JTESTRUN0000000000000000"

#: 仓库根下的 ``fixtures/``。**从 fixtures 里读而不是在这里手写一份 diff**：
#: 手写的那份一定会和 CLI 用的那份分叉，而分叉的后果是
#: 「测试里 Mock 报 12 条、命令行上只报 2 条」这种要查半天的不一致。
FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"

#: 演示用的 diff。它被三个地方共用：``sfly_workers --diff``、
#: ``sfly_orchestrator --diff``、以及这里的图测试。
DEMO_DIFF = "security_demo.diff"


def demo_patches(name: str = DEMO_DIFF, *, max_patch_chars: int = 8_000) -> list[FilePatch]:
    """``fixtures/`` 里那份真实 diff 解析出来的补丁。

    **不要用 ``bootstrap()`` 的默认补丁跑图测试或消费循环测试**：
    那是三行的玩具补丁（``@@ -1 +1 @@``），Mock 是确定性的规则扫描器，
    在它上面什么都不会报 —— 于是测试会在「零发现」那条路径上通过，
    而它要验证的东西一条也没验证到。M4 就是被这个坑掉的。
    """
    text = (FIXTURES_DIR / name).read_text(encoding="utf-8")
    return parse_unified_diff(text, max_patch_chars=max_patch_chars).patches


def bootstrap(**over: Any) -> BootstrapMessage:
    """一个合法的 BootstrapMessage。``idempotency_key`` 由三元组算出来，
    不留给人填 —— 填错的话契约层会直接拒绝，那不是这里想测的东西。"""
    base: dict[str, Any] = {
        "task_id": DEFAULT_TASK_ID,
        "repo_id": "123456",
        "repo_node_id": "R_kgDOAbcdef",
        "pr_number": 7,
        "head_sha": "a" * 40,
        "base_sha": "b" * 40,
        "pr_title": "给查询接口加上分页",
        "pr_author": "contributor",
        "file_patches": [
            FilePatch(path="app/db.py", language="python", patch="@@ -1 +1 @@\n-x\n+y\n", changed_lines=[1]),
        ],
    }
    base.update(over)
    base["idempotency_key"] = idempotency_key_for(base["repo_id"], base["pr_number"], base["head_sha"])
    return BootstrapMessage(**base)


def task(
    task_id: str,
    *,
    worker_type: WorkerType = WorkerType.SECURITY,
    file_patches: list[FilePatch] | None = None,
) -> TaskMessage:
    """``task_id`` 是**同一个 run 的所有 Worker 共用的**那个 id。
    多条消息各带不同 task_id，就等于这条流上同时跑着多个 run —— 这是常态。

    ``file_patches`` 可以覆盖：消费循环的测试要用**真的会被 Mock 报出问题**的补丁
    （Mock 是确定性的规则扫描器，随便几行代码它什么都不会报，于是测试会在
    「零发现」那条路径上通过 —— 什么也没验证到）。
    """
    boot = bootstrap()
    return TaskMessage(
        task_id=task_id,
        worker_type=worker_type,
        idempotency_key=boot.idempotency_key,
        repo_id=boot.repo_id,
        repo_node_id=boot.repo_node_id,
        pr_number=boot.pr_number,
        head_sha=boot.head_sha,
        base_sha=boot.base_sha,
        file_patches=list(file_patches) if file_patches is not None else list(boot.file_patches),
        language="python",
    )


def rule(**over: Any) -> Rule:
    base: dict[str, Any] = {
        "id": "sec-sqli-001",
        "title": "SQL 语句不得用字符串拼接构造",
        "worker_type": WorkerType.SECURITY,
        "category": "sqli",
        "severity_hint": Severity.CRITICAL,
        "languages": ["python"],
        "cwe": "CWE-89",
        "owasp": "A03:2021",
        "body": "把用户输入直接拼进 SQL 会改变查询语义。",
        "references": ["https://cwe.mitre.org/data/definitions/89.html"],
    }
    base.update(over)
    return Rule(**base)


def finding(**over: Any) -> Finding:
    base: dict[str, Any] = {
        "file": "app/db.py",
        "line": 12,
        "severity": Severity.HIGH,
        "category": "sqli",
        "message": "用 f-string 拼接 SQL，用户输入没有参数化",
        "confidence": 0.8,
    }
    base.update(over)
    return Finding.model_validate(base)


def result(
    task_id: str,
    *,
    worker_type: WorkerType = WorkerType.SECURITY,
    findings: list[Finding] | None = None,
    **over: Any,
) -> WorkerResult:
    """一个成功上报。

    ``findings`` 的默认值是「一条」，不是空 —— 空的合法但太特殊，
    拿来当默认会让「忘了传 findings」看起来像「这个 Worker 什么都没发现」。
    """
    base: dict[str, Any] = {
        "task_id": task_id,
        "worker_type": worker_type,
        "status": ResultStatus.OK,
        "findings": findings if findings is not None else [finding()],
        "tokens_in": 1234,
        "tokens_out": 567,
        "cached_tokens": 800,
        "latency_ms": 4321,
        "model": "mock-1",
    }
    base.update(over)
    return WorkerResult(**base)


def aggregated(**over: Any) -> AggregatedFinding:
    """聚合后的发现 —— 比 ``finding()`` 多出的字段全部来自确定性计算。"""
    base: dict[str, Any] = {
        **finding().model_dump(),
        "adjusted_confidence": 0.72,
        "sources": [WorkerType.SECURITY, WorkerType.PERFORMANCE],
        "corroboration_count": 2,
        "cluster_id": 1,
        "stage": "clustered",
    }
    base.update(over)
    return AggregatedFinding.model_validate(base)


def conflict(**over: Any) -> ConflictRecord:
    base: dict[str, Any] = {
        "file": "app/db.py",
        "line": 12,
        "winner_worker": WorkerType.SECURITY,
        "loser_worker": WorkerType.STYLE,
        "winner_severity": Severity.CRITICAL,
        "loser_severity": Severity.LOW,
        "resolution_rule": "category_authority",
        "rationale": "SQL 注入属于安全 Worker 的职责域",
    }
    base.update(over)
    return ConflictRecord(**base)


def report(task_id: str = DEFAULT_TASK_ID, **over: Any) -> ReviewReport:
    """一份最终报告。**形状要尽量覆盖各分支**：既有一条正常发现、也有一条被
    置信度闸砍掉的（``suppressed``）、还有一条冲突记录，以及非零的 totals ——
    仓储往返测试拿它当输入，字段越全，jsonb 编解码的问题越藏不住。"""
    base: dict[str, Any] = {
        "task_id": task_id,
        "repo_id": "123456",
        "repo_node_id": "R_kgDOAbcdef",
        "pr_number": 7,
        "head_sha": "a" * 40,
        "base_sha": "b" * 40,
        "findings": [aggregated()],
        "suppressed": [aggregated(category="naming", adjusted_confidence=0.2, stage="suppressed")],
        "conflicts": [conflict()],
        "block_merge": True,
        "decision_reason": "critical_in_policy",
        "degraded": True,
        "missing_workers": [WorkerType.STYLE],
        "files_total": 4,
        "files_reviewed": 3,
        "diff_truncated": True,
        "totals": RunTotals(
            tokens_in=1600,
            tokens_out=890,
            cached_tokens=1024,
            cost_usd=0.004321,
            llm_calls=3,
            duration_ms=8123,
            per_worker_ms={"security": 3100, "performance": 2500},
        ),
        "comment_body": "## sfly 审查报告\n\n发现 1 个高危问题。",
    }
    base.update(over)
    return ReviewReport(**base)
