"""webhook 的 HMAC 签名 —— **签发与校验只此一份**。

``scripts/replay_webhook.py`` 从这里 import ``sign``：回放脚本必须和服务器
按逐字节相同的规则签名，而「两边各写一遍 HMAC」是那种会静默分叉的东西
（一个用 hexdigest、一个用 base64，看起来都对，直到某天对上真实 GitHub）。

    sig = "sha256=" + HMAC_SHA256(secret, raw_body).hexdigest()

### 为什么必须对**原始字节**验签

这是这个文件唯一值得反复强调的地方：

* **不能对 ``json.dumps(payload)`` 验签。** 重新序列化会改变键顺序、空白、
  以及非 ASCII 的转义方式，于是 HMAC 一定对不上 —— 而症状是「签名校验失败」，
  看起来像密钥配错了。真凶是验签发生在解析之后。
* 所以路由里必须先 ``await request.body()`` 拿到字节，再解析。
  FastAPI 会缓存 body，读两次不会出问题（也不会重复消费流）。
* 摘要必须用 ``hmac.compare_digest`` 比，不用 ``==``：字符串比较会在第一个
  不同的字节上返回，理论上可以按字节爆破出正确的签名。

### 为什么算的是签名而不是「有没有带签名」

``verify`` 在头部格式不对（缺 ``sha256=`` 前缀、长度不对）时直接返回 False，
不尝试兼容 ``sha1``。GitHub 从 2021 年起同时发 ``X-Hub-Signature``（sha1）
和 ``X-Hub-Signature-256``，而 sha1 已经不安全了 —— 支持它等于把强度降到
最弱的那一个。
"""

from __future__ import annotations

import hashlib
import hmac

#: GitHub 发的签名头。sha256 版本。
SIGNATURE_HEADER = "X-Hub-Signature-256"

#: 投递 id 头。GitHub 为每一次投递生成一个 GUID，**重投时这个值不变** ——
#: 去重就靠它（见 ``migrations/002_webhook_deliveries.sql``）。
DELIVERY_HEADER = "X-GitHub-Delivery"

#: 事件类型头（``pull_request`` / ``ping`` / ``push``...）。
EVENT_HEADER = "X-GitHub-Event"

_PREFIX = "sha256="


def sign(body: bytes, secret: str) -> str:
    """签名。返回 **带前缀** 的完整头部值（``sha256=...``）。

    直接返回可用的头部值而不是裸摘要：``hexdigest`` 与 ``sha256=hexdigest``
    混用是这里最容易犯的错，而它的症状是「密钥明明一样却验不过」。
    """
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"{_PREFIX}{digest}"


def verify(body: bytes, header: str | None, secret: str) -> bool:
    """校验签名。**任何异常路径都返回 False**，不抛错。"""
    if not header or not header.startswith(_PREFIX):
        return False
    expected = sign(body, secret)
    # compare_digest 对 str 要求 ASCII-only —— 签名是十六进制，满足；
    # 但只要 header 里混进一个非 ASCII 字符它就会抛 TypeError，所以这里
    # 先做一次编码对齐，把「输入畸形」也归到「验签失败」里去。
    try:
        return hmac.compare_digest(expected, header)
    except TypeError:  # pragma: no cover —— 非 ASCII 的畸形头部
        return False
