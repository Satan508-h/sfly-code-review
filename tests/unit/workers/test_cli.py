"""``python -m sfly_workers --spec X --diff Y`` 的测试。

M1 的交付物就是这条命令，所以这里测的是它的**契约**而不只是「能不能跑」：

* **stdout 必须是纯 JSON** —— 有一行日志混进去，``| jq`` 就会在第一个字符上
  解析失败，而报错指向 jq 的语法错误，完全看不出真正的原因
* **退出码必须区分三件事** —— 有结果 / 审查失败 / 输入不是 diff。
  合并它们会让 CI 把「模型抽风」和「文件给错了」当成同一件事
* **「没发现问题」和「没跑成」必须读起来完全不同**

测试全部是同步函数：CLI 内部用 ``sfly_shared.aio.run`` 起事件循环，
在已有事件循环里调用它会直接报错 —— 这也正是 ``--diff`` 只能作为
进程入口使用的原因。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sfly_shared.config import Settings
from sfly_shared.contracts import ErrorClass, WorkerResult, WorkerType
from sfly_workers.__main__ import EXIT_BAD_INPUT, EXIT_FAILED, EXIT_OK, main
from sfly_workers.pool import should_dead_letter

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures"
NOT_A_DIFF = "这是一段普通文字，不是 diff。"


def _run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *argv: str, **over: object
) -> tuple[int, str, str]:
    """跑一次 CLI，返回 ``(退出码, stdout, stderr)``。

    ``get_settings`` 被替换掉是**刻意的**：开发机上有一份 .env，
    不隔离的话测试结果会随本机配置变化 —— 而「在我机器上是绿的」
    正是这类测试最没有价值的形态。
    """
    settings = Settings(llm_provider="mock", **over)  # type: ignore[arg-type]
    monkeypatch.setattr("sfly_workers.__main__.get_settings", lambda: settings)

    with pytest.raises(SystemExit) as exc:
        main(list(argv))
    captured = capsys.readouterr()
    code = exc.value.code
    return (code if isinstance(code, int) else 1), captured.out, captured.err


# --------------------------------------------------------------------------- #
# stdout 是程序输出，stderr 是给人看的
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_stdout_is_valid_json_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """**这条是 --diff 模式存在的意义。**

    输出必须能直接喂给 jq。任何一个日志处理器把一行文本打到 stdout，
    这条测试就会失败 —— 而那个故障在真实使用里表现为
    「jq 报语法错误，但我明明什么都没改」。
    """
    code, out, _err = _run(
        monkeypatch, capsys, "--spec", "security", "--diff", str(FIXTURES / "security_demo.diff")
    )
    assert code == EXIT_OK
    payload = json.loads(out)  # 整段 stdout 必须是一个合法 JSON 文档
    assert payload["worker_type"] == "security"
    assert payload["status"] == "ok"
    assert len(payload["findings"]) > 5


@pytest.mark.unit
def test_human_summary_goes_to_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _code, out, err = _run(
        monkeypatch, capsys, "--spec", "security", "--diff", str(FIXTURES / "security_demo.diff")
    )
    assert "安全审查结果" in err
    assert "安全审查结果" not in out


@pytest.mark.unit
def test_findings_are_sorted_by_severity_in_the_summary(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """摘要里最严重的排最前 —— 人扫一眼就要能决定要不要停下来。"""
    _code, _out, err = _run(
        monkeypatch, capsys, "--spec", "security", "--diff", str(FIXTURES / "security_demo.diff")
    )
    assert err.index("[严重]") < err.index("[中危]")


@pytest.mark.unit
def test_quiet_suppresses_the_json_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = _run(
        monkeypatch, capsys, "--spec", "security", "--diff", str(FIXTURES / "security_demo.diff"), "--quiet"
    )
    assert code == EXIT_OK
    assert out == ""
    assert "发现" in err


@pytest.mark.unit
def test_no_english_info_logs_leak_into_the_summary(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """**回归测试**：``--diff`` 模式下不该出现 INFO 级的日志行。

    踩过的坑：日志级别原本是用 ``logging.getLogger().setLevel(WARNING)`` 压的，
    而 structlog 的 PrintLogger **不经过标准库的 root logger** ——
    那一行看着像在静音，实际一点作用都没有，命令行上照刷英文 INFO。
    真正的开关是传给 ``setup_logging`` 的 level。

    这条测试盯的是「人读的那段摘要里只有中文、没有一堆事件名」，
    所以断言的是**行为**（没有 INFO 行），而不是实现（用什么方式压的）。
    """
    _code, _out, err = _run(
        monkeypatch, capsys, "--spec", "security", "--diff", str(FIXTURES / "security_demo.diff")
    )
    for noise in ("worker.reviewed", "rag.corpus_loaded", "[info", "status=ok"):
        assert noise not in err, f"摘要里混进了日志：{noise}"


# --------------------------------------------------------------------------- #
# 退出码
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_clean_diff_exits_zero_with_an_empty_findings_list(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """「审了，没问题」是一次成功。"""
    code, out, err = _run(monkeypatch, capsys, "--spec", "security", "--diff", str(FIXTURES / "clean.diff"))
    assert code == EXIT_OK
    assert json.loads(out)["findings"] == []
    assert "未发现问题" in err


@pytest.mark.unit
def test_input_without_any_diff_is_an_input_error_not_a_clean_review(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """**最要命的一种误导就在这里。**

    把「这不是 diff」当成「没问题」，用户会以为审查通过了 ——
    而实际上它面向的是空气。所以这里必须是输入错误，而且要说清楚为什么。
    """
    bad = tmp_path / "notes.txt"
    bad.write_text(NOT_A_DIFF, encoding="utf-8")

    code, out, err = _run(monkeypatch, capsys, "--spec", "security", "--diff", str(bad))
    assert code == EXIT_BAD_INPUT
    assert out == ""
    assert "没有解析出任何可审查的文件" in err
    assert "unified diff" in err


@pytest.mark.unit
def test_missing_file_is_an_input_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _out, err = _run(monkeypatch, capsys, "--spec", "security", "--diff", "根本不存在.diff")
    assert code == EXIT_BAD_INPUT
    assert "找不到 diff 文件" in err


@pytest.mark.unit
def test_failure_injection_still_produces_results(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """**开了 100% 故障注入，审查照样出结果。**

    这条是修复阶梯的端到端证据：每一次调用都返回坏 JSON，但七种坏法里有五种
    能被 L1/L2 就地救回来，剩下两种走一次修复调用。所以退出码仍然是 0，
    finding 仍然在。

    反面（直接把 ``failure_rate`` 当成「一定会失败」）很容易写错，
    所以这条测试单独立在这里。
    """
    code, out, err = _run(
        monkeypatch,
        capsys,
        "--spec",
        "security",
        "--diff",
        str(FIXTURES / "security_demo.diff"),
        mock_llm_failure_rate=1.0,
    )
    assert code == EXIT_OK, err
    payload = json.loads(out)
    assert payload["status"] in {"ok", "partial"}
    assert payload["findings"], "阶梯应该把结果救回来"


@pytest.mark.unit
def test_schema_failure_exits_with_a_distinct_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """模型彻底抽风时退出码必须**区别于**输入错误。

    两者在 CI 里的处置完全不同：输入错误要人去看文件，
    模型抽风要去看 prompt 和原始响应。合并成一个码，脚本就只能一律当成失败。

    这里直接换掉 provider 而不是调大 ``failure_rate`` —— 后者注入的坏法
    大多能被阶梯救回来（见上一条），用它测「彻底失败」会得到一条
    在某些随机种子下偶发通过的测试。
    """
    from sfly_agent.llm.base import LLMResponse

    class AlwaysGarbage:
        name = "garbage"
        model = "garbage-1"

        async def complete(
            self,
            *,
            system: str,
            user: str,
            max_tokens: int | None = None,
            temperature: float | None = None,
        ) -> LLMResponse:
            return LLMResponse(text="我觉得这段代码没什么问题。", model=self.model)

    settings = Settings(llm_provider="mock")
    monkeypatch.setattr("sfly_workers.__main__.get_settings", lambda: settings)
    monkeypatch.setattr("sfly_workers.__main__.build_llm", lambda *a, **kw: AlwaysGarbage())

    with pytest.raises(SystemExit) as exc:
        main(["--spec", "security", "--diff", str(FIXTURES / "security_demo.diff")])
    captured = capsys.readouterr()

    assert exc.value.code == EXIT_FAILED
    payload = json.loads(captured.out)
    assert payload["status"] == "failed"
    assert payload["error_class"] == "schema_unrecoverable"
    assert payload["raw_response"] == "我觉得这段代码没什么问题。", "没有原文就无法改进 prompt"
    assert "审查失败" in captured.err
    # 「没发现问题」和「没跑成」必须读起来完全不同
    assert "未发现问题" not in captured.err


# --------------------------------------------------------------------------- #
# 参数
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_max_files_caps_the_review_and_says_so(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """大 PR 要有上限，而且**必须在摘要里说出来** ——
    静默截断会让「只审了 1 个文件」看起来像「4 个文件都没问题」。"""
    _code, out, err = _run(
        monkeypatch,
        capsys,
        "--spec",
        "security",
        "--diff",
        str(FIXTURES / "security_demo.diff"),
        "--max-files",
        "1",
    )
    payload = json.loads(out)
    reviewed = {f["file"] for f in payload["findings"]}
    assert reviewed <= {"app/api.py"}, f"只该审第一个文件，实际出现 {reviewed}"
    assert "已按上限截取" in err


@pytest.mark.unit
@pytest.mark.parametrize("spec", ["security", "performance", "style"])
def test_no_rules_still_runs_and_clears_rule_ids(
    spec: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--no-rules`` 用来验证「没有规则时会不会开始自由发挥」。

    注意 ``rule_id`` 全部变成 null —— 没送规则时任何 rule_id 都是编的，
    ``reconcile_findings`` 会如实清掉它。
    """
    code, out, _err = _run(
        monkeypatch, capsys, "--spec", spec, "--diff", str(FIXTURES / "security_demo.diff"), "--no-rules"
    )
    assert code == EXIT_OK
    assert all(f["rule_id"] is None for f in json.loads(out)["findings"])


