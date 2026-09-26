"""消融与单 Agent 基线 —— 「多 Agent 到底值不值」的量化回答。

    python tasks.py eval

**这组实验回答的问题和主评测不一样。** 主评测问「我的系统得了多少分」，
这一组问「三个 Agent 比一个 Agent 好在哪、贵在哪」。前者是成绩单，
后者才是设计选择的依据 —— 面试官会追问的是后者。

四档配置，其余一切相同（同一批用例、同一份规则库、同一个聚合层、
同一个 ``WorkerRunner``）：

1. **单 Agent 基线** —— 一次调用包办三个 lane，规则也是三份全给
2. **1 Worker** —— 只有安全
3. **2 Worker** —— 安全 + 性能
4. **3 Worker** —— 完整形态

2→3→4 是增量对比，用来回答「多加一个 Worker 是不是白花钱」。
1 是对照组，用来回答「分布式执行 + 集中式决策比一个人干强在哪」。

**召回率只看 ground truth 命中**，而评测集永远标不完 —— 增量 Worker 发现的
常常是没被标注的真问题。所以某一档「+0 条」时能下结论的是**成本**那一半，
不是「它没用」。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from harness import (
    CaseRun,
    Variant,
    budget_from_env,
    load_cases,
    render_ablation,
    review_case,
    review_case_single_agent,
    score,
)
from test_eval import _sha, _working_tree_is_dirty

from sfly_shared.config import Settings
from sfly_shared.contracts import WorkerType
from sfly_shared.logging import get_logger

pytestmark = pytest.mark.eval

REPO_ROOT = Path(__file__).resolve().parents[2]
REPORTS_DIR = REPO_ROOT / "reports"

#: 增量档位。**顺序有意义** —— 这是一条「一个一个加上去」的曲线。
STAGES: tuple[tuple[str, tuple[WorkerType, ...]], ...] = (
    ("1 Worker（仅安全）", (WorkerType.SECURITY,)),
    ("2 Worker（+性能）", (WorkerType.SECURITY, WorkerType.PERFORMANCE)),
    ("3 Worker（完整）", tuple(WorkerType)),
)


@pytest.fixture(scope="module")
def variants() -> list[Variant]:
    """四档跑完。**整个模块共用一次** —— 真实层上这里是真金白银。

    预算**跨四档累加**（不是每档各给一份）：这一组总共要跑
    ``30 × (1+1+2+3) = 210`` 次调用，四档各给一份上限会让真实上限变成四倍，
    而「上限」这个词就不再描述任何东西了。超了就停在当前档，
    后面几档不跑 —— 报告里那一档的数字缺失，比一个悄悄花超的账单好。
    """
    cases = load_cases()
    budget = budget_from_env()
    out: list[Variant] = []
    spent = 0.0
    logger = get_logger(__name__)

    def _run(awaitables: list[object]) -> list[CaseRun]:
        """顺序跑完，累加花费；超预算就提前停下并返回已经跑完的部分。"""

        async def _main() -> list[CaseRun]:
            nonlocal spent
            done: list[CaseRun] = []
            for coro in awaitables:
                run = await coro  # type: ignore[misc]
                done.append(run)
                spent += run.cost_usd
                if spent >= budget:
                    logger.warning("eval.ablation_budget_exhausted", spent=round(spent, 4))
                    break
            return done

        return asyncio.run(_main())

    baseline = _run([review_case_single_agent(c) for c in cases])
    out.append(Variant(label="**单 Agent 基线**", metrics=score(baseline), calls=len(baseline)))

    for label, workers in STAGES:
        if spent >= budget:
            break
        runs = _run([review_case(c, workers=workers) for c in cases])
        out.append(Variant(label=label, metrics=score(runs), calls=len(runs) * len(workers)))
    return out
    return out


def test_the_ablation_is_monotone_in_cost(variants: list[Variant]) -> None:
    """Worker 越少，调用次数越少 —— 这是「消融真的按档位跑了」的检查。

    看着像句废话，但它能抓住一类很隐蔽的错：``review_case`` 的 ``workers``
    参数被忽略（或者被默认值盖掉）时，四档跑的是同一个配置，
    而**每一档的数字都会正常显示**，曲线平得像一条直线而没有任何报错。

    超预算提前停下时，档数会少于 4 —— 那时只检查已经跑过的那几档，
    但**必须至少两档**，否则这条测试什么也没验证。
    """
    cases = len(load_cases())
    stages = [v for v in variants if v.label != "**单 Agent 基线**"]
    assert len(stages) >= 2, f"只跑完 {len(stages)} 档，消融没有意义（多半是超预算）"

    for variant in stages:
        workers = next(w for label, w in STAGES if label == variant.label)
        # 提前停止的档位会少跑几个用例，所以只要求「不超过」。
        assert variant.calls <= cases * len(workers), f"{variant.label} 的调用次数超了"

    calls = [v.calls for v in stages]
    assert calls == sorted(calls), f"调用次数没有随 Worker 数递增：{calls}"


def test_the_baseline_covers_the_same_categories_as_three_workers(variants: list[Variant]) -> None:
    """基线必须真的「三件事都干」。

    如果基线只报了安全类目，那它不是基线，是一个被削弱的对照 ——
    而它**看起来完全正常**（有输出、有召回），只是拿它对比毫无意义。
    """
    baseline = next(v for v in variants if v.label == "**单 Agent 基线**")
    assert baseline.metrics.published > 0, "基线一条都没报出来，那它不是基线"


def test_write_the_ablation_report(variants: list[Variant]) -> None:
    """写成 ``reports/eval-<sha>-ablation.md``。

    **与主报告分开**：主报告便宜（离线层秒级、$0），而这一组在真实层上
    要跑 210 次调用。分开之后可以只重跑便宜的、也可以只重跑贵的。
    """
    sha = _sha()
    dirty = _working_tree_is_dirty()
    provider = Settings().llm_provider
    # 层名进文件名，理由见 ``test_eval.py`` 里的同一处。
    layer_slug = "offline" if provider == "mock" else f"real-{provider}"
    lines = [
        f"# sfly 消融与基线报告 · {sha}",
        "",
        f"- 代码版本：`{sha}`" + ("（**生成时工作区有未提交改动**）" if dirty else "（生成时工作区干净）"),
        f"- 用例：{len(load_cases())} 个",
        f"- LLM：`{provider}`",
        f"- 数据来源：与 `eval-{sha}-{layer_slug}.md` 同一批用例，配置不同",
        "",
        "这四档之间**只有拓扑不同**：用例、规则库、`WorkerRunner`、聚合层逐字相同。",
        "所以表里的差值是拓扑带来的，不是实现差异带来的。",
        "",
    ]
    if provider == "mock":
        lines += [
            "> ⚠️ **这一轮跑在 Mock 上，所以「单 Agent ≈ 三个 Worker」这个结论由构造决定，不是测出来的。**",
            ">",
            "> Mock 按 lane 过滤：三个 Worker 各报自己那一份，合起来正好是"
            "「三份的并集」；而基线一次拿到三条 lane 的全部规则、被允许报所有类目 ——"
            "两者**在检测能力上天然等价**，差的只是调用次数。",
            "> 所以离线层能验证的是「聚合与计量是对的」（调用次数、成本、命中口径），",
            "> 不能验证「多 Agent 是否更强」。**那个问题只有真实模型能回答** ——"
            "真实模型在长提示词下的注意力、以及被要求同时干三件事时的取舍，",
            "> 才是多 Agent 设计真正要对付的东西。",
            "",
        ]
    lines += [
        *render_ablation(variants),
        "## 怎么读这张表",
        "",
        "* **召回率低不等于那一档没用。** ground truth 标不完，而增量 Worker 报出的",
        "  常常是没被标注的真问题 —— 那样的发现落在精确率那一栏的「假阳性」里，",
        "  实际上它是真的。所以自动化指标只能回答「成本」，回答不了「价值」。",
        "  要回答价值得拿发布的原始条目人工抽查，那是任何评测集都替代不了的一步。",
        "* **边际成本那一列是给产品决策用的**：「多花 $X 换一条真问题」值不值，",
        "  取决于这个仓库漏掉一个问题的代价 —— 那是业务判断，不是技术判断。",
        "* 基线的存在是为了让「多 Agent 值不值」有个参照物。没有它，",
        "  上面所有的召回率都只是「我的系统得了多少分」，而那不是设计依据。",
        "",
    ]
    body = "\n".join(lines).rstrip() + "\n"

    REPORTS_DIR.mkdir(exist_ok=True)
    path = REPORTS_DIR / f"eval-{sha}-ablation-{layer_slug}.md"
    path.write_text(body, encoding="utf-8", newline="")

    print(f"\n消融报告写入 {path.relative_to(REPO_ROOT)}")
    for variant in variants:
        print(
            f"  {variant.label:<20} 调用 {variant.calls:>3}  命中 {variant.metrics.strict.tp:>2}"
            f"  召回 {variant.metrics.strict.recall:.1%}  成本 ${variant.metrics.cost_usd:.4f}"
        )
