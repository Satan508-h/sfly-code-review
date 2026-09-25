#!/usr/bin/env python3
"""跨平台任务入口 —— `python tasks.py <命令>`

**为什么不用 Makefile 当主入口**：Windows 默认没有 make，而这个项目的开发
机器就是 Windows。Makefile 保留了一个瘦壳（`make up` 仍可用），实际逻辑
全在这里，零第三方依赖，到处都能跑。

    python tasks.py up          一键起全部服务并等健康检查通过
    python tasks.py test        全量单测（不需要 Docker、不需要密钥）
    python tasks.py demo        端到端演示：投递 3 次 webhook
    python tasks.py scale 3     把 worker-security 扩到 3 个副本
    python tasks.py kill-worker 杀掉一个 Worker，验证容错
"""

from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

# --- Windows 控制台编码 --------------------------------------------------- #
#
# 背景：Windows 控制台默认代码页是 GBK/CP936，而 Python 在**真实控制台**上
# 走 WriteConsoleW，中文能正常显示；但当输出被重定向或接到管道（git-bash、
# CI、`| grep`）时，会退化成按 GBK 编码字节流。
#
# GBK 装不下 ✓ / ✗ / ▸ 这类符号，编码失败抛在 print() 里 ——
# 命令实际已经执行完了才崩，看起来像是执行失败，非常误导。
#
# 所以本文件的提示标记一律用 ASCII（[OK] / [!!] / >>），下面再加一道兜底：
# 任何意外字符退化成 "?" 而不是中断整个命令。
#
# 注意这里**不把编码改成 UTF-8**：那会让真实 Windows 控制台（cmd / PowerShell /
# Windows Terminal，代码页 936）里的中文变成乱码，为了修少数场景而破坏主场景。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        with contextlib.suppress(ValueError, OSError):
            _stream.reconfigure(errors="replace")

ROOT = Path(__file__).resolve().parent
VENV_BIN = ROOT / ".venv" / ("Scripts" if os.name == "nt" else "bin")

IS_WINDOWS = os.name == "nt"


# --------------------------------------------------------------------------- #
# 基础设施
# --------------------------------------------------------------------------- #


def _which(name: str) -> str | None:
    return shutil.which(name)


