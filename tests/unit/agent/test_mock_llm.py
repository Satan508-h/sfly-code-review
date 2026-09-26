"""Mock LLM 的测试。

**它的正确性标准是「可复现」和「真的读懂了 diff」，而不是「像个模型」。**
下游所有东西都建在它上面 —— M2/M3 的队列往返、M5 的图端到端、M9 的评测基线。
如果它输出的是随机内容，那些测试和数字就全都没有意义。

所以这里断言的重点是：同样的输入永远得到同样的输出、行号落在变更行上、
不越界报别的 Worker 的问题、以及干净代码上一条都不报。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from sfly_agent.llm.mock import FAILURE_MODES, MockLLM, _inject_failure
from sfly_agent.prompt import build_system_prompt, build_user_prompt
from sfly_shared.contracts import FilePatch, Rule, WorkerType
from sfly_shared.diff import parse_unified_diff
from sfly_workers.specs import SECURITY, spec_for

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures"


def _patches(name: str) -> list[FilePatch]:
    return parse_unified_diff((FIXTURES / name).read_text(encoding="utf-8")).patches


async def _raw(name: str, spec_name: str, **kwargs: object) -> str:
    """跑一次 Mock，返回它「吐出来」的原文（可能是坏的）。"""
    spec = spec_for(spec_name)
    llm = MockLLM(worker_types=(spec.worker_type,), **kwargs)  # type: ignore[arg-type]
    response = await llm.complete(
        system=build_system_prompt(spec.persona),
        user=build_user_prompt(patches=_patches(name), rules=[]),
    )
    return response.text


async def _review(name: str, spec_name: str, **kwargs: object) -> list[dict[str, Any]]:
    """跑一次 Mock，返回它「吐出来」的原始 finding 字典。

    刻意从**反序列化后的 JSON** 里取，而不是从 Mock 内部的 Finding 对象取 ——
    这样连「序列化时多带了一个字段」这类问题也一起测到了。
    """
    return list(json.loads(await _raw(name, spec_name, **kwargs))["findings"])


def _patches_from(path: str, lines: list[str]) -> list[FilePatch]:
    """把几行代码拼成一份最小 diff（新增文件）。

    比往 ``fixtures/`` 里塞一个文件轻 —— 那些 fixture 是给人读的示例，
    而这里要表达的是「某一行的写法」，塞进去反而让人以为它是一个用例。
    """
    body = "".join(f"+{line}\n" for line in lines)
    text = f"diff --git a/{path} b/{path}\n--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n{body}"
    return parse_unified_diff(text).patches


async def _categories(patches: list[FilePatch], spec_name: str) -> set[str]:
    """这份补丁会被报出哪些类目。"""
    spec = spec_for(spec_name)
    llm = MockLLM(worker_types=(spec.worker_type,))
    response = await llm.complete(
        system=build_system_prompt(spec.persona),
        user=build_user_prompt(patches=patches, rules=[]),
    )
    return {str(f["category"]) for f in json.loads(response.text)["findings"]}


# --------------------------------------------------------------------------- #
# 确定性
# --------------------------------------------------------------------------- #


@pytest.mark.unit
async def test_same_input_yields_byte_identical_output() -> None:
    """**这是 Mock 存在的全部意义。**

    一旦它带上一点随机性，「这个 fixture 应该报出 3 条」这类断言就写不了，
    而下游所有依赖它的测试都会开始间歇性失败 —— 那比直接报错更难处理。
    """
    a = await _review("security_demo.diff", "security")
    b = await _review("security_demo.diff", "security")
    assert a == b
    assert len(a) > 0


@pytest.mark.unit
async def test_failure_injection_is_also_reproducible() -> None:
    """**连故障注入都必须可复现。**

    随机种子由提示词内容决定，不是全局随机。否则「这个 fixture 能不能被
    修复阶梯救回来」根本没法写成断言 —— 而那条路径恰恰是最需要测的。

    注意比对的是**原始文本**而不是解析后的结果：注入了故障的响应本来就
    不该能被解析，拿解析结果比会把这条测试变成一个永远失败的假警报。
    """
    first = await _raw("security_demo.diff", "security", failure_rate=0.5)
    second = await _raw("security_demo.diff", "security", failure_rate=0.5)
    assert first == second
    clean = await _raw("security_demo.diff", "security")
    assert first != clean, "failure_rate=0.5 时应当至少真的坏了一次（种子由内容决定）"


@pytest.mark.unit
@pytest.mark.parametrize("mode", FAILURE_MODES)
def test_every_failure_mode_actually_breaks_the_json(mode: str) -> None:
    """每一种注入都必须真的把 JSON 弄坏。

    这条防的是一类很隐蔽的失效：某个注入模式在当前输出上变成了**空操作**
    （比如「把 null 换成 None」——而响应里根本没有 null），
    于是那一格测试永远是绿的，而它什么都没测。
    """
    clean = json.dumps(
        {
            "findings": [
                {
                    "file": "a.py",
                    "line": 1,
                    "severity": "low",
                    "category": "x",
                    "message": "m",
                    "confidence": 0.5,
                }
            ]
        }
    )
    if mode in {"truncated"}:
        # 截断在小样本上仍是合法 JSON 是正常的，单独用大样本测
        return
    broken, _ = _inject_failure(clean, mode)
    if mode in {"fenced", "prose"}:
        # 这两种坏法**外面**坏了但里面还是合法 JSON —— 正是 L1 要处理的形态
        assert broken != clean
        assert json.loads(clean) is not None
        with pytest.raises(json.JSONDecodeError):
            json.loads(broken)
    else:
        with pytest.raises(json.JSONDecodeError):
            json.loads(broken)


# --------------------------------------------------------------------------- #
# 真的读懂了 diff
# --------------------------------------------------------------------------- #


@pytest.mark.unit
async def test_findings_land_on_added_lines_of_the_real_files() -> None:
    """报出来的 ``(文件, 行号)`` 必须真的落在变更行上。

    这是「Mock 真的读了 diff」与「Mock 在编造」的分界线。
    编造的行号会让 ``reconcile_findings`` 把所有结果降级成文件级评论，
    于是整条链路看起来在跑，实际上没有一条 inline 意见能发出去。
    """
    patches = {p.path: p for p in _patches("security_demo.diff")}
    findings = await _review("security_demo.diff", "security")
    assert findings

    for finding in findings:
        path = str(finding["file"])
        assert path in patches, f"报了一个不存在的文件：{path}"
        assert int(finding["line"]) in patches[path].changed_lines, f"{path}:{finding['line']} 不在变更行上"


@pytest.mark.unit
async def test_evidence_is_copied_verbatim_from_the_diff() -> None:
    """``evidence`` 是从 diff 里逐字复制的 —— 人工核对时靠它。

    上面那句注释不是修辞：``reconcile_findings`` 之后会用它做证据比对，
    而比对的前提是它确实来自原文。
    """
    findings = await _review("security_demo.diff", "security")
    by_line = {int(f["line"]): str(f.get("evidence", "")) for f in findings if f["file"] == "app/db.py"}
    assert by_line[8].startswith("API_KEY =")
    assert by_line[32] == "return pickle.loads(blob)"


@pytest.mark.unit
async def test_detects_the_whole_smorgasbord() -> None:
    """一份故意写坏的 diff 必须把各类问题都抓出来。

    少抓一条通常意味着某条正则写错了（或者被某个 unless 意外挡掉），
    而那种错误的表现是「这条规则从来没触发过」—— 很容易被误读成
    「代码里没有这种写法」。
    """
    findings = await _review("security_demo.diff", "security")
    categories = {str(f["category"]) for f in findings}
    assert {
        "sqli",
        "secrets",
        "crypto",
        "deserialization",
        "command_injection",
        "insecure_random",
        "path_traversal",
        "ssrf",
        "xss",
    } <= categories


@pytest.mark.unit
async def test_n_plus_one_uses_the_loop_from_context_lines() -> None:
    """循环体是新增的、``for`` 那行是原有的 —— 这是 N+1 最常见的真实形态。

    只看新增行的实现会**恰好漏掉**这一类：``for`` 是上下文行，
    而循环体里那句话本身看不出任何问题。
    """
    findings = await _review("security_demo.diff", "performance")
    n_plus_one = [f for f in findings if f["category"] == "n_plus_one"]
    assert len(n_plus_one) == 1
    assert n_plus_one[0]["file"] == "app/report.py"
    assert n_plus_one[0]["line"] == 10


@pytest.mark.unit
async def test_a_fluent_chain_split_across_lines_is_still_an_unbounded_query() -> None:
    """链式调用换行写时也必须能被认出来。

    ``unbounded_query`` 的 ``\\b`` 原本写在分组外面，于是它要求「匹配起点前
    是词边界」—— 而起点是 ``.``，点号前面是空白时**没有边界**。后两个分支
    （``.all()`` / ``.scalars()``）因此永远匹配不到，唯一能匹配的是 ``SELECT *``。

    结果是**最地道的那种写法恰好是唯一看不见的**：同一句挤在一行里
    （``q.all()``，字母与点之间有边界）认得出，换行写（``    .all()``）认不出。
    M9 写评测集时撞上的：一个 ``.limit(20).all()`` 链式分页被判成「干净代码」。

    这条测试盯的是**两个分支都活着**，所以两种写法各断言一次。
    """
    inline = _patches_from(
        "app/a.py",
        [
            '    rows = session.query("SELECT id FROM t").all()',
            "    return rows",
        ],
    )
    chained = _patches_from(
        "app/b.py",
        [
            "    rows = (",
            '        session.query("SELECT id FROM t")',
            "        .all()",
            "    )",
            "    return rows",
        ],
    )

    for patches in (inline, chained):
        categories = await _categories(patches, "performance")
        assert "unbounded_query" in categories


@pytest.mark.unit
async def test_a_screaming_snake_constant_is_still_a_hardcoded_credential() -> None:
    """``DB_PASSWORD = "..."`` 必须被认出来。

    凭据检测原本用 ``\\b`` 打头，而**下划线是词字符** —— 于是它要求「``password``
    前面是词边界」时，``DB_PASSWORD`` 里那个下划线不算边界，整条规则对
    **全大写常量**完全失效。而那正是模块级常量最常见的写法，
    也就是硬编码凭据最常见的落点：一个写满凭据的文件被判成干净。

    同一类错误在 ``unbounded_query`` 上也有一份（``\\b`` 写在分组外面），
    两条都是写评测集时才暴露出来的 —— 单纯的「能报出问题」的测试不会碰到它们，
    因为它们测的恰好是**规则认不出来的写法**。
    """
    patches = _patches_from(
        "app/settings.py",
        [
            'DB_PASSWORD = "Pr0d-Passw0rd-2024"',
            'BILLING_API_KEY = "sk_live_9f4c2a7d8e1b6305"',
        ],
    )
    categories = await _categories(patches, "security")
    assert "secrets" in categories


# --------------------------------------------------------------------------- #
# 纪律
# --------------------------------------------------------------------------- #


@pytest.mark.unit
@pytest.mark.parametrize("spec_name", ["security", "performance", "style"])
async def test_clean_diff_yields_nothing(spec_name: str) -> None:
    """干净代码上一条都不报。

    **这条比「能发现问题」更重要。** 一个在正确代码上乱报的审查工具，
    会让作者开始忽略它的全部意见 —— 那时它的真实价值归零，
    而不是下降一点。``fixtures/clean.diff`` 里是参数化查询、批量取数、
    有界分页的写法，三个 Worker 都必须保持沉默。
    """
    assert await _review("clean.diff", spec_name) == []


@pytest.mark.unit
async def test_worker_only_reports_its_own_lane() -> None:
    """安全 Worker 不该报风格问题。

    越界报的问题在聚合阶段会污染 ``sources``（跨 Worker 印证是最值钱的信号），
    而冲突消解的 ``category_authority`` 规则也会因此判错 ——
    一个「安全 Worker 报的风格问题」在冲突里会被当成专业意见。
    """
    security_categories = set(SECURITY.categories)
    findings = await _review("security_demo.diff", "security")
    assert {str(f["category"]) for f in findings} <= security_categories

    style_findings = await _review("security_demo.diff", "style")
    assert {str(f["category"]) for f in style_findings} <= set(spec_for("style").categories)


@pytest.mark.unit
async def test_no_findings_is_an_empty_array_not_an_omitted_key() -> None:
    """空结果必须是 ``{"findings": []}``。

    省略这个键会让解析器要靠猜测来区分「没有问题」和「响应被截断在
    这个键之前」—— 而这两件事的处置完全相反。
    """
    llm = MockLLM(worker_types=(WorkerType.SECURITY,))
    response = await llm.complete(system="s", user="没有任何 diff 的普通文本")
    assert json.loads(response.text) == {"findings": []}


@pytest.mark.unit
async def test_model_output_does_not_leak_worker_only_fields() -> None:
    """``source_line_verified`` 和 ``fingerprint`` 不在 LLM 的输出契约里。

    让 Mock 带上它们，契约测试就会以为模型真的会报这两个字段 ——
    而真实模型永远不会，于是接线的时候才发现，那时已经晚了。
    """
    findings = await _review("security_demo.diff", "security")
    for finding in findings:
        assert "source_line_verified" not in finding
        assert "fingerprint" not in finding
        assert set(finding) <= {
            "file",
            "line",
            "end_line",
            "severity",
            "category",
            "message",
            "evidence",
            "confidence",
            "suggestion",
            "rule_id",
        }


@pytest.mark.unit
async def test_long_lines_short_circuit_other_rules_on_the_same_line() -> None:
    """一行又长又含漏洞时只报长行，不再叠三条别的。

    同一行刷出三四条 finding 会把真正的告警淹没在格式噪音里，
    而作者会得出「这个工具很吵」的结论 —— 之后就再也不看了。
    """
    # 补丁必须带完整的文件头。这不是为了好看：没有 diff --git / --- / +++ 头，
    # 解析器根本不会把这段文本当成一个文件块 —— 而在真实链路里补丁永远
    # 来自 parse_unified_diff，所以带头是常态，手工构造时最容易漏掉。
    long_line = "x = " + "a" * 130  # 超过 120 且什么规则都不命中
    patch = FilePatch(
        path="a.py",
        language="python",
        patch=(f"diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n+{long_line}"),
        changed_lines=[1],
    )
    llm = MockLLM(worker_types=(WorkerType.STYLE,))
    response = await llm.complete(system="s", user=build_user_prompt(patches=[patch], rules=[]))
    findings = json.loads(response.text)["findings"]
    assert [f["category"] for f in findings] == ["formatting"]


@pytest.mark.unit
async def test_rule_id_is_filled_from_the_matched_rule() -> None:
    """命中规则时回填 ``rule_id`` —— 它是置信度 ``grounded`` 加成的唯一来源。

    回填错了（或者永远为 None）会让 +0.10 的加成永远拿不到，
    而置信度公式会整体偏低 —— 表现是「大量 finding 被抑制阈值砍掉」。
    """
    findings = await _review("security_demo.diff", "security")
    tagged = {str(f["category"]): f.get("rule_id") for f in findings}
    assert tagged["sqli"] == "sec-sqli-001"
    assert tagged["secrets"] == "sec-secrets-001"


@pytest.mark.unit
async def test_cost_fields_are_estimated_not_zero() -> None:
    """Mock 也要报 token 数。

    报 0 会让本地跑出来的成本报告全是 0，而那份报告是要写进 README 和
    评测报表的 —— 一个全是 0 的成本表比没有成本表更误导。
    """
    llm = MockLLM(worker_types=(WorkerType.SECURITY,))
    response = await llm.complete(system="x" * 100, user="y" * 400)
    assert response.tokens_in > 100
    assert response.tokens_out > 0
    assert response.cached_tokens == 0, "Mock 没有前缀缓存，编一个命中率会污染评测"
    assert response.finish_reason == "stop"


@pytest.mark.unit
async def test_mock_does_not_consult_rules_for_its_lane() -> None:
    """传不传规则，Mock 的检测结果都一样。

    这是刻意的：Mock 检测的是**代码里的真实模式**，不是「规则清单里有什么」。
    这样规则库的检索质量就能被单独测量 —— 否则「检索到的规则是否提升了
    精确率」这个问题会被 Mock 的行为掩盖掉。
    """
    spec = spec_for("security")
    patch = _patches("security_demo.diff")
    rule = Rule(
        id="sec-sqli-001",
        title="SQL 注入",
        worker_type=WorkerType.SECURITY,
        category="sqli",
        body="b",
    )
    llm = MockLLM(worker_types=(WorkerType.SECURITY,))
    with_rules = await llm.complete(
        system=build_system_prompt(spec.persona),
        user=build_user_prompt(patches=patch, rules=[rule]),
    )
    without = await llm.complete(
        system=build_system_prompt(spec.persona),
        user=build_user_prompt(patches=patch, rules=[]),
    )
    assert json.loads(with_rules.text)["findings"] == json.loads(without.text)["findings"]
