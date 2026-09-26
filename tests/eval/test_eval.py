"""评测集 —— 离线层（Mock LLM）跑一遍，出报告。

    python tasks.py eval

**这里测的不是「系统对不对」，而是「系统有多好」。** 断言只写那些必须成立、
且不随调参漂移的东西：

* 评测集本身是合法的（ground truth 落在真实的新增行上、类目在分类体系里）
* 每条注入用例**真的能被测到** —— 一条谁也测不出来的用例会让所有指标变好看，
  而它什么都没验证
* 干净代码上不出现高危误报
* 跑两遍数字一样

指标本身、阈值扫描、逐用例明细都写进 ``reports/eval-<sha>.md`` ——
那份报告才是产物，测试只是保证它没有建立在错误的输入上。

### 不能拿这份报告讲的事

离线层用的是 Mock —— 一个确定性的正则扫描器。所以：

* **精确率/召回率量的是「聚合层」，不是「模型的审查能力」。**
  这里根本没有模型。
* 干净组的误报率主要由**扫描器有多粗**决定（它一次只看一行），
  不能当成系统的精确率来讲。
* 真实模型上的数字要跑 ``python tasks.py eval --real``，那是另一层。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml
from harness import (
    CASES_DIR,
    EvalCase,
    all_merged,
    budget_from_env,
    load_cases,
    render_report,
    run_all,
    score,
    score_by_group,
    threshold_sweep,
)

from sfly_agent.aggregate.confidence import SUPPRESS_THRESHOLD
from sfly_shared.config import Settings
from sfly_shared.contracts import CATEGORY_OWNER, Severity, normalize_path
from sfly_shared.diff import iter_added_lines

pytestmark = pytest.mark.eval

REPO_ROOT = Path(__file__).resolve().parents[2]
REPORTS_DIR = REPO_ROOT / "reports"

#: 扫描的阈值档位。覆盖「闸几乎不开」到「闸几乎全关」。
SWEEP = (0.20, 0.25, 0.30, SUPPRESS_THRESHOLD, 0.40, 0.50, 0.60, 0.70)

#: 干净组上不允许出现这个级别及以上的发现。
#: 「在没问题的代码上报了一个 HIGH」比漏报一个 LOW 严重得多 ——
#: 它会让作者开始不信任全部意见，而那时工具的价值归零而不是下降一点。
CLEAN_SEVERITY_CEILING = Severity.HIGH


def _git(*args: str) -> str | None:
    """跑一条 git 命令。拿不到就返回 ``None``（没有 git 不该让评测跑不起来）。"""
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip()


def _sha() -> str:
    return _git("rev-parse", "--short", "HEAD") or "nogit"


def _working_tree_is_dirty() -> bool:
    """除 ``reports/`` 之外，工作区有没有未提交的改动。

    **必须排除 ``reports/``**：报告自己就写在那里，不排除的话每次都是「有改动」，
    这个标记就永远是真的、等于没有 —— 而它要回答的恰恰是
    「这份数字对应的是不是那个 commit」。

    这个标记存在的理由：报告文件名里的 sha 是**生成时的 HEAD**，
    而内容是**还没提交的工作区**跑出来的。两者通常不是一回事，
    不说清楚的话，面试官会照着 sha 去 checkout，然后发现对不上。
    """
    out = _git("status", "--porcelain", "--", ".", ":(exclude)reports")
    return bool(out)


# --------------------------------------------------------------------------- #
# 评测集本身的合法性
# --------------------------------------------------------------------------- #


def test_the_eval_set_is_well_formed() -> None:
    """id 唯一、与文件名一致、组别合法、用例数量够。"""
    cases = load_cases()
    assert cases, "评测集是空的"

    ids = [c.id for c in cases]
    assert len(ids) == len(set(ids)), f"用例 id 重复：{ids}"

    for case in cases:
        assert case.id == case.diff_path.stem, f"{case.id} 的 id 与文件名对不上"
        assert case.group in ("rebuilt", "injected", "clean"), f"{case.id} 的组别不合法"
        assert case.note, f"{case.id} 缺少说明 —— 报告里那一栏就空着"
        assert case.diff_path.exists(), f"{case.id} 缺 diff"

    by_group: dict[str, int] = {}
    for case in cases:
        by_group[case.group] = by_group.get(case.group, 0) + 1
    assert by_group.get("clean", 0) >= 3, "干净组太少，误报率没有意义"
    assert by_group.get("injected", 0) >= 10, "注入组太少"


def test_every_ground_truth_line_is_an_added_line() -> None:
    """ground truth 指的必须是**这次新增的**那一行。

    手写 diff 时数行号极容易数错，而数错的后果是**静默的**：
    一条永远不可能被命中的 ground truth 会让召回率永远差一截，
    或者更糟 —— 它恰好命中了无关的一行，于是评测夸了一个不存在的能力。
    交给机器数。
    """
    problems: list[str] = []
    for case in load_cases():
        added = {(normalize_path(item.path), item.line) for item in iter_added_lines(case.diff)}
        for item in case.expected:
            if (normalize_path(item.file), item.line) not in added:
                problems.append(f"{case.id}: {item.file}:{item.line} 不是新增行")
    assert not problems, "ground truth 有错：\n  " + "\n  ".join(problems)


def test_every_ground_truth_category_exists_in_the_taxonomy() -> None:
    """评测用的类目必须是分类体系里真有的。

    编一个类目名出来的话，冲突消解的职责域规则查不到归属、
    评测的匹配也永远是假的 —— 而这两件事都不会报错。
    """
    unknown = {
        item.category
        for case in load_cases()
        for item in case.expected
        if item.category not in CATEGORY_OWNER
    }
    assert not unknown, f"这些类目不在 CATEGORY_OWNER 里：{sorted(unknown)}"


def test_clean_cases_have_no_ground_truth() -> None:
    """干净组的 ground truth 必须是空的 —— 否则它就不是干净组。"""
    bad = [c.id for c in load_cases() if c.group == "clean" and c.expected]
    assert not bad, f"干净组里有用例标了 ground truth：{bad}"


def test_the_manifest_matches_the_directory() -> None:
    """每个 diff 都要有 yaml，每个 yaml 都要有 diff。"""
    diffs = {p.stem for p in CASES_DIR.glob("*.diff")}
    cases = {c.id for c in load_cases()}
    assert diffs == cases, f"多出来的 diff：{sorted(diffs - cases)}；缺 diff：{sorted(cases - diffs)}"


# --------------------------------------------------------------------------- #
# 离线层：Mock 跑一遍
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def eval_runs():  # type: ignore[no-untyped-def]
    """整批用例跑一遍。**整个模块共用一次** —— 真实层上这里要花钱。

    预算从 ``EVAL_BUDGET_USD`` 读（默认 $5）。Mock 的单价是 0，
    所以离线层永远不会触发它 —— 但真实层会，而那时**必须有东西拦着**。
    """
    return run_all(load_cases(), budget_usd=budget_from_env())


def test_every_injected_case_is_actually_detectable(eval_runs) -> None:  # type: ignore[no-untyped-def]
    """**每条注入用例都必须真的被测到 —— 越过置信度闸之前。**

    这条断言存在的理由：一条 Mock 根本认不出来的用例，会让召回率恒为 0、
    而精确率虚高（它没有假阳性，因为它什么也没说）。它**看起来是一条正常的用例**，
    报告里的数字也不会因此变红。所以它必须在闸之前就被抓住。

    用「全部合并后的发现」而不是「发布的发现」来判断，是为了把
    「扫描器认不出」和「置信度闸砍掉了」区分开 —— 后者是另一条断言的事。
    """
    blind = [run.case.id for run in eval_runs if run.case.group == "injected" and not all_merged(run)]
    assert not blind, f"这些用例 Mock 一条都没报出来（用例写错了，或者规则认不出）：{blind}"


def test_clean_cases_do_not_get_high_severity_findings(eval_runs) -> None:  # type: ignore[no-untyped-def]
    """干净代码上不许出现 HIGH 及以上的发现。"""
    alarms: list[str] = []
    for run in eval_runs:
        if run.case.group != "clean":
            continue
        for finding in run.report.findings:
            if finding.severity in (Severity.CRITICAL, Severity.HIGH):
                alarms.append(f"{run.case.id}: {finding.file}:{finding.line} {finding.category}")
    assert not alarms, f"干净代码上报了高危：{alarms}"


def test_the_offline_numbers_are_reproducible() -> None:
    """同一批用例跑两遍，指标必须逐字节一样。

    这是「确定性」这个设计目标唯一能被机器检查的形式。Mock LLM 是可复现的、
    聚合层是纯函数 —— 那么整条链路就应该是可复现的。挂掉说明有东西引入了
    非确定性（字典序、时间、随机数），而那种 bug 平时完全看不见。
    """
    cases = load_cases()
    first = score(run_all(cases))
    second = score(run_all(cases))

    assert (first.strict.tp, first.strict.fp, first.strict.fn) == (
        second.strict.tp,
        second.strict.fp,
        second.strict.fn,
    )
    assert first.published == second.published
    assert first.conflicts == second.conflicts


def test_the_sweep_is_monotone_in_the_obvious_direction(eval_runs) -> None:  # type: ignore[no-untyped-def]
    """阈值调低，发布的条数不会变少。

    看着像句废话，但它是**扫描本身正确**的唯一检查：如果 ``threshold_sweep``
    读错了数据（比如只扫了 ``findings`` 而漏了 ``suppressed``），
    非单调就会立刻暴露出来。
    """
    rows = threshold_sweep(eval_runs, SWEEP)
    counts = [row.published for row in rows]
    # ``SWEEP`` 是升序的阈值，所以条数必须是**降序** —— 门槛越高放过去越少。
    assert counts == sorted(counts, reverse=True), f"发布条数随阈值不单调：{counts}"
    assert counts[0] > counts[-1], "阈值从 0.2 提到 0.7 却一条都没少，扫描多半没读对数据"


# --------------------------------------------------------------------------- #
# 出报告
# --------------------------------------------------------------------------- #


def test_write_the_report(eval_runs) -> None:  # type: ignore[no-untyped-def]
    """把指标、阈值扫描、逐用例明细写成 ``reports/eval-<sha>.md``。

    报告**提交进仓库**：面试官 clone 下来直接读，不需要跑任何东西；
    而文件名里的 sha 让「这份数字对应哪个 commit」一眼可见 ——
    改了代码之后数字过期，也是一眼可见。
    """
    metrics = score(eval_runs)
    metrics.depth = 3
    sweep = threshold_sweep(eval_runs, SWEEP)
    sha = _sha()
    dirty = _working_tree_is_dirty()
    provider = Settings().llm_provider
    budget = budget_from_env()
    is_mock = provider == "mock"
    truncated = metrics.runs < len(load_cases())

    if is_mock:
        layer = "离线层 · Mock LLM"
        llm_line = "LLM：**Mock**（确定性正则扫描器，不产生任何模型调用，成本恒为 $0）"
        blurb = (
            "**这份报告量的是聚合层**（聚类、去重、置信度闸、冲突消解），"
            "不是模型的审查能力 —— 这一层里根本没有模型。"
            "干净组的误报率主要由扫描器一次只看一行的粗糙程度决定，"
            "**不能当作系统的精确率引用**。"
            "`rebuilt` 组基本认不出来也属于同一件事：那些是真代码，不是为正则准备的。"
        )
        command = "python tasks.py eval"
    else:
        layer = f"真实层 · {provider}"
        llm_line = f"LLM：**{provider}**（真实调用）· 花费 ${metrics.cost_usd:.4f} / 上限 ${budget:.2f}" + (
            "　⚠️ **因超预算提前停止**" if truncated else ""
        )
        blurb = (
            "**这份报告量的是模型在真实调用下的审查能力**，同时也覆盖了聚合层。"
            "数字会随模型版本、温度、以及 provider 侧的改动漂移 —— "
            "所以它比离线层更接近真实，但**不可复现**，这是它的固有代价。"
        )
        command = "python tasks.py eval --real"

    body = render_report(
        metrics,
        eval_runs,
        title=f"sfly 评测报告（{layer}）· {sha}",
        meta=[
            f"代码版本：`{sha}`"
            + (
                "（**生成时工作区有未提交改动**，这份数字不一定精确对应上面那个 commit）"
                if dirty
                else "（生成时工作区干净，数字精确对应这个 commit）"
            ),
            f"用例：{metrics.runs} 个（{_group_line(eval_runs)}）"
            + (f"　⚠️ 计划 {len(load_cases())} 个，**超预算提前停止**" if truncated else ""),
            llm_line,
            f"命令：`{command}`",
            "",
            blurb,
        ],
        sweep=sweep,
        current_threshold=SUPPRESS_THRESHOLD,
        by_group=score_by_group(eval_runs),
    )

    REPORTS_DIR.mkdir(exist_ok=True)
    path = REPORTS_DIR / f"eval-{sha}.md"
    path.write_text(body, encoding="utf-8", newline="")

    print(f"\n报告写入 {path.relative_to(REPO_ROOT)}")
    print(
        f"  严格精确率 {metrics.strict.precision:.1%} / 召回率 {metrics.strict.recall:.1%}"
        f" · 干净组每条 {metrics.false_positive_rate:.2f}"
        f" · 被闸砍掉的真阳性 {metrics.suppressed_hits}"
    )


def _group_line(runs: list[object]) -> str:
    counts: dict[str, int] = {}
    for run in runs:
        case: EvalCase = run.case  # type: ignore[attr-defined]
        counts[case.group] = counts.get(case.group, 0) + 1
    return "，".join(f"{name} {counts[name]}" for name in ("rebuilt", "injected", "clean") if name in counts)


def test_the_manifest_files_parse_as_yaml() -> None:
    """yaml 本身要能解析 —— ``load_cases`` 已经会抛，但报错太靠后。"""
    for path in sorted(CASES_DIR.glob("*.yaml")):
        assert yaml.safe_load(path.read_text(encoding="utf-8")) is not None
