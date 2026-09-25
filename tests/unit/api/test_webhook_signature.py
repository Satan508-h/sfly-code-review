"""webhook 签名 —— 纯函数，不需要任何依赖。

这个文件里最重要的是 ``test_a_reserialized_body_does_not_verify``：
它是「必须对原始字节验签」这条规则的**可执行形式**。没有它的话，
某个下午有人为了「顺便把载荷整理一下」在验签之前做了一次
``json.loads`` + ``json.dumps``，于是所有签名开始失败，
而症状是「密钥明明配对了却验不过」—— 排查方向会先跑偏到密钥上。
"""

from __future__ import annotations

import json

import pytest

from sfly_api.webhook import SIGNATURE_HEADER, sign, verify

pytestmark = pytest.mark.unit

SECRET = "s3cr3t-from-github"
BODY = b'{"action":"opened","number":42}'


def test_a_signature_round_trips() -> None:
    assert verify(BODY, sign(BODY, SECRET), SECRET) is True


def test_the_header_carries_the_algorithm_prefix() -> None:
    """``sha256=`` 前缀是 GitHub 的格式，不是装饰。

    少了它，GitHub 的载荷会被我们拒绝、我们的回放脚本会被真实 GitHub
    的校验拒绝 —— 而两边都不会说「你少了个前缀」。
    """
    assert sign(BODY, SECRET).startswith("sha256=")
    assert len(sign(BODY, SECRET)) == len("sha256=") + 64  # sha256 是 32 字节 = 64 个十六进制字符


def test_a_tampered_body_is_rejected() -> None:
    signature = sign(BODY, SECRET)
    assert verify(BODY + b" ", signature, SECRET) is False
    assert verify(b'{"action":"closed","number":42}', signature, SECRET) is False


def test_a_wrong_secret_is_rejected() -> None:
    assert verify(BODY, sign(BODY, "另一个密钥"), SECRET) is False


def test_a_missing_or_malformed_header_is_rejected() -> None:
    good = sign(BODY, SECRET)
    assert verify(BODY, None, SECRET) is False
    assert verify(BODY, "", SECRET) is False
    # 不带算法前缀 —— 早期 GitHub 发的是裸摘要，我们不能把它当成 sha256
    assert verify(BODY, good.removeprefix("sha256="), SECRET) is False
    # sha1 版本（GitHub 至今仍在发）。**刻意不支持** —— 支持它等于把强度
    # 降到最弱的那一个。
    assert verify(BODY, "sha1=da39a3ee5e6b4b0d3255bfef95601890afd80709", SECRET) is False


def test_a_non_ascii_header_is_rejected_rather_than_raising() -> None:
    """畸形头部必须走「验签失败」这条路，不能抛异常。

    抛出去的话，它会变成一个 500 —— 而 500 会被 GitHub 读成「服务器故障」，
    于是那条投递会被反复重投。
    """
    assert verify(BODY, "sha256=签名不对", SECRET) is False


def test_a_reserialized_body_does_not_verify() -> None:
    """**解析之后再序列化，签名一定对不上。**

    这正是「必须对原始字节验签」的原因，也是最容易犯的错：
    先 ``request.json()`` 拿到 dict、再 ``json.dumps`` 回去验签 ——
    键顺序、空白、非 ASCII 的转义方式都可能变，于是 HMAC 变了。
    表现是「密钥配对了但验不过」，排查会先跑偏到密钥上。
    """
    body = json.dumps({"b": 1, "a": "中"}, ensure_ascii=False).encode("utf-8")
    parsed = json.loads(body)
    # 三种「看起来一样」的重建方式，字节都不同
    reordered = json.dumps({"a": parsed["a"], "b": parsed["b"]}, ensure_ascii=False).encode("utf-8")
    indented = json.dumps(parsed, indent=2).encode("utf-8")
    ascii_escaped = json.dumps(parsed, ensure_ascii=True).encode("utf-8")

    assert verify(body, sign(body, SECRET), SECRET) is True
    # 注意：``json.dumps(json.loads(x))`` 在参数相同时**是**逐字节相同的
    # （dict 保序、分隔符一致）—— 所以这里不能用它当反例，
    # 它会让这条测试以为自己在验证一件其实没发生的事。
    for rebuilt in (reordered, indented, ascii_escaped):
        assert rebuilt != body
        assert verify(rebuilt, sign(body, SECRET), SECRET) is False


def test_the_header_constant_matches_github() -> None:
    """拼错这个头名的症状是「所有真实投递都验不过，而回放脚本一切正常」——
    因为我们自己的脚本用的也是同一个常量。所以它对不对只能靠这条断言钉住。
    """
    assert SIGNATURE_HEADER == "X-Hub-Signature-256"
