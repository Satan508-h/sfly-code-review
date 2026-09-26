"""修复阶梯的测试 —— **全项目测试价值最高的一个文件**。

这里每一条用例都对应一种观察到的真实坏输出，而不是「试几个边界值」。
理由很直接：模型返回坏 JSON 是**必然事件**（不是事故），而每一次解析失败
都等于整个 Worker 的结果归零。把它测厚，比在别处多测十个函数有用得多。

三个层次：
  * ``extract_json`` —— 纯函数，表驱动喂各种坏文本
  * ``validate_items`` —— 逐元素校验，断言的重点是「坏 1 条不该毁掉 12 条」
  * ``complete_structured`` —— 整个阶梯，含 L3 修复调用
"""

from __future__ import annotations

import json

import pytest

from sfly_agent.llm.base import LLMResponse
from sfly_agent.llm.structured import (
    L0_EXACT,
    L2_CLEANED,
    L4_GAVE_UP,
    complete_structured,
    extract_json,
    items_from_payload,
    salvage_truncated,
    validate_items,
)
from sfly_shared.contracts import Finding, Severity

# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #

#: 被 ``max_tokens`` 砍在**第一条 finding 内部**的响应。这类残片一条都抢救
#: 不出来（``salvage_truncated`` 只能救已闭合的条目），所以它表现为解析失败 ——
#: 而它和「模型说没有问题」在旧的判断里长得一模一样。这才是 M10 实测撞到的形态。
_CUT_INSIDE_FIRST_ITEM = (
    '{"findings": [{"file": "app/api.py", "line": 12, "severity": "high", '
    '"category": "secrets", "message": "硬编码的数据库密码被提交进了仓库，'
    "任何能读代码的人都能拿到它，而且它会留在 git 历史里"
)

GOOD = json.dumps(
    {
        "findings": [
            {
                "file": "app/db.py",
                "line": 17,
                "severity": "critical",
                "category": "sqli",
                "message": "SQL 用 f-string 拼接",
                "confidence": 0.9,
            },
            {
                "file": "app/db.py",
                "line": 28,
                "severity": "medium",
                "category": "crypto",
                "message": "MD5 已不适合用于口令",
                "confidence": 0.7,
            },
        ]
    },
    ensure_ascii=False,
)


class FakeLLM:
    """按脚本逐次返回。``calls`` 记下每次的 prompt，供断言修复提示词的内容。

    ``finish_reason`` 可以是单个值（每次都用它），也可以是一串（按调用次序取）。
    区分开是必要的：**「截断」是一条独立的信息**，它走的是和普通解析失败
    完全不同的修复指令，而这个测试正是要证明那条指令真的不一样。
    """

    name = "fake"
    model = "fake-1"

    def __init__(self, *texts: str, finish_reason: str | tuple[str, ...] = "stop") -> None:
        self._texts = list(texts)
        self._finish: tuple[str, ...] = (finish_reason,) if isinstance(finish_reason, str) else finish_reason
        self.calls: list[str] = []

    async def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        self.calls.append(user)
        index = min(len(self.calls) - 1, len(self._texts) - 1)
        return LLMResponse(
            text=self._texts[index],
            model=self.model,
            tokens_in=10,
            tokens_out=5,
            finish_reason=self._finish[min(len(self.calls) - 1, len(self._finish) - 1)],
        )


# --------------------------------------------------------------------------- #
# extract_json：L0
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_clean_output_is_level_0() -> None:
    """干净输出是常态，不能让它付任何额外成本。"""
    got = extract_json(GOOD)
    assert got is not None
    assert got.level == L0_EXACT
    assert len(got.payload["findings"]) == 2


@pytest.mark.unit
def test_empty_and_whitespace_are_none_not_empty_payload() -> None:
    """空响应必须返回 ``None``（走修复阶梯），不能返回空对象。

    这是最容易写错的一处：把空串当成「模型没发现问题」，
    于是**一次网络层截断会伪装成一次干净的审查**。
    """
    assert extract_json("") is None
    assert extract_json("   \n\t ") is None


# --------------------------------------------------------------------------- #
# extract_json：L1 配平扫描
# --------------------------------------------------------------------------- #