@pytest.mark.unit
def test_unknown_spec_fails_fast_with_the_valid_options(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """打错字时最需要看到的是可选项列表。

    ``--spec`` 走 argparse 的 choices，``spec_for`` 是第二道（给非 CLI 调用方）。
    两道都必须在报错里列出可选值 —— 这个入口唯一的用户就是命令行上的人。
    """
    with pytest.raises(SystemExit) as exc:
        main(["--spec", "securty", "--diff", str(FIXTURES / "clean.diff")])
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "security" in err and "performance" in err and "style" in err


# --------------------------------------------------------------------------- #
# 死信判定（纯函数，不需要队列也不需要库）
#
# 这段逻辑的代价是不对称的：判早了，一条还会成功的任务被丢进死信；判晚了，
# 每条坏消息都会烧三次 prompt 的 token，而**没有任何地方会报错** ——
# 账单上只是多了一点。
# --------------------------------------------------------------------------- #


class _Handle:
    """一个 ``MessageHandle`` 桩，只有 ``attempt`` 有实际取值。

    另外两个方法**写出来就抛**：死信判定是纯函数，它不该去 ack 任何东西 ——
    真这么做了，这条测试要立刻说出来，而不是等某天在线上发现
    「判定了死信但消息没被 ack」。
    """

    def __init__(self, attempt: int) -> None:
        self.id = "1-1"
        self.attempt = attempt

    async def ack(self) -> None:
        raise AssertionError("死信判定不该 ack —— 那是调用方的事")

    async def to_dead_letter(self, error: str, error_class: ErrorClass) -> None:
        raise AssertionError("死信判定只回答「该不该」，发送是调用方的事")


def _failed(error_class: ErrorClass) -> WorkerResult:
    return WorkerResult.failed("01JTESTRUN0000000000000000", WorkerType.SECURITY, "boom", error_class)


@pytest.mark.unit
def test_a_non_retryable_failure_goes_to_the_dead_letter_on_the_first_try() -> None:
    """schema_unrecoverable / diff_too_large / repo_not_found / auth_revoked
    再试一百次的结果完全一样，而每一次都要烧一份 prompt。"""
    settings = Settings(llm_provider="mock")
    handle = _Handle(attempt=1)

    assert should_dead_letter(_failed(ErrorClass.SCHEMA_UNRECOVERABLE), handle, settings) is True
    assert should_dead_letter(_failed(ErrorClass.AUTH_REVOKED), handle, settings) is True


@pytest.mark.unit
def test_a_transient_failure_is_retried_up_to_max_attempts() -> None:
    """``attempt`` 从 1 开始，所以 ``MAX_ATTEMPTS=3`` 是**三次机会**，不是四次。

    边界写成 ``>`` 是最容易犯的错，而且它的症状是「明明写了 3 次却跑了 4 次」——
    多出来那一次要付一份完整的 prompt 钱。
    """
    settings = Settings(llm_provider="mock", max_attempts=3)

    assert should_dead_letter(_failed(ErrorClass.TRANSIENT), _Handle(1), settings) is False
    assert should_dead_letter(_failed(ErrorClass.TRANSIENT), _Handle(2), settings) is False
    assert should_dead_letter(_failed(ErrorClass.TRANSIENT), _Handle(3), settings) is True


@pytest.mark.unit
def test_a_successful_result_never_goes_to_the_dead_letter() -> None:
    """成功的结果当然不进死信 —— 但这条也顺手挡住「忘了判断 status」这种写法。"""
    settings = Settings(llm_provider="mock")
    ok = WorkerResult(task_id="01JTESTRUN0000000000000000", worker_type=WorkerType.SECURITY)

    assert should_dead_letter(ok, _Handle(attempt=9), settings) is False
