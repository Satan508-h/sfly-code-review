"""提示词组装的测试。

这里测的不是「提示词写得好不好」——那要靠评测集。测的是三条**一旦破坏
就静默损失真金白银或准确率**的结构性约束：

  * ``json`` 字面词必须出现（OpenAI 的 ``response_format=json_object`` 会因此报错）
  * 稳定前缀必须在最前面（DeepSeek 的前缀缓存按前缀计价，顺序错了缓存全失效）
  * 截断的文件必须显式告知模型（否则它会为看不到的代码写评论）
"""

from __future__ import annotations

import pytest

from sfly_agent.prompt import (
    OUTPUT_CONTRACT,
    build_system_prompt,
    build_user_prompt,
    dominant_language,
    render_rule,
)
from sfly_shared.contracts import FilePatch, Rule, Severity, WorkerType
from sfly_workers.specs import SPECS, spec_for


def _patch(path: str = "app/db.py", *, lines: list[int] | None = None, **over: object) -> FilePatch:
    base: dict[str, object] = {
        "path": path,
        "language": "python",
        "patch": "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n+new",
        "changed_lines": lines if lines is not None else [1],
    }
    base.update(over)
    return FilePatch.model_validate(base)


def _rule(rule_id: str = "sec-sqli-001") -> Rule:
    return Rule(
        id=rule_id,
        title="SQL 注入",
        worker_type=WorkerType.SECURITY,
        category="sqli",
        severity_hint=Severity.CRITICAL,
        cwe="CWE-89",
        body="把外部输入拼进 SQL 文本，数据库就无法区分代码和数据。",
    )


# --------------------------------------------------------------------------- #
# 输出契约
# --------------------------------------------------------------------------- #


@pytest.mark.unit
@pytest.mark.parametrize("worker", list(SPECS))
def test_system_prompt_contains_the_literal_word_json(worker: WorkerType) -> None:
    """**必须出现字面词 ``json``。**

    OpenAI 的 ``response_format={"type": "json_object"}`` 在提示词里检测不到
    这个词时会直接返回 400。它在这里出现不是巧合，是那个接口的硬要求 ——
    删掉它会得到一个只在生产环境（真 provider）复现的报错。
    """
    system = build_system_prompt(spec_for(worker).persona)
    assert "json" in system
    assert '"findings"' in system


@pytest.mark.unit
def test_contract_is_byte_identical_across_workers() -> None:
    """三个 Worker 的 system 里那段契约逐字相同 —— 它构成跨 Worker 共享的
    缓存前缀。改成人设在前，缓存就只在同一种 Worker 内部命中。"""
    prompts = [build_system_prompt(spec.persona) for spec in SPECS.values()]
    assert all(p.startswith(OUTPUT_CONTRACT) for p in prompts)
    assert len({p[: len(OUTPUT_CONTRACT)] for p in prompts}) == 1


@pytest.mark.unit
def test_personas_differ_or_the_whole_design_is_pointless() -> None:
    """反面：如果三个人设一样，三种 Worker 就退化成一个。"""
    personas = {w: spec_for(w).persona for w in SPECS}
    assert len(set(personas.values())) == len(SPECS)
    assert all(len(p.strip()) > 50 for p in personas.values())


@pytest.mark.unit
def test_contract_states_the_output_shape_and_the_discipline() -> None:
    """契约里必须同时有「长什么样」和「什么时候闭嘴」。

    少了后半句，模型在模糊代码上会倾向于多报 —— 而一条假警报会让作者
    开始忽略全部意见，代价远大于漏报一条。
    """
    assert "findings" in OUTPUT_CONTRACT
    assert "confidence" in OUTPUT_CONTRACT
    assert "不要为了凑数而报" in OUTPUT_CONTRACT
    assert "变更行" in OUTPUT_CONTRACT or "新增行" in OUTPUT_CONTRACT