@pytest.mark.unit
@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("markdown 围栏", f"```json\n{GOOD}\n```"),
        ("无语言标记的围栏", f"```\n{GOOD}\n```"),
        ("前后有解释文字", f"好的，我的审查结果如下：\n\n{GOOD}\n\n希望有帮助。"),
        ("围栏 + 解释文字", f"结果：\n```json\n{GOOD}\n```\n以上。"),
        ("后面的花括号不该被吞进去", "输出格式为 {json}，本次结果：\n" + GOOD + "\ntrailing }"),
    ],
)
def test_recoverable_by_balanced_scan(name: str, text: str) -> None:
    got = extract_json(text)
    assert got is not None, name
    assert len(got.payload["findings"]) == 2, name


@pytest.mark.unit
def test_greedy_regex_would_have_eaten_three_objects() -> None:
    """这条是**回归测试**，防的是把 L1 写成 ``re.search(r"\\{.*\\}", text)``。

    贪婪匹配会从第一个 ``{`` 一路吃到最后那个 ``}`` —— 如果那中间夹着
    散文里的花括号，切出来的东西就不是一个合法 JSON，而**不报错**。
    手工扫描只认「配平到深度 0」的位置，所以能精确切出真正的那个对象。
    """
    text = f"注意 {{{{占位符}}}} 的用法。\n\n{GOOD}\n\n就这些 {{结束}}"
    got = extract_json(text)
    assert got is not None
    assert got.payload["findings"][0]["file"] == "app/db.py"


@pytest.mark.unit
def test_braces_inside_string_values_do_not_confuse_the_scanner() -> None:
    """字符串里的 ``{`` ``}`` ``[`` 不参与配平。

    模型经常在 message 里写代码片段（``f"{user_id}"``），
    状态机必须知道那在引号里。
    """
    payload = {
        "findings": [
            {
                "file": "a.py",
                "line": 1,
                "severity": "low",
                "category": "x",
                "message": '用了 f"{a} 和 } 还有 [ 的写法',
                "confidence": 0.5,
            }
        ]
    }
    text = "结果：" + json.dumps(payload, ensure_ascii=False)
    got = extract_json(text)
    assert got is not None
    assert 'f"{a}' in got.payload["findings"][0]["message"]


@pytest.mark.unit
def test_escaped_quote_inside_a_string_does_not_end_it() -> None:
    """``\\"`` 是转义，不能当成字符串结束 —— 误判会让后面全部错位。"""
    payload = {
        "findings": [
            {
                "file": "a.py",
                "line": 1,
                "severity": "low",
                "category": "x",
                "message": '引号 \\" 和花括号 {"a": 1}',
                "confidence": 0.5,
            }
        ]
    }
    got = extract_json("前言 " + json.dumps(payload, ensure_ascii=False) + " 后记")
    assert got is not None
    assert '\\"' in got.payload["findings"][0]["message"]


# --------------------------------------------------------------------------- #
# extract_json：L2 清理
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_trailing_comma_is_cleaned() -> None:
    text = GOOD.rstrip()[:-1].rstrip() + ",\n}"
    got = extract_json(text)
    assert got is not None
    assert got.level == L2_CLEANED
    assert len(got.payload["findings"]) == 2


@pytest.mark.unit
def test_smart_quotes_are_cleaned() -> None:
    text = GOOD.replace('": ', "”: ").replace(', "', ", “")
    got = extract_json(text)
    assert got is not None
    assert len(got.payload["findings"]) == 2


@pytest.mark.unit
def test_trailing_comma_inside_a_string_is_not_touched() -> None:
    """**清理动作绝不能改到字符串正文。**

    一个把 ``{"message": "a, }"`` 改成 ``{"message": "a }"`` 的清理器
    会安静地篡改 finding 的内容 —— 而且改了之后照样能解析，
    所以没有任何东西会报错。这比解析失败难发现得多。
    """
    payload = {
        "findings": [
            {
                "file": "a.py",
                "line": 1,
                "severity": "low",
                "category": "x",
                "message": "结尾是 , } 和 , ] 的行",
                "confidence": 0.5,
            }
        ]
    }
    text = json.dumps(payload, ensure_ascii=False).rstrip()[:-1].rstrip() + ",\n}"
    got = extract_json(text)
    assert got is not None
    assert got.payload["findings"][0]["message"] == "结尾是 , } 和 , ] 的行"


