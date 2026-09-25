"""控制台编码 —— **两台机器共用一份代码时的经典问题，而且两个方向都会错**。

* 输出到**管道或文件**（``> out.json``、``| jq``）时必须是 UTF-8。
  Windows 上 Python 默认用系统编码（中文系统是 cp936），于是重定向出来的
  json 文件不是合法 UTF-8 —— 本地看着正常，别的工具一读就乱码。
  更糟的是报告正文里有 emoji（``🤖``），cp936 编码不了，直接抛
  ``UnicodeEncodeError`` —— **整个命令以非零码退出，而报告已经生成好了**。
* 输出到**控制台**时保留控制台自己的编码。强行改成 UTF-8 会让中文
  在 cp936 终端里变成一堆问号，也就是把一个能看的结果变成一个不能看的。

所以判据是 ``isatty()`` 而不是平台：Windows 上重定向到文件时也要 UTF-8，
Linux 上接终端时也要跟着终端走。``errors="replace"`` 两侧都要加 ——
diff 和 LLM 的输出里什么字符都可能有，输出少一个字符远比整条命令失败好。

### 为什么在 shared 里而不是各自的 ``__main__``

原本它住在 ``sfly_workers/__main__.py``（M1 写的）。M5 的编排器 CLI 也要打
中文报告和 emoji —— 而**第二份拷贝一定会漏掉一侧**：那个 ``isatty()`` 分支
看起来像多余的谨慎，抄的人很容易只抄 ``encoding="utf-8"`` 那一半，
然后在某个 Linux 终端上把中文变成问号。一份，两个入口用。
"""

from __future__ import annotations

import sys


def configure_streams() -> None:
    """按「输出到哪儿」决定 stdout / stderr 的编码。见模块文档。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        if stream.isatty():
            reconfigure(errors="replace")
        else:
            reconfigure(encoding="utf-8", errors="replace")