# --------------------------------------------------------------------------- #
# 顺序 = 钱
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_rules_come_before_the_diff_in_the_user_message() -> None:
    """**规则在 diff 前面。** 前缀缓存是按「从头开始连续一致」计价，
    把逐次变化的 diff 放前面，等于每一次调用都是全新的前缀。"""
    user = build_user_prompt(patches=[_patch()], rules=[_rule()])
    assert user.index("可参考的规则") < user.index("待审查的变更")


@pytest.mark.unit
def test_missing_rules_are_stated_explicitly_not_left_blank() -> None:
    """没有检索到规则时要**说明**，而不是留一段空白。

    留空白时模型会把它读成「可以自由发挥」，而实际含义是「这次没有参考依据，
    请只依据代码语义判断」。两者的精确率差得很远。
    """
    user = build_user_prompt(patches=[_patch()], rules=[])
    assert "没有检索到" in user
    assert "不要降低判定标准" in user


# --------------------------------------------------------------------------- #
# 截断必须显式告知
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_truncated_patch_is_warned_about() -> None:
    """补丁被截断时，模型**必须**知道。

    不告诉它，它会为看不见的部分写评论 —— 而这正是幻觉最喜欢藏身的地方：
    一条关于「这个文件缺少错误处理」的意见，在只看到前 30 行的前提下
    完全是猜测。
    """
    user = build_user_prompt(patches=[_patch(truncated=True)], rules=[])
    assert "补丁已截断" in user
    assert "不要对看不到的部分" in user


@pytest.mark.unit
def test_no_truncation_warning_when_nothing_is_truncated() -> None:
    """没有截断就不要提这件事 —— 每一句多余的话都在稀释真正的约束。"""
    user = build_user_prompt(patches=[_patch()], rules=[])
    assert "补丁已截断" not in user


@pytest.mark.unit
def test_file_header_carries_size_signals() -> None:
    """文件头要带语言、增删行数和变更行数。

    一个 +400 行的新文件和一处 +2 行的改动，值得投入的注意力完全不同 ——
    而这个判断只能靠这些数字。"""
    user = build_user_prompt(
        patches=[_patch("app/new.py", additions=400, deletions=0, is_new_file=True)],
        rules=[],
    )
    assert "app/new.py" in user
    assert "python" in user
    assert "新文件" in user
    assert "+400" in user


# --------------------------------------------------------------------------- #
# 规则渲染
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_rule_body_is_included_not_just_the_title() -> None:
    """规则的价值在解释「为什么」，只给标题会让模型退回它自己的先验 ——
    那我们辛苦写规则库就白写了。"""
    rendered = render_rule(_rule())
    assert "sec-sqli-001" in rendered
    assert "SQL 注入" in rendered
    assert "CWE-89" in rendered
    assert "无法区分代码和数据" in rendered


@pytest.mark.unit
def test_rule_order_is_preserved() -> None:
    """核心规则的顺序是**有意排的**（最核心的在前），不能重排。

    提示词里这一段的顺序变化会让前缀缓存每次失效 —— 而缓存单价差约十倍。
    """
    rules = [_rule("sec-secrets-001"), _rule("sec-sqli-001"), _rule("sec-authz-001")]
    user = build_user_prompt(patches=[_patch()], rules=rules)
    assert user.index("sec-secrets-001") < user.index("sec-sqli-001") < user.index("sec-authz-001")


# --------------------------------------------------------------------------- #
# 语言判定
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_dominant_language_is_weighted_by_changed_lines() -> None:
    """按变更行数取代表语言，而不是按文件数。

    一个 PR 里 20 个配置文件 + 1 个改了 300 行的 Python 文件，
    主体显然是那个 Python 文件。"""
    patches = [
        _patch("a.yaml", language="yaml", lines=[1]),
        _patch("b.yaml", language="yaml", lines=[1]),
        _patch("c.py", language="python", lines=list(range(1, 50))),
    ]
    assert dominant_language(patches) == "python"


@pytest.mark.unit
def test_dominant_language_is_empty_when_nothing_is_recognized() -> None:
    assert dominant_language([_patch("x.unknownext", language="unknown")]) == ""
    assert dominant_language([]) == ""
