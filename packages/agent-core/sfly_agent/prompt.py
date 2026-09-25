"""提示词组装。

**顺序是刻意的，而且是可测量的**：稳定前缀在前，变化内容在后。

    system: [输出契约（三个 Worker 逐字相同）] + [人设（每个 Worker 一份）]
    user:   [规则] + [diff]

DeepSeek 的上下文缓存是**前缀缓存** —— 只要从第一个 token 开始到某处完全一致，
那一段就能命中缓存，价格降到约十分之一。所以任何会变的东西都必须排在最后：
把 diff 放到规则前面，等于每一次调用都是全新的前缀，缓存的收益直接归零。

三个 Worker 的 system 里那段输出契约是**逐字相同**的，所以它构成了一段
跨 Worker 共享的缓存前缀。这不是巧合，是把它放在人设前面的原因。

关于「提示词里必须出现字面词 json」：OpenAI 的 ``response_format=json_object``
在检测不到这个词时会直接报错，而 DeepSeek 虽然不报错，给出一个紧凑示例
仍然能显著降低结构错误率。两件事碰巧由同一段文字解决。
"""

from __future__ import annotations

from collections.abc import Sequence

from sfly_shared.contracts import FilePatch, Rule

__all__ = ["OUTPUT_CONTRACT", "build_system_prompt", "build_user_prompt", "render_file"]


#: 输出契约。**逐字相同地出现在三个 Worker 的 system 提示词里**，
#: 所以它必须完全不依赖具体的审查类型。
OUTPUT_CONTRACT = """\
你是一名代码审查助手。你会收到若干文件的 unified diff，以及一份可参考的规则清单。

## 输出格式（必须严格遵守）

只返回一个 json 对象，不要任何解释文字、不要 markdown 代码围栏。顶层结构必须是
`{"findings": [...]}`，其中每个元素形如：

{"file": "src/db.py", "line": 42, "severity": "high", "category": "sqli", "message": "一句话说明问题", "evidence": "触发问题的原始代码片段", "confidence": 0.85, "suggestion": "具体怎么改", "rule_id": "sec-sqli-001"}

字段约束：
- `file`：**必须**是下面出现过的文件路径，逐字照抄，不要改写、不要加前缀。
- `line`：**必须**是 diff 中新增行（以 + 开头）在新文件里的行号。
  报未变更的行是无效的 —— 那样的意见无法定位，会被直接丢弃。
- `severity`：critical / high / medium / low / info 之一。
- `category`：小写下划线形式，例如 sqli、secrets、n_plus_one、dead_code。
- `message`：一句话说清「这里有什么问题、会导致什么后果」。不要复述代码。
- `evidence`：从 diff 里逐字复制的触发片段，用于人工核对。没有就省略。
- `confidence`：0 到 1 的小数。**不确定就写低**，写高不会让它变成真的。
- `suggestion`：可执行的具体改法。不要写「建议优化」这类没有信息量的话。
- `rule_id`：命中规则清单里的某条时填它的 id，没命中就省略。

## 纪律

- 没有问题就返回 `{"findings": []}`。**不要为了凑数而报**：一条假警报
  会让作者开始忽略你的全部意见，代价远大于漏报一条。
- 一次只报一个独立问题。同一处代码的两个不同问题分成两条。
- 不确定的用低 confidence，不要不报 —— 低置信度的条目会被标注而不是被丢弃。
"""


def build_system_prompt(persona: str) -> str:
    """组装 system 提示词。**契约在前、人设在后**，见模块文档。"""
    return f"{OUTPUT_CONTRACT}\n## 你的专长\n\n{persona.strip()}\n"


def render_rule(rule: Rule) -> str:
    """渲染一条规则。

    ``body`` 原样带上是**故意的**：规则的价值在于解释「为什么」，
    只给标题会让模型退回它自己的先验，那我们辛苦写的规则库就白写了。
    但它会占用不少 token —— 所以规则数量必须靠检索控制（plan 节点的活），
    而不是靠在这里省略。
    """
    bits = [f"- [{rule.id}] {rule.title}（类目 {rule.category}"]
    if rule.severity_hint is not None:
        bits.append(f"，建议严重度 {rule.severity_hint.value}")
    if rule.cwe:
        bits.append(f"，{rule.cwe}")
    bits.append("）\n")
    body = "\n".join(f"  {line}" for line in rule.body.strip().splitlines())
    return "".join(bits) + body


def render_file(patch: FilePatch) -> str:
    """渲染一个文件。

    头部带上变更行数和语言，是为了让模型知道这个文件有多大 ——
    一个 +400 行的新文件和一处 +2 行的修改，值得投入的注意力完全不同。
    """
    flags = []
    if patch.is_new_file:
        flags.append("新文件")
    if patch.is_deleted_file:
        flags.append("被删除")
    if patch.truncated:
        flags.append("补丁已截断，只能看到前面一部分")
    suffix = f"，{'；'.join(flags)}" if flags else ""

    header = (
        f"### {patch.path}（{patch.language}，+{patch.additions} -{patch.deletions}"
        f"，{len(patch.changed_lines)} 个变更行{suffix}）"
    )
    return f"{header}\n\n```diff\n{patch.patch}\n```"


def build_user_prompt(
    *,
    patches: Sequence[FilePatch],
    rules: Sequence[Rule],
    language: str = "",
) -> str:
    """组装 user 提示词：规则在前，diff 在后。

    ``rules`` 为空是合法的（没有检索到相关规则），但要让模型知道
    这不是「可以随便报」的信号 —— 没有规则约束时它最容易开始自由发挥。
    """
    sections: list[str] = []

    if rules:
        rendered = "\n\n".join(render_rule(r) for r in rules)
        sections.append(
            f"## 可参考的规则\n\n命中其中某条时，在 finding 的 `rule_id` 里填它的 id。\n\n{rendered}"
        )
    else:
        sections.append(
            "## 可参考的规则\n\n（本次没有检索到与该变更相关的规则。"
            "请仅依据代码本身的语义判断，不要降低判定标准，也不要凭猜测报问题。）"
        )

    lang_note = f"本次变更主要语言：{language}。\n\n" if language else ""
    files = "\n\n".join(render_file(p) for p in patches)
    sections.append(f"## 待审查的变更\n\n{lang_note}{files}")

    if any(p.truncated for p in patches):
        sections.append(
            "## 注意\n\n有文件的补丁被截断了（标记为「补丁已截断」）。"
            "**不要对看不到的部分下任何结论** —— 只在可见的新增行上提出问题。"
        )

    sections.append("请按上面的输出格式返回 json。")
    return "\n\n".join(sections)


def dominant_language(patches: Sequence[FilePatch]) -> str:
    """取变更行最多的语言，作为整批文件的代表语言。

    用于规则的语言过滤。按行数取而不是按文件数：一个 PR 里 20 个
    配置文件 + 1 个改了 300 行的 Python 文件，主体显然是那个 Python 文件。
    """
    weight: dict[str, int] = {}
    for patch in patches:
        if patch.language and patch.language != "unknown":
            weight[patch.language] = weight.get(patch.language, 0) + max(1, len(patch.changed_lines))
    if not weight:
        return ""
    return max(weight.items(), key=lambda kv: kv[1])[0]