def run(
    cmd: list[str],
    *,
    check: bool = True,
    capture: bool = False,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """跑一条命令。失败时把命令原文打出来 —— 否则用户只看到 'exit 1' 无从下手。"""
    printable = " ".join(cmd)
    if not capture:
        print(f"\n\033[36m$\033[0m {printable}", flush=True)
    try:
        # 命令列表全部由本文件的开发者硬编码，不含用户输入；参数（--times /
        # --port）是 argparse 校验过的 int，不做字符串插值，所以没有注入面。
        # 唯一开 shell 的情况是 Windows 上的 npm.cmd。
        return subprocess.run(  # noqa: S603
            cmd,
            cwd=cwd or ROOT,
            check=check,
            text=True,
            capture_output=capture,
            # npm 在 Windows 上是 npm.cmd，没有 shell 会报 FileNotFoundError
            shell=IS_WINDOWS and cmd[0] in {"npm", "npx"},
        )
    except FileNotFoundError:
        _die(f"找不到可执行文件：{cmd[0]}\n  请确认它已安装并在 PATH 里。")
    except subprocess.CalledProcessError as exc:
        if capture and exc.stdout:
            print(exc.stdout)
        _die(f"命令失败（exit {exc.returncode}）：{printable}")


def _die(msg: str) -> None:
    print(f"\n\033[31m[!!]\033[0m {msg}\n", file=sys.stderr)
    raise SystemExit(1)


def _ok(msg: str) -> None:
    print(f"\033[32m[OK]\033[0m {msg}")


def _step(msg: str) -> None:
    print(f"\n\033[1m>> {msg}\033[0m")


def _uv() -> str:
    """优先用虚拟环境里的 uv，其次是 PATH 上的。"""
    local = VENV_BIN / ("uv.exe" if IS_WINDOWS else "uv")
    if local.exists():
        return str(local)
    if found := _which("uv"):
        return found
    _die("找不到 uv。安装：pip install uv")


def _py() -> str:
    """虚拟环境里的 python，没有就用当前解释器。"""
    p = VENV_BIN / ("python.exe" if IS_WINDOWS else "python")
    return str(p) if p.exists() else sys.executable


def _ensure_env_file() -> None:
    """.env 不存在就从模板复制。compose 里声明了 required:false，
    但没有 .env 时用户改配置会找不到地方下手。"""
    env, example = ROOT / ".env", ROOT / ".env.example"
    if not env.exists() and example.exists():
        shutil.copyfile(example, env)
        _ok("已从 .env.example 创建 .env（默认零密钥可跑通）")


def _compose(*args: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess[str]:
    """包一层 docker compose。``capture`` 用于需要读取输出的场景（如取容器 id）。

    注意要把关键字参数**显式转发**给 run()。漏掉一个的话，
    调用点会在很晚的地方抛 TypeError（比如取容器 id 时），
    而报错位置和真正的原因（这行少了个参数）隔得很远。
    """
    if not _which("docker"):
        _die("找不到 docker。")
    return run(["docker", "compose", *args], check=check, capture=capture)


# --------------------------------------------------------------------------- #
# 命令实现
# --------------------------------------------------------------------------- #


def cmd_env(_: argparse.Namespace) -> None:
    _ensure_env_file()
    _ok(f".env 就绪：{ROOT / '.env'}")


def cmd_lock(_: argparse.Namespace) -> None:
    """重新解析依赖并写 uv.lock。改了任何 pyproject 之后都要跑。"""
    run([_uv(), "lock"])
    _ok("uv.lock 已更新 —— 记得提交它，Docker 构建用 --frozen 依赖它")


def cmd_sync(_: argparse.Namespace) -> None:
    run([_uv(), "sync", "--all-packages"])
    _ok("依赖已同步到 .venv")


def cmd_build(args: argparse.Namespace) -> None:
    extra = ["--no-cache"] if args.no_cache else []
    _compose("build", *extra)
    _ok("镜像构建完成")


def cmd_up(args: argparse.Namespace) -> None:
    _ensure_env_file()
    _step("构建并启动全部服务")
    # --wait 会阻塞到所有 healthcheck 变绿（或超时失败），
    # 这正是「一键启动」该有的语义：命令返回即代表真的可用。
    _compose("up", "--build", "-d", "--wait", timeout=args.timeout)
    _ok("全部服务健康")
    _print_endpoints()


def cmd_down(_: argparse.Namespace) -> None:
    _compose("down")
    _ok("服务已停止（数据卷保留）")


def cmd_clean(_: argparse.Namespace) -> None:
    """连数据卷一起删 —— 改过 infra/postgres/init.sql 之后必须这么做。"""
    _compose("down", "-v", "--remove-orphans")
    _ok("服务与数据卷已清空")


def cmd_restart(args: argparse.Namespace) -> None:
    service = args.service or ""
    _compose("restart", service) if service else _compose("restart")


def cmd_ps(_: argparse.Namespace) -> None:
    _compose("ps")


def cmd_logs(args: argparse.Namespace) -> None:
    tail = ["--tail", str(args.tail), "-f"] if args.follow else ["--tail", str(args.tail)]
    _compose("logs", *tail, *(args.services or []))


def cmd_scale(args: argparse.Namespace) -> None:
    """演示水平扩展：docker compose up --scale worker-security=N"""
    n = args.replicas
    _step(f"把 worker-security 扩到 {n} 个副本")
    _compose("up", "-d", "--scale", f"worker-security={n}", "--wait")
    _ok(f"worker-security 现在有 {n} 个副本")
    _compose("ps", "worker-security")


def cmd_kill_worker(_: argparse.Namespace) -> None:
    """容错演示：杀掉一个正在跑的 Worker，run 仍应跑完并显示降级徽章。

    这是全项目最有说服力的一个演示 —— 它同时证明了 XAUTOCLAIM 回收、
    结果 UPSERT 幂等、以及「失败也是结果」三条机制。
    """
    ps = _compose("ps", "-q", "worker-security", capture=True)
    ids = [x for x in ps.stdout.split() if x.strip()]
    if not ids:
        _die("没有在跑的 worker-security 容器。先 `python tasks.py up`。")
    target = ids[0]
    _step(f"杀死 worker-security 容器 {target[:12]}")
    run(["docker", "kill", target])
    _ok("已杀死。观察日志：同伴应在 CLAIM_IDLE_MS 后回收该任务，run 照样跑完")


#: 依赖状态三态在终端里的记号。ASCII，因为 Windows 控制台的 GBK 代码页
#: 装不下 ● ○ 这类字符（见文件头的编码说明）。
_CHECK_MARK = {"ok": "[OK]  ", "down": "[!!]  ", "skipped": "[--]  "}


def cmd_health(args: argparse.Namespace) -> None:
    """探测 ``/api/health`` —— **就绪**探针，会真的去连 Postgres 和 Redis。

    刻意不探 ``/healthz``：那个是存活探针，永远返回 200，用它做验收
    等于什么都没验。这里要看的是「这套部署现在能不能干活」。
    """
    port = args.port or _load_env().get("API_HOST_PORT", "8000")
    url = f"http://127.0.0.1:{port}/api/health"

    import json
    import urllib.error
    import urllib.request

    body: dict[str, Any] | None = None
    last_error = ""
    for attempt in range(1, 6):
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                body = json.loads(r.read().decode())
                break
        except urllib.error.HTTPError as exc:
            # 503 是**有内容的失败**：依赖挂了，但后端是活的，响应体里写着是哪个。
            # 把它和「连不上」区分开，否则用户会去查一个根本没坏的后端。
            with contextlib.suppress(Exception):
                body = json.loads(exc.read().decode())
            break
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = str(exc)
            print(f"  第 {attempt}/5 次探测失败：{exc}")
            time.sleep(2)
    else:
        _die(f"api 在 {url} 无响应。看日志：python tasks.py logs api")

    assert body is not None

    print(
        f"\n  {body.get('service', '?')} v{body.get('version', '?')}"
        f"   mode={body.get('mode', '?')}   已运行 {body.get('uptime_s', 0)}s\n"
    )
    for name, check in (body.get("checks") or {}).items():
        mark = _CHECK_MARK.get(check.get("status", ""), "      ")
        latency = f"{check['latency_ms']:>7.1f} ms" if check.get("status") == "ok" else " " * 10
        print(f"  {mark}{name:<10}{latency}  {check.get('detail', '')}")

    if not body.get("ok", False):
        failed = [n for n, c in (body.get("checks") or {}).items() if c.get("status") == "down"]
        _die(
            f"依赖不可用：{', '.join(failed) or '未知'}\n"
            "  看容器状态：python tasks.py ps\n"
            "  看日志：    python tasks.py logs postgres redis"
        )
    _ok(f"全部依赖就绪：{url}")
    if last_error:
        print(f"  （前几次探测失败过：{last_error}）")


# -- 测试 ------------------------------------------------------------------- #


def _pytest(marker: str | None = None, extra: list[str] | None = None) -> None:
    # addopts 里已经默认排除 integration/e2e，所以跑集成测试要显式覆盖
    cmd = [_py(), "-m", "pytest"]
    if marker:
        cmd += ["-m", marker, "-o", "addopts=-q --strict-markers"]
    cmd += extra or []
    run(cmd)


def cmd_test(_: argparse.Namespace) -> None:
    """默认只跑单测：不需要 Docker、不需要密钥，应该 10 秒内结束。"""
    _step("单测（无 Docker、无密钥）")
    _pytest()


def cmd_test_int(_: argparse.Namespace) -> None:
    _step("集成测试（需要 Docker，用 Mock LLM）")
    _pytest("integration")


def cmd_test_e2e(_: argparse.Namespace) -> None:
    """真实 DeepSeek + 真实 GitHub。**要花钱**，只在演示前手动跑。"""
    if not os.environ.get("LLM_API_KEY"):
        _die("e2e 测试需要 LLM_API_KEY。这是刻意的门槛 —— 它会产生真实费用。")
    _step("端到端测试（真实密钥，有花费上限 $2）")
    _pytest("e2e")


def cmd_eval(_: argparse.Namespace) -> None:
    _step("评测集：精确率 / 召回率 / 误报率 / 成本 / 消融实验")
    run([_py(), "-m", "pytest", "-m", "eval", "-o", "addopts=-q", "-s"])


# -- 质量 ------------------------------------------------------------------- #


def cmd_lint(_: argparse.Namespace) -> None:
    run([_uv(), "run", "ruff", "check", "."])
    run([_uv(), "run", "ruff", "format", "--check", "."])
    _ok("lint 通过")


def cmd_fmt(_: argparse.Namespace) -> None:
    run([_uv(), "run", "ruff", "check", "--fix", "."])
    run([_uv(), "run", "ruff", "format", "."])
    _ok("格式化完成")


def cmd_typecheck(_: argparse.Namespace) -> None:
    run([_uv(), "run", "mypy", "packages", "apps"])


# -- 演示 ------------------------------------------------------------------- #


def cmd_demo(args: argparse.Namespace) -> None:
    """端到端演示。M6 会实现 scripts/replay_webhook.py。"""
    script = ROOT / "scripts" / "replay_webhook.py"
    if not script.exists():
        _die(
            "scripts/replay_webhook.py 还不存在 —— 它是 M6 的交付物。\n"
            "  Step 0 阶段请先验证：python tasks.py health"
        )
    run([_py(), str(script), "--times", str(args.times)])


def cmd_web_dev(_: argparse.Namespace) -> None:
    """本地起 Vite 开发服务器（热更新），对接 Docker 里的 api。

    Vite 的 proxy 会把 /api 转到 localhost:8000，所以前端代码里
    始终用相对路径 /api，不用管后端在哪。
    """
    web = ROOT / "web"
    if not _which("npm"):
        _die("找不到 npm。")
    if not (web / "node_modules").exists():
        _step("首次运行，先装前端依赖")
        run(["npm", "install"], cwd=web)
    run(["npm", "run", "dev"], cwd=web, check=False)


def cmd_web_install(_: argparse.Namespace) -> None:
    if not _which("npm"):
        _die("找不到 npm。")
    run(["npm", "install"], cwd=ROOT / "web")


def _print_endpoints() -> None:
    """打印访问地址。端口从 .env 读 —— 这台机器上 5432/6379 常被别的项目占用，
    写死的话提示出来的地址是错的，比不提示更误导。"""
    s = _load_env()
    api = s.get("API_HOST_PORT", "8000")
    web = s.get("WEB_HOST_PORT", "5173")
    pg = s.get("POSTGRES_HOST_PORT", "5432")
    rd = s.get("REDIS_HOST_PORT", "6379")

    note = ""
    if (pg, rd) != ("5432", "6379"):
        note = "\n  （宿主端口已改，因为默认端口被本机其他服务占用；容器内仍是 5432/6379）"

    print(
        f"""
  前端       http://localhost:{web}
  API 文档   http://localhost:{api}/api/docs
  依赖状态   http://localhost:{api}/api/health   （或：python tasks.py health）
  存活探针   http://localhost:{api}/healthz      （不探依赖，永远 200）
  Postgres   localhost:{pg}  (sfly / sfly)
  Redis      localhost:{rd}{note}

  下一步：
    python tasks.py demo           端到端演示
    python tasks.py scale 3        水平扩展 Worker
    python tasks.py kill-worker    容错演示
"""
    )


def _load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    f = ROOT / ".env"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env


# --------------------------------------------------------------------------- #
# 命令注册
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tasks.py",
        description="sfly 项目任务入口",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = p.add_subparsers(dest="command", metavar="<命令>")

    def add(
        name: str,
        fn: Callable[[argparse.Namespace], None],
        help_: str,
        args: Sequence[tuple[tuple[str, ...], dict[str, Any]]] = (),
    ) -> None:
        """注册一个子命令。

        **不要写成 ``add(...).add_argument(...)`` 的链式调用。**
        argparse 的 ``add_argument`` 返回的是 Action 而不是 parser，
        所以第二个链式调用会变成 ``Action.add_argument``，报
        ``AttributeError: '_StoreAction' object has no attribute 'add_argument'``。
        更糟的是它在 ``build_parser()`` 里抛出，于是**整个 CLI 全部命令都不可用**，
        而报错信息指向的是最后那一行，和真正的问题位置差很远。
        参数用声明式列表传入可以从根上杜绝这类错误。
        """
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(func=fn)
        for flags, kwargs in args:
            sp.add_argument(*flags, **kwargs)

    add("env", cmd_env, "从 .env.example 创建 .env")
    add("lock", cmd_lock, "重新解析依赖并更新 uv.lock")
    add("sync", cmd_sync, "同步依赖到 .venv")

    add(
        "build",
        cmd_build,
        "构建全部镜像",
        [(("--no-cache",), {"action": "store_true", "help": "绕过构建缓存"})],
    )
    add(
        "up",
        cmd_up,
        "构建并启动全部服务，阻塞到健康检查通过",
        [(("--timeout",), {"type": int, "default": 600, "help": "等待健康的秒数（默认 600）"})],
    )
    add("down", cmd_down, "停止服务（保留数据卷）")
    add("clean", cmd_clean, "停止服务并删除数据卷")
    add("restart", cmd_restart, "重启服务", [(("service",), {"nargs": "?"})])
    add("ps", cmd_ps, "查看容器状态")
    add(
        "logs",
        cmd_logs,
        "跟踪日志",
        [
            (("services",), {"nargs": "*"}),
            (("-f", "--follow"), {"action": "store_true", "help": "持续跟踪"}),
            (("--tail",), {"type": int, "default": 100}),
        ],
    )
    add(
        "health",
        cmd_health,
        "探测 api 健康检查",
        [(("--port",), {"default": None, "help": "留空则读 .env 的 API_HOST_PORT"})],
    )

    add(
        "scale",
        cmd_scale,
        "水平扩展 worker-security",
        [(("replicas",), {"type": int, "nargs": "?", "default": 3})],
    )
    add("kill-worker", cmd_kill_worker, "容错演示：杀死一个 worker-security")

    add("test", cmd_test, "单测（默认，无需 Docker/密钥）")
    add("test-int", cmd_test_int, "集成测试（需要 Docker）")
    add("test-e2e", cmd_test_e2e, "端到端测试（需要真实密钥，会花钱）")
    add("eval", cmd_eval, "评测集：精确率/召回率/成本")

    add("lint", cmd_lint, "ruff 检查 + 格式校验")
    add("fmt", cmd_fmt, "自动格式化")
    add("typecheck", cmd_typecheck, "mypy 类型检查")

    add("demo", cmd_demo, "端到端演示", [(("--times",), {"type": int, "default": 1})])
    add("web-install", cmd_web_install, "安装前端依赖")
    add("web-dev", cmd_web_dev, "启动前端开发服务器")

    return p


def main() -> None:
    parser = build_parser()
    if len(sys.argv) == 1:
        parser.print_help()
        raise SystemExit(0)
    args = parser.parse_args()
    try:
        args.func(args)
    except KeyboardInterrupt:
        print("\n中断。")
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