@pytest.mark.unit
def test_chinese_full_width_punctuation_in_messages_survives() -> None:
    """全角标点在**正文里**是正确内容，不能被当成语法错误修掉。

    这是本项目特有的风险：所有规则、message、suggestion 都是中文，
    而中文里逗号顿号引号全是全角。任何「统一把全角换成半角」的清理
    都会把好好的输出改成乱码 —— 而它还能解析成功，所以是静默的。
    """
    message = "先说结论：这里“必须”用参数化查询，否则会被注入。"
    payload = {
        "findings": [
            {
                "file": "a.py",
                "line": 1,
                "severity": "low",
                "category": "x",
                "message": message,
                "confidence": 0.5,
            }
        ]
    }
    got = extract_json(json.dumps(payload, ensure_ascii=False))
    assert got is not None
    assert got.payload["findings"][0]["message"] == message


@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    [
        json.dumps({"findings": []}),  # 空数组是合法结论
        '{"findings": []}',
    ],
)
def test_empty_findings_array_is_a_valid_answer(text: str) -> None:
    """「没有问题」是一个**结论**，不是一个错误。"""
    got = extract_json(text)
    assert got is not None
    assert got.payload["findings"] == []


@pytest.mark.unit
def test_leading_bom_and_language_word_are_stripped() -> None:
    got = extract_json("﻿json\n" + GOOD)
    assert got is not None
    assert len(got.payload["findings"]) == 2


# --------------------------------------------------------------------------- #
# 截断抢救
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_truncated_response_keeps_the_complete_items() -> None:
    """模型写了 3 条，token 用完了，第 3 条只有半个。

    **代价应该是那半条，而不是全部 3 条。** 这是抢救存在的全部理由。
    """
    text = GOOD[: int(len(GOOD) * 0.55)]
    got = extract_json(text)
    assert got is not None
    assert got.payload["findings"], "至少要抢救出第一条"
    # 抢救出来的条目必须字段完整
    first = got.payload["findings"][0]
    assert first["file"] == "app/db.py"
    assert first["severity"] == "critical"


@pytest.mark.unit
def test_salvage_returns_none_when_nothing_is_complete() -> None:
    """砍在第一条的中间时，一条都救不回来 —— 那就该老实返回 None，交给 L3。"""
    assert salvage_truncated('{"findings": [{"file": "a.py", "line": 1, "sev') is None


@pytest.mark.unit
def test_salvage_ignores_incomplete_items_in_the_middle() -> None:
    """坏在中间的条目被跳过，后面的好条目照样救回来。

    「逐元素」在这里体现得最直接：一条坏的不该让它后面的全部消失。
    """
    text = (
        '{"findings": ['
        '{"file": "a.py", "line": 1, "severity": "low", "category": "x", "message": "好的", "confidence": 0.5},'
        '{"file": "b.py", "line": , "severity": "low"},'
        '{"file": "c.py", "line": 3, "severity": "low", "category": "x", "message": "也是好的", "confidence": 0.5}'
    )
    got = salvage_truncated(text)
    assert got is not None
    payload, _note = got
    assert [f["file"] for f in payload["findings"]] == ["a.py", "c.py"]


# --------------------------------------------------------------------------- #
# payload → 条目
# --------------------------------------------------------------------------- #


@pytest.mark.unit
@pytest.mark.parametrize(
    ("payload", "expect_files"),
    [
        ({"findings": [{"file": "a.py"}]}, ["a.py"]),
        ([{"file": "a.py"}], ["a.py"]),  # 裸数组
        ({"findings": {"x": {"file": "a.py"}}}, ["a.py"]),  # 按文件分了组
        ({"findings": {"x": {"file": "a.py"}, "y": {"file": "b.py"}}}, ["a.py", "b.py"]),
        ({"file": "a.py", "line": 1}, ["a.py"]),  # 只报一条，忘了包数组
        ({"issues": [{"file": "a.py"}]}, ["a.py"]),  # 字段名写错但意图明确
    ],
)
def test_non_standard_payload_shapes_are_recovered(payload: object, expect_files: list[str]) -> None:
    """模型不总是给 ``{"findings": [...]}``。

    这几种形态都真实出现过，都能救 —— 救不回来的只有「连一个列表都没有」。
    判为失败是下策：明明拿到了数据却丢掉了。
    """
    items, _note = items_from_payload(payload, "findings")
    assert [i["file"] for i in items] == expect_files


@pytest.mark.unit
def test_unrecognizable_payload_reports_instead_of_silently_empty() -> None:
    items, note = items_from_payload({"status": "ok"}, "findings")
    assert items == []
    assert note, "要说明为什么没取到，否则日志里只剩一个 0"


