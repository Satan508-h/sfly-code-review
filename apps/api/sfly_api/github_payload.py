"""GitHub webhook 载荷 → ``BootstrapMessage``。**纯函数，不碰网络、不碰数据库。**

### 载荷里没有补丁，这是个必须说清楚的地方

GitHub 的 ``pull_request`` webhook 只带 PR 的元数据（标题、作者、head_sha、
仓库 id），**不带任何代码**。要拿到补丁得再调一次
``GET /repos/{owner}/{repo}/pulls/{n}/files`` —— 那是 M7 的 GitHub 客户端。

所以这里约定：载荷里可以带一个 ``files`` 字段，内容是**那一次 API 调用的录制结果**
（GitHub 的原始响应形状：``filename`` / ``status`` / ``patch`` / ``additions``...）。
``scripts/replay_webhook.py`` 回放的就是这种录制载荷，而 M7 之后真实客户端
把 API 响应喂给**同一个转换函数** —— 两条路径共用一个 ``patches_from_files``，
不会出现「回放时能审、真上线审不了」这种分叉。

### 补丁要**重新拼回一份 git diff** 再解析

``/pulls/{n}/files`` 的 ``patch`` 字段是 **hunk 片段**，不含
``diff --git`` / ``---`` / ``+++`` 三行头。而整个项目计算
「哪些行是变更行」（inline 评论只能锚定在这些行上，见 ``FilePatch`` 的文档）
用的是同一个解析器 ``parse_unified_diff``，它**需要那三行**：
少了文件头，Mock LLM 的 ``iter_added_lines`` 会**静默返回空** ——
表现是「审完了，0 条发现」，没有任何报错。

所以这里干的事是：把 API 的每条记录**还原成一段标准 git diff**，再交给
唯一的解析器。刻意不自己数新增行：数行号的规则（hunk 头声明的行数、
新旧两侧要分开计数）已经在那边踩过坑了，抄一遍就是把那些坑再踩一遍。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import ValidationError

from sfly_shared.contracts import BootstrapMessage, idempotency_key_for

# 文件 → 补丁的转换住在 shared：它有两个消费者（网关读录制载荷、编排器读
# 自己从 GitHub 拉回来的响应），而它们**必须用同一份实现** —— 分叉的后果是
# 「回放时能审、真上线审不了」。见那个模块的文档。
from sfly_shared.github_files import patches_from_files as patches_from_files

#: 触发审查的 PR 动作。
#:
#: * ``opened`` / ``reopened`` —— 新开的、重新打开的
#: * ``synchronize`` —— **往 PR 上推了新提交**，这是最主要的一种
#: * ``ready_for_review`` —— 草稿转正式
#:
#: 其余动作（``closed`` / ``labeled`` / ``assigned`` / ``edited``...）都不该
#: 触发一次审查：它们不改变代码，而每次审查都是要花钱的。
REVIEW_ACTIONS: frozenset[str] = frozenset({"opened", "synchronize", "reopened", "ready_for_review"})

#: 我们唯一处理的事件类型。
PULL_REQUEST_EVENT = "pull_request"


class PayloadError(ValueError):
    """载荷不合法。**调用方应该回 400 并把消息原样带上** ——

    这条消息是排查「GitHub 说投递成功了，但我们说什么也没收到」的唯一线索，
    所以它是写给运维看的：说清楚缺的是哪个字段，而不是「invalid payload」。
    """


# --------------------------------------------------------------------------- #
# 取值
# --------------------------------------------------------------------------- #


def _dig(payload: Mapping[str, Any], *path: str) -> Any:
    """按路径取嵌套字段。中间任何一层不是对象就返回 ``None``。"""
    cur: Any = payload
    for key in path:
        if not isinstance(cur, Mapping):
            return None
        cur = cur.get(key)
    return cur


def _text(payload: Mapping[str, Any], *path: str) -> str:
    value = _dig(payload, *path)
    return str(value).strip() if value is not None else ""


def _require_text(payload: Mapping[str, Any], *path: str) -> str:
    value = _text(payload, *path)
    if not value:
        raise PayloadError(f"载荷里缺少 {'.'.join(path)}")
    return value


# --------------------------------------------------------------------------- #
# 载荷 → bootstrap
# --------------------------------------------------------------------------- #


def delivery_context(payload: Mapping[str, Any]) -> tuple[str, int | None]:
    """账本要的最小上下文：``(repo_id, pr_number)``。

    **刻意不做任何校验。** 这条投递可能马上就要被拒（动作不关心、载荷坏了），
    而「被拒的那条投递是哪个 PR 的」正是排查时第一个想知道的事 ——
    如果校验不过就不记，账本里只会剩下一堆没有归属的行。
    """
    pr_number = _dig(payload, "pull_request", "number")
    if pr_number is None:
        pr_number = _dig(payload, "number")
    return _text(payload, "repository", "full_name"), pr_number if isinstance(pr_number, int) else None


def bootstrap_from_payload(
    payload: Mapping[str, Any],
    *,
    event: str,
    task_id: str,
    max_patch_chars: int,
) -> tuple[BootstrapMessage | None, str]:
    """构造一条 bootstrap。**返回 ``(None, 原因)`` 表示这次投递不触发审查。**

    三种结局，调用方要区分开（它们的 HTTP 状态码和账本状态都不同）：

    * ``(msg, "")``        —— 正常，往下走
    * ``(None, 原因)``     —— 事件类型/动作不关心。**这不是错误**，
      GitHub 的 Recent Deliveries 里必须显示 200，否则每次 push 到别的分支都会
      在那边留一个红色的 ✗，而人就会开始忽略那个页面
    * 抛 ``PayloadError``  —— 载荷坏了。这是我们自己接线接错，要说出来
    """
    if event != PULL_REQUEST_EVENT:
        # ``ping`` 也走这里：GitHub 在 webhook 刚建好时发一次 ping，
        # 回 200 + 一句说明，配置页才会显示绿灯。
        return None, f"事件类型 {event or '(缺失)'} 不触发审查"

    action = _text(payload, "action")
    if action not in REVIEW_ACTIONS:
        return None, f"动作 {action or '(缺失)'} 不触发审查"

    if _dig(payload, "pull_request", "draft") is True:
        # 草稿 PR 是「还没写完」。等它转正式时 GitHub 会再发一次
        # ``ready_for_review``，那一次才审 —— 提前审既浪费钱，
        # 又会在作者还没准备好时留下评论。
        return None, "草稿 PR，等 ready_for_review"

    repo_id = _require_text(payload, "repository", "full_name")
    head_sha = _require_text(payload, "pull_request", "head", "sha")
    pr_number = _dig(payload, "pull_request", "number")
    if pr_number is None:
        pr_number = _dig(payload, "number")
    if not isinstance(pr_number, int):
        raise PayloadError("载荷里缺少 pull_request.number")

    files = payload.get("files") or []
    if not isinstance(files, Sequence) or isinstance(files, (str, bytes)):
        raise PayloadError("files 字段必须是数组（内容是 /pulls/{n}/files 的响应）")
    patches, _skipped = patches_from_files(files, max_patch_chars=max_patch_chars)

    installation_id = _dig(payload, "installation", "id")

    try:
        msg = BootstrapMessage(
            task_id=task_id,
            # 幂等键由三元组算出来，**不接受载荷里的值** ——
            # 用上游给的值等于把「同一个提交只审一次」这件事交给上游。
            idempotency_key=idempotency_key_for(repo_id, pr_number, head_sha),
            repo_id=repo_id,
            repo_node_id=_text(payload, "repository", "node_id"),
            pr_number=pr_number,
            head_sha=head_sha,
            # base_sha 允许缺失（fork 的 PR、被删掉的目标分支）——
            # 它只用于展示，不是幂等键的一部分。
            base_sha=_text(payload, "pull_request", "base", "sha"),
            installation_id=int(installation_id) if isinstance(installation_id, int) else None,
            file_patches=patches,
            pr_title=_text(payload, "pull_request", "title"),
            pr_author=_text(payload, "pull_request", "user", "login"),
        )
    except ValidationError as exc:
        # 契约层校验失败（head_sha 为空、键算错）在这里转成 400。
        # 不转的话它会变成 500 —— 而 500 会被 GitHub 读成「服务器故障，
        # 稍后重投」，于是这条注定失败的载荷会被反复投递。
        raise PayloadError(f"构造 bootstrap 失败：{exc}") from exc

    return msg, ""
