"""结构化输出 —— 从模型的自由文本里掏出经过校验的 finding 列表。

这是全项目最值得写测试的一个模块：**模型吐出来的东西永远不会是干净的 JSON**，
而每一次解析失败都等于整个 Worker 的结果归零。这里的每一条分支都对应一种
真实观察到的坏输出，不是假想出来的。

修复阶梯（``L0 → L4``）：
  ``L0`` 直接 ``json.loads``
  ``L1`` 花括号配平扫描 —— 从「这是我的分析：{...} 以上」这类杂散文本里
        切出完整 JSON；响应被截断时还能抢救出已经闭合的条目
  ``L2`` 清理后重试 —— 去代码围栏、去尾逗号、直引号化
  ``L3`` 一次修复调用 —— 把原文和错误一起发回去，要求只返回合法 JSON
  ``L4`` 放弃，保留原文（截断至 8KB）供复盘

**L1 为什么不能写成正则**：``re.search(r"\\{.*\\}", text)`` 用贪婪匹配在响应
被截断时会跨过对象边界，把若干个 finding 连成一坨；而它**不会报错**，
表现是「模型这次只报了 1 个问题」。你会去调 prompt，而真正的问题在解析器里。
所以这里手写字符状态机：字符串、转义、嵌套深度都显式跟踪。
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from pydantic import BaseModel, ValidationError

from sfly_agent.llm.base import LLMProvider
from sfly_shared.logging import get_logger

log = get_logger(__name__)

#: 修复阶梯的级别。数字会进日志和评测报表，所以用常量而不是裸字面量 ——
#: 「L1 占了多少比例」是判断该不该继续调 prompt 的唯一依据。
L0_EXACT: Final = 0
L1_BALANCED: Final = 1
L2_CLEANED: Final = 2
L3_REPAIRED: Final = 3
L4_GAVE_UP: Final = 4

LEVEL_NAMES: dict[int, str] = {
    L0_EXACT: "L0-exact",
    L1_BALANCED: "L1-balanced",
    L2_CLEANED: "L2-cleaned",
    L3_REPAIRED: "L3-repaired",
    L4_GAVE_UP: "L4-gave-up",
}

#: 存进 ``WorkerResult.raw_response`` 的上限。没有它就无法改进 prompt；
#: 有了它就不能不封顶 —— 一个失控的响应能撑爆 Postgres 的行。
RAW_RESPONSE_LIMIT: Final = 8192


# --------------------------------------------------------------------------- #
# 字符级扫描
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class _Frame:
    """一个尚未闭合的容器。``key`` 是它作为某个对象的值时所用的字段名。"""

    ch: str
    start: int
    key: str | None = None


def _string_end(text: str, start: int) -> tuple[int, bool]:
    """``start`` 指向开引号。返回 ``(闭合引号之后的下标, 是否真的闭合)``。

    不闭合是**正常情况**：响应被 ``max_tokens`` 砍断时最后一个字符串就是断的，
    而这个位置恰好是最有价值的诊断信息（模型正在写第 4 条 finding）。
    """
    i = start + 1
    n = len(text)
    while i < n:
        c = text[i]
        if c == "\\":  # 转义：跳过下一个字符，哪怕它是引号
            i += 2
            continue
        if c == '"':
            return i + 1, True
        i += 1
    return n, False


def _scan(text: str) -> tuple[list[str], list[_Frame]]:
    """扫一遍文本，返回 ``(顶层完整值的原文列表, 未闭合容器栈)``。

    两个返回值对应两种坏输出：前者治「JSON 淹没在散文里」，
    后者治「响应被截断」—— 它们需要完全不同的补救方式。
    """
    spans: list[str] = []
    stack: list[_Frame] = []
    pending_key: str | None = None
    key_for_next: str | None = None
    i = 0
    n = len(text)

    while i < n:
        c = text[i]

        if c == '"':
            end, closed = _string_end(text, i)
            if not closed:
                break  # 字符串断了，后面的一切都不可信
            pending_key = text[i + 1 : end - 1]
            i = end
            continue

        if c in "{[":
            stack.append(_Frame(c, i, key_for_next))
            key_for_next = None
            pending_key = None
        elif c in "}]":
            if stack:
                frame = stack.pop()
                if not stack:
                    spans.append(text[frame.start : i + 1])
            pending_key = None
        elif c == ":":
            key_for_next = pending_key
            pending_key = None
        elif not c.isspace():
            pending_key = None

        i += 1

    return spans, stack


def _parse_candidates(text: str) -> Any | None:
    """把文本里每一个顶层完整值逐个喂给 ``json.loads``，返回第一个成功的。

    逐个而不是只取第一个：散文里出现一个 ``{`` 是常事（比如「格式为 {json}」），
    只试第一个会在这里空手而归。
    """
    spans, _stack = _scan(text)
    for span in spans:
        try:
            return json.loads(span)
        except (json.JSONDecodeError, ValueError):
            continue
    return None


# --------------------------------------------------------------------------- #
# L2 清理
# --------------------------------------------------------------------------- #

_FENCE_RE = re.compile(r"```[a-zA-Z0-9_+-]*[ \t]*\r?\n(.*?)\r?\n?[ \t]*```", re.DOTALL)
_FENCE_OPEN_RE = re.compile(r"```[a-zA-Z0-9_+-]*[ \t]*\r?\n")
_FENCE_CLOSE_RE = re.compile(r"\r?\n?[ \t]*```\s*$")
_LANGUAGE_WORD_RE = re.compile(r"^\s*(?:json|JSON|Json)\s*[\r\n:]*")
_CURLY_QUOTES = str.maketrans({"“": '"', "”": '"'})


def _strip_fences(text: str) -> str:
    """去掉 markdown 代码围栏。

    先试成对的围栏；不成对（响应在围栏内被截断）时只去掉开围栏 ——
    这种半截围栏是最常见的截断形态之一，regex 只处理成对围栏的话会漏掉它。
    """
    m = _FENCE_RE.search(text)
    if m is not None:
        return text[: m.start()] + m.group(1) + text[m.end() :]
    return _FENCE_CLOSE_RE.sub("", _FENCE_OPEN_RE.sub("", text))


def _remove_trailing_commas(text: str) -> str:
    """去掉对象/数组末尾多余的逗号。

    **必须跳过字符串内部**：``{"message": "a, }"`` 里的逗号是正文，
    动它会静默改掉 finding 的内容 —— 一个改错了还照样能解析的 bug，
    比解析失败难发现得多。
    """
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == '"':
            end, closed = _string_end(text, i)
            out.append(text[i:end])
            if not closed:
                break
            i = end
            continue
        if c == ",":
            k = i + 1
            while k < n and text[k] in " \t\r\n":
                k += 1
            if k < n and text[k] in "}]":
                i += 1  # 这个逗号是多余的，丢掉
                continue
        out.append(c)
        i += 1
    return "".join(out)


def _cleanup_candidates(text: str) -> list[str]:
    """返回若干个「清理过的」候选串，调用方逐个尝试。

    **返回列表而不是单个结果**，是这个函数最要紧的设计。清理动作都是
    「有副作用的修复」：把全角引号换成直引号，如果它出现在一句中文 finding
    的正文里，就会把原本合法的 JSON 弄坏。让每个变体各自去试，
    弄坏了的那个自然会被淘汰 —— 前提是**原始文本也留在候选里**，
    否则我们就用一个可能改坏内容的版本覆盖了一个本来能用的版本。
    """
    base = text.strip().lstrip("﻿")
    base = _strip_fences(base)
    base = _LANGUAGE_WORD_RE.sub("", base).strip()

    candidates: list[str] = []
    for candidate in (
        base,
        _remove_trailing_commas(base),
        base.translate(_CURLY_QUOTES),
        _remove_trailing_commas(base.translate(_CURLY_QUOTES)),
    ):
        if candidate.strip() and candidate not in candidates:
            candidates.append(candidate)
    return candidates


# --------------------------------------------------------------------------- #
# 截断抢救
# --------------------------------------------------------------------------- #


def salvage_truncated(text: str) -> tuple[dict[str, list[Any]], str] | None:
    """从被截断的 JSON 里抢救出已经完整闭合的条目。

    这一步的价值用一句话就能说清：模型写了 8 条 finding，token 用完了，
    第 8 条只有半个。**代价应该是那半条，而不是全部 8 条。**

    只在整体解析失败后才调用 —— 能整体解析时根本不需要抢救。
    """
    _spans, stack = _scan(text)
    arrays = [f for f in stack if f.ch == "[" and f.key]
    if not arrays:
        return None

    frame = arrays[-1]
    key = frame.key
    if key is None:  # pragma: no cover - arrays 已经过滤过 key，这里只是让类型收敛
        return None
    items, _rest = _scan(text[frame.start + 1 :])
    salvaged: list[Any] = []
    for span in items:
        try:
            value = json.loads(span)
        except (json.JSONDecodeError, ValueError):
            continue
        salvaged.append(value)

    if not salvaged:
        return None
    return {key: salvaged}, f"响应被截断，抢救出 {len(salvaged)} 条已闭合的条目"


# --------------------------------------------------------------------------- #
# 解析入口
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Extraction:
    payload: Any
    level: int
    note: str = ""


def extract_json(text: str) -> Extraction | None:
    """走 L0 → L2，返回解析出的 payload；全部失败返回 ``None``（交给 L3）。"""
    if not text.strip():
        return None

    # L0：干净输出。这是常态，别让它付任何额外成本。
    try:
        return Extraction(json.loads(text), L0_EXACT)
    except (json.JSONDecodeError, ValueError):
        pass

    # L1：配平扫描。先试原文 —— 散文和围栏对状态机来说都只是噪音，
    # 所以这一步经常能直接命中，连清理都不必做。
    if (payload := _parse_candidates(text)) is not None:
        return Extraction(payload, L1_BALANCED, "从杂散文本中配平出 JSON")

    # L1 的截断分支。放在 L2 之前：抢救出来的条目是**原文里的**，
    # 而 L2 的清理是有损的，能用原文就不用改过的。
    if (salvaged := salvage_truncated(text)) is not None:
        payload, note = salvaged
        return Extraction(payload, L1_BALANCED, note)

    # L2：清理后重试，每个变体都要再走一遍配平（围栏去掉后可能就露出 JSON 了）
    for candidate in _cleanup_candidates(text):
        try:
            return Extraction(json.loads(candidate), L2_CLEANED, "清理后解析成功")
        except (json.JSONDecodeError, ValueError):
            pass
        if (payload := _parse_candidates(candidate)) is not None:
            return Extraction(payload, L2_CLEANED, "清理后配平解析成功")

    return None


# --------------------------------------------------------------------------- #
# 逐元素校验
# --------------------------------------------------------------------------- #


def _empty_is_an_answer(*, content_seen: bool) -> bool:
    """一个空的 ``findings`` 数组，是「模型认为没有问题」还是「内容丢了」？

    **这是本模块最容易出错的一处判断，而两个方向都很贵。**

    把它当成「没有问题」当成默认，会得到一种**最坏的静默失败**：模型被
    ``max_tokens`` 砍断 → 修复调用收到残片后回一个 ``{"findings": []}``
    （修复提示词里那句「宁少勿多」正是在推它这么做）→ 一串完整可用的
    findings 变成「未发现问题」，``status=ok``、``error=None``、报告不降级。
    M10 实测撞到过，用的就是线上那个 fixture：``tokens_out=4291``、
    ``raw={"findings": []}``、``items=0``。

    反过来一律当成「丢了」也不行：一个真正干净的 PR 会被标成降级，
    而它本来就不该有任何发现 —— 那会让「降级」这个徽章失去意义，
    最后没人再看它。

    所以判据是**在此之前有没有见过内容**，两个可观察的信号：

    * 上一轮**压根解析不出来** → 不知道里面有什么，按有内容算；
    * 上一轮有**非空的条目**（哪怕它们全都没通过校验）→ 内容确实存在过。

    被截断（``finish_reason='length'``）不用单独判：它必然表现为上面两种之一
    （``salvage_truncated`` 会尝试抢救出已闭合的条目，一条都救不回来就是
    解析失败）。两个信号都没有过，才认为这个空数组是真的。
    """
    return not content_seen


def items_from_payload(payload: Any, key: str) -> tuple[list[Any], str]:
    """从 payload 里取出条目列表，顺带说明遇到的是哪种非标准形态。

    模型不总是给 ``{"findings": [...]}``。实际见过：裸数组、
    ``{"findings": {"a.py": {...}}}`` 这种按文件分组、
    以及只报一条时忘了包数组的裸对象。这三种都能救，不该判为失败。
    """
    if isinstance(payload, list):
        return payload, ""
    if not isinstance(payload, dict):
        return [], f"payload 是 {type(payload).__name__}，不是对象也不是数组"

    value = payload.get(key)
    if isinstance(value, list):
        return value, ""
    if isinstance(value, dict):
        return list(value.values()), f"{key} 是对象而非数组，已取它的值"
    for other_key, other in payload.items():
        if isinstance(other, list):
            return other, f"没有 {key} 字段，改用 {other_key!r}"
    # 单条 finding 忘了包数组 —— 字段名对得上就认，否则宁可判失败也不要瞎猜
    if key in payload or "file" in payload:
        return [payload], "返回了单个对象而非数组"
    return [], f"payload 里没有 {key} 数组"


def validate_items[T: BaseModel](raw_items: Sequence[Any], item_type: type[T]) -> tuple[list[T], list[str]]:
    """**逐元素**校验，返回 ``(通过的条目, 每条的失败原因)``。

    12 条里有 1 条格式错，代价必须是 1 条而不是整个 Worker。整体校验
    （``TypeAdapter(list[Finding])``）做不到这一点：它一遇到坏条目就放弃整包，
    于是「模型报对了 11 个问题」和「模型什么都没报」在系统里长得一模一样。

    这也是 ``status="partial"`` 的来源 —— 它是一等公民，前端有降级徽章，
    评测用它区分「全错」和「部分对」。
    """
    valid: list[T] = []
    errors: list[str] = []

    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            errors.append(f"[{index}] 不是对象，是 {type(raw).__name__}")
            continue
        try:
            valid.append(item_type.model_validate(raw))
        except ValidationError as exc:
            errors.append(f"[{index}] {_brief_error(exc)}")

    return valid, errors


def _brief_error(exc: ValidationError) -> str:
    """把 ValidationError 压成一行。

    完整的 pydantic 报错有几十行，写进日志没人看、发给模型又太贵。
    只保留第一条错误的位置和原因 —— 它几乎总是根因，后面的都是它的连锁反应。
    """
    errors = exc.errors()
    if not errors:
        return str(exc)
    first = errors[0]
    loc = ".".join(str(x) for x in first.get("loc", ()))
    return f"{loc}: {first.get('msg', '')}".strip(": ")


# --------------------------------------------------------------------------- #
# 对外唯一入口
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class StructuredOutcome[T: BaseModel]:
    """一次结构化调用（含修复尝试）的完整结果。

    ``error`` 非空表示**彻底失败**（走了 L4），此时 ``items`` 必然为空。
    调用方应该据此构造 ``status="failed"`` 的 WorkerResult，
    **而不是**把它当成「模型认为没有问题」—— 那是最坏的一种静默失败：
    屏障会闭合、run 会成功、PR 上一条评论都没有，而没人知道为什么。
    """

    items: list[T] = field(default_factory=list)
    #: 有内容但没通过校验的条目数。``items`` 非空且这个数大于 0 → ``partial``
    dropped: int = 0
    drop_reasons: list[str] = field(default_factory=list)
    level: int = L4_GAVE_UP
    repairs: int = 0
    notes: list[str] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    cached_tokens: int = 0
    latency_ms: int = 0
    model: str = ""
    raw: str = ""
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def _repair_prompt(bad_text: str, problem: str, truncated: bool) -> str:
    """L3 修复调用的用户消息。

    ``truncated`` 走一条**完全不同**的指令。被 ``max_tokens`` 砍断时，
    让模型「把上面这段重新格式化成 json」是没用的 —— 它会原样再写一遍，
    然后再撞一次上限。截断的解法只有一个：让它少写几条。
    """
    if truncated:
        return (
            f"你上一次的回复在上面的 JSON 结束之前被输出长度上限截断了（{problem}）。\n"
            "请重新输出，并且：\n"
            "1. 只保留最重要的若干条，宁少勿多，**必须保证 JSON 完整闭合**；\n"
            "2. 每条只写必要的字段，不要解释、不要 markdown 代码围栏；\n"
            '3. 顶层必须是 `{"findings": [...]}` 的形式。\n\n'
            f"上一次的回复（已截断）：\n{bad_text}"
        )
    return (
        f"你上一次的回复不是合法的 JSON。错误：{problem}\n\n"
        "请**只**返回一个合法的 json 对象，不要任何解释文字、不要 markdown 代码围栏。"
        '格式必须是 `{"findings": [...]}`，每个元素的字段与之前要求的一致。\n\n'
        f"上一次的回复：\n{bad_text}"
    )


async def complete_structured[T: BaseModel](
    llm: LLMProvider,
    *,
    system: str,
    user: str,
    item_type: type[T],
    key: str = "findings",
    max_tokens: int | None = None,
    temperature: float | None = None,
    max_repairs: int = 1,
) -> StructuredOutcome[T]:
    """发一次补全，走完修复阶梯，返回逐元素校验过的条目。

    **异常边界（有意如此）**：传输层故障（超时、5xx）会原样抛出，
    由上层决定重试 —— 那是重试策略的职责。而**解析失败不抛**，
    它以 ``outcome.error`` 的形式返回，因为「模型返回了没法解析的东西」
    是一个需要记录、需要展示、需要进死锁队列的业务结果，不是一个异常。
    """
    started = time.perf_counter()
    outcome: StructuredOutcome[T] = StructuredOutcome(model=llm.model)
    last_text = ""
    problem = ""
    truncated = False
    #: 「这一轮之前，模型有没有产出过内容」。见 :func:`_empty_is_an_answer`。
    content_seen = False

    for attempt in range(max_repairs + 1):
        level_hint = "（修复后）" if attempt else ""
        if attempt == 0:
            response = await llm.complete(
                system=system, user=user, max_tokens=max_tokens, temperature=temperature
            )
        else:
            outcome.repairs += 1
            outcome.notes.append(f"L3 修复调用 #{outcome.repairs}：{problem[:120]}")
            response = await llm.complete(
                system=system,
                user=_repair_prompt(last_text[:RAW_RESPONSE_LIMIT], problem, truncated),
                max_tokens=max_tokens,
                temperature=temperature,
            )

        outcome.tokens_in += response.tokens_in
        outcome.tokens_out += response.tokens_out
        outcome.cached_tokens += response.cached_tokens
        outcome.model = response.model or outcome.model
        last_text = response.text
        truncated = response.truncated
        outcome.raw = last_text[:RAW_RESPONSE_LIMIT]

        extraction = extract_json(last_text)
        if extraction is None:
            # 解析不出来 = **不知道**里面原本有什么。按「有内容」记 —— 宁可
            # 多花一次修复调用，也不要把一次丢失伪装成「没有问题」。
            content_seen = True
            problem = f"解析不出 JSON{level_hint}（finish_reason={response.finish_reason!r}）"
            outcome.level = L4_GAVE_UP
            continue

        raw_items, shape_note = items_from_payload(extraction.payload, key)
        if shape_note:
            outcome.notes.append(shape_note)

        items, errors = validate_items(raw_items, item_type)
        outcome.level = extraction.level
        if extraction.note:
            outcome.notes.append(extraction.note)

        if items:
            outcome.items = items
            outcome.dropped = len(errors)
            outcome.drop_reasons = errors[:20]  # 只留前 20 条，够定位根因了
            outcome.error = None
            outcome.latency_ms = int((time.perf_counter() - started) * 1000)
            if errors:
                outcome.notes.append(f"逐元素校验丢弃 {len(errors)} 条")
            return outcome

        # 这一轮看见了内容吗？见 :func:`_empty_is_an_answer`。
        #
        # 这里**不单独判 ``response.truncated``** —— 它是多余的：被砍断的响应
        # 必然表现为下面两种之一（解析不出来，或者抢救出至少一条）。多写一个
        # 不改变结果的条件，只会在将来有人改了抢救逻辑时留下来继续说话。
        content_seen = content_seen or bool(raw_items)

        # 解析出来了但一条都没通过校验 —— 这值得一次修复调用，而且
        # 把校验错误发回给模型比说「重来一遍」有效得多。
        if not raw_items:
            if _empty_is_an_answer(content_seen=content_seen):
                outcome.items = []
                outcome.dropped = 0
                outcome.error = None
                outcome.latency_ms = int((time.perf_counter() - started) * 1000)
                return outcome
            problem = (
                f"{key} 数组是空的{level_hint}，但在这之前模型已经产出过内容"
                "（被截断或没法解析）—— 空数组是**丢失**，不是「没有问题」"
            )
            continue
        problem = f"全部 {len(raw_items)} 条都没通过校验：{errors[0] if errors else '未知'}"

    outcome.items = []
    outcome.error = problem
    outcome.dropped = 0
    outcome.drop_reasons = []
    outcome.level = L4_GAVE_UP
    outcome.latency_ms = int((time.perf_counter() - started) * 1000)
    log.warning(
        "structured.gave_up",
        model=outcome.model,
        repairs=outcome.repairs,
        problem=problem,
        raw_head=last_text[:200],
    )
    return outcome