# --------------------------------------------------------------------------- #
# 逐元素校验：12 条里坏 1 条的代价必须是 1 条
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_one_bad_item_costs_one_item_not_the_whole_batch() -> None:
    raw = [
        {"file": "a.py", "line": 1, "severity": "high", "category": "sqli", "message": "m1"},
        {"file": "b.py", "line": "不是数字", "severity": "high", "category": "sqli", "message": "坏的"},
        {"file": "c.py", "line": 3, "severity": "high", "category": "sqli", "message": "m3"},
    ]
    valid, errors = validate_items(raw, Finding)
    assert [f.file for f in valid] == ["a.py", "c.py"]
    assert len(errors) == 1
    assert errors[0].startswith("[1]"), f"错误要带下标才能定位：{errors[0]}"


@pytest.mark.unit
def test_non_dict_item_is_reported_not_crashed() -> None:
    valid, errors = validate_items(["我是一个字符串", 42], Finding)
    assert valid == []
    assert len(errors) == 2


@pytest.mark.unit
def test_error_message_is_one_short_line() -> None:
    """完整的 pydantic 报错有几十行：写进日志没人看，发回给模型又太贵。"""
    _valid, errors = validate_items([{"file": "a.py"}], Finding)
    assert len(errors[0]) < 200
    assert "\n" not in errors[0]


@pytest.mark.unit
def test_coercion_pipeline_applies_through_validation() -> None:
    """契约层的容错要在真实路径上生效，而不只是单测里直接调 Finding。"""
    valid, errors = validate_items(
        [
            {
                "file": "a.py",
                "line": "L42",
                "severity": "ERROR",
                "category": "SQL Injection",
                "message": "m",
                "confidence": "high",
            }
        ],
        Finding,
    )
    assert errors == []
    assert valid[0].line == 42
    assert valid[0].severity is Severity.HIGH
    assert valid[0].category == "sqli"
    assert valid[0].confidence == 0.85


# --------------------------------------------------------------------------- #
# 完整阶梯
# --------------------------------------------------------------------------- #


@pytest.mark.unit
async def test_happy_path_costs_no_repair_call() -> None:
    llm = FakeLLM(GOOD)
    out = await complete_structured(llm, system="s", user="u", item_type=Finding)
    assert out.ok
    assert out.level == L0_EXACT
    assert out.repairs == 0
    assert len(out.items) == 2
    assert len(llm.calls) == 1


@pytest.mark.unit
async def test_unrecoverable_output_triggers_one_repair_call() -> None:
    """单引号这种坏法只能靠 L3 —— 而 L3 必须真的发出去。"""
    llm = FakeLLM(GOOD.replace('"', "'"), GOOD)
    out = await complete_structured(llm, system="s", user="u", item_type=Finding, max_repairs=1)
    assert out.ok
    assert out.repairs == 1
    assert len(out.items) == 2
    assert len(llm.calls) == 2
    # 修复提示词必须把「原文」和「错误」都带上，否则模型只能瞎猜
    assert "json" in llm.calls[1].lower()
    assert "findings" in llm.calls[1]


@pytest.mark.unit
async def test_giving_up_keeps_the_raw_response() -> None:
    """L4 时保留原文 —— 没有它就无法改进 prompt，只能靠猜。"""
    llm = FakeLLM("这不是 JSON，只是普通文字。")
    out = await complete_structured(llm, system="s", user="u", item_type=Finding, max_repairs=1)
    assert not out.ok
    assert out.level == L4_GAVE_UP
    assert out.items == []
    assert out.error
    assert out.raw == "这不是 JSON，只是普通文字。"
    assert out.repairs == 1


@pytest.mark.unit
async def test_schema_unrecoverable_does_not_loop_forever() -> None:
    llm = FakeLLM("还是不是 JSON")
    out = await complete_structured(llm, system="s", user="u", item_type=Finding, max_repairs=2)
    assert out.repairs == 2
    assert len(llm.calls) == 3, "初试 1 次 + 修复 2 次，不能更多"


