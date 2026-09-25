"""ID 生成。

用 ULID 而不是 UUID4：ULID 前 48 位是毫秒时间戳，所以 ``ORDER BY task_id``
就是按时间排序，``review_runs`` 的列表查询和索引都不用额外的时间列。
同时它仍然有 80 位随机性，不会暴露业务量。
"""

from __future__ import annotations

import secrets

from ulid import ULID


def new_task_id() -> str:
    """一个新的 run 标识。26 字符的 Crockford base32。"""
    return str(ULID())


def new_id(prefix: str) -> str:
    """带前缀的短 ID，用于日志关联和临时资源命名。

    用 ``secrets`` 而不是 ``random`` —— 即使这些 id 出现在对外可见的地方
    （评论标记、stub 服务器路径），也不该是可预测的。
    """
    return f"{prefix}_{secrets.token_hex(6)}"