@pytest.mark.unit
async def test_truncated_response_asks_for_fewer_items_not_reformatting() -> None:
    """截断的修复指令必须**不一样**。

    让模型「把上面这段重新格式化成 json」在截断场景下毫无作用 ——
    它会原样再写一遍，然后第二次撞上同一个 max_tokens。
    唯一有效的指令是「少写几条」。
    """
    llm = FakeLLM(GOOD[: len(GOOD) // 2], GOOD, finish_reason=("length", "stop"))
    await complete_structured(llm, system="s", user="u", item_type=Finding, max_repairs=1)
    repair_prompt = llm.calls[1]
    assert "截断" in repair_prompt
    assert "完整闭合" in repair_prompt
    # 反面：不能说「请重新格式化为 json」—— 那会原样再写一遍再撞一次上限
    assert "重新格式化" not in repair_prompt


@pytest.mark.unit
async def test_empty_findings_is_success_not_failure() -> None:
    """模型说「没问题」和模型「没说话」是两件事，必须区分开。"""
    llm = FakeLLM('{"findings": []}')
    out = await complete_structured(llm, system="s", user="u", item_type=Finding)
    assert out.ok
    assert out.items == []
    assert out.dropped == 0


@pytest.mark.unit
async def test_a_truncated_answer_that_collapses_to_empty_is_a_failure() -> None:
    """**M10 实测撞到的那条静默失败。**

    链条：响应被 ``max_tokens`` 砍断 → 修复调用收到残片 → 回一个
    ``{"findings": []}``（修复提示词里那句「宁少勿多」正是在推它这么做）
    → 一串本来可用的 findings 变成「未发现问题」，而 ``status=ok``、
    ``error=None``、报告不降级。

    实测的形态（线上那个 fixture，security Worker）::

        notes : ["L3 修复调用 #1：解析不出 JSON（finish_reason='length'）"]
        items : 0      ok: True      error: None
        raw   : {"findings": []}
        tokens: 2661 in / 4291 out

    一份因为被砍断而丢掉全部发现的报告，最后以「未发现问题」发布出去 ——
    这正是本项目最想防的那类失败：**没有任何一层会报错**。
    """
    llm = FakeLLM(_CUT_INSIDE_FIRST_ITEM, '{"findings": []}', finish_reason=("length", "stop"))

    out = await complete_structured(llm, system="s", user="u", item_type=Finding, max_repairs=1)

    assert not out.ok, "空数组出现在一次截断之后 —— 那是丢失，不是「没有问题」"
    assert out.error is not None and "丢失" in out.error
    assert len(llm.calls) == 2, "仍然要给它一次修复机会，而不是直接放弃"


@pytest.mark.unit
async def test_an_empty_answer_after_an_unparseable_one_is_also_a_failure() -> None:
    """上一轮压根解析不出来时，「不知道里面有什么」按**有内容**算。

    这条是同一个判断的另一个入口：没有截断标志，只是没法解析。判错的话
    得到的是一个更隐蔽的版本 —— 连 ``finish_reason`` 那条线索都没有。
    """
    llm = FakeLLM("我发现了 3 个问题，先说第一个……", '{"findings": []}')

    out = await complete_structured(llm, system="s", user="u", item_type=Finding, max_repairs=1)

    assert not out.ok
    assert "丢失" in (out.error or "")


@pytest.mark.unit
async def test_an_empty_answer_after_dropped_items_is_a_failure() -> None:
    """上一轮有 3 条、全都格式不对 → 修复后说「没有问题」同样是丢失。

    非空条目（哪怕一条都没通过校验）是「内容存在过」的证据 —— 上一轮有三条
    东西，这一轮一条都没有，消失的那三条就是丢了。
    """
    bad = json.dumps({"findings": [{"nope": 1}, {"nope": 2}, {"nope": 3}]})
    llm = FakeLLM(bad, '{"findings": []}')

    out = await complete_structured(llm, system="s", user="u", item_type=Finding, max_repairs=1)

    assert not out.ok


@pytest.mark.unit
async def test_a_clean_pr_is_not_punished_for_saying_nothing() -> None:
    """反面：真正干净的 PR 不该被标成降级。

    判据**不能**是「空数组一律可疑」—— 那会让降级徽章失去意义，最后没人再看它
    （和一个永远亮着的报警灯一样没用）。``{"findings": {}}`` 是模型按文件分组、
    而一个文件都没出问题时的形态，同样是合法的「没有问题」。
    """
    for text in ('{"findings": []}', '{"findings": {}}'):
        out = await complete_structured(FakeLLM(text), system="s", user="u", item_type=Finding)

        assert out.ok, text
        assert out.items == []


@pytest.mark.unit
async def test_tokens_accumulate_across_repair_calls() -> None:
    """成本要按**实际发生的调用**累计，否则修复重试的开销在报表里是隐形的。"""
    llm = FakeLLM("坏的", GOOD)
    out = await complete_structured(llm, system="s", user="u", item_type=Finding, max_repairs=1)
    assert out.tokens_in == 20  # FakeLLM 每次报 10
    assert out.tokens_out == 10
