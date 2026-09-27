"""``deploy-hf`` 的上传清单 —— **这是一条安全边界，不是过滤优化**。

Space 是一个**公开**仓库。``.env`` 里躺着这个项目全部的凭据（DeepSeek key、
GitHub token、webhook 密钥、Neon 连接串），而 ``huggingface_hub.upload_folder``
**不读 ``.gitignore``**（它的文档明说了），``.gitignore`` 也不在它默认排除的
东西里 —— 所以「哪些文件不该传」只能由我们显式列出来。

列漏一个的后果不是「多传了个文件」：是把密钥推到公网上，而**推上去就撤不回**
（fork、缓存、爬虫都当它已经公开了）。

所以这个文件盯的不是「过滤得好不好」，而是「有没有把不能传的东西传出去」。
反面同样要测：清单里必须有 ``Dockerfile`` 和 ``README.md`` —— 少了任何一个
Space 都建不起来，而那属于「上传成功、构建失败」，比传错文件更难查。

`tasks.py` 在仓库根目录（不是 workspace 成员），pytest 不会自动把它放进
sys.path，所以下面手动加一次。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tasks import _assert_sane_upload, _is_secret_path, _upload_files

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("path", "secret"),
    [
        (".env", True),
        (".env.local", True),
        ("apps/api/.env", True),
        ("deploy/server.pem", True),
        ("keys/private.key", True),
        # **模板不是密钥。** 它值都是空的，而且本来就该公开（README 里让人
        # 照着它建 .env）。这两者在磁盘上只差一个后缀，判错的方向是把真密钥
        # 传出去，所以两边都要钉住。
        (".env.example", False),
        ("src/config.py", False),
        ("README.md", False),
        ("web/env.example.ts", False),
    ],
)
def test_the_secret_predicate_is_deliberately_paranoid(path: str, secret: bool) -> None:
    assert _is_secret_path(path) is secret


def test_the_real_repo_list_contains_no_credentials() -> None:
    """拿**当前工作区的真实文件列表**测一遍。

    这不是「测一个纯函数」，而是「上传之前那道闸在真数据上会不会放行」——
    排除规则写坏时唯一能救你的就是它，所以它必须在真实清单上验过。
    """
    files = _upload_files()

    leaked = [p for p in files if _is_secret_path(p)]
    assert leaked == [], f"上传清单里有凭据：{leaked[:5]}"


def test_the_real_repo_list_excludes_what_the_platform_rebuilds() -> None:
    """`.venv` / `node_modules` 这些必须不在清单里。

    它们不是「传了会泄露」，是「传了几万个文件、每次部署几分钟」——
    而更阴的一面：它们**看起来**传成功了，只是 Space 构建时用的是容器里
    重新装的那一份，所以问题不会当场暴露。
    """
    files = _upload_files()

    for needle in (".venv/", "node_modules/", "__pycache__/"):
        assert not [p for p in files if needle in p], f"清单里混进了 {needle}"


def test_the_real_repo_list_keeps_what_the_space_needs() -> None:
    """反面：过滤不能把构建需要的东西也滤掉。

    ``Dockerfile`` 决定怎么跑，``README.md`` 的 frontmatter 决定它是个
    Docker Space、以及监听哪个端口 —— 两个都不能少。
    """
    files = _upload_files()

    assert "Dockerfile" in files
    assert "README.md" in files
    assert "uv.lock" in files, "锁文件不在的话镜像里装的是另一套依赖"


def test_a_leaked_secret_refuses_to_upload() -> None:
    """清单里出现凭据时**必须拒传**，而不是打个警告。

    警告会被刷过去，而这一步之后没有撤销操作。
    """
    with pytest.raises(SystemExit):
        _assert_sane_upload([".env", "Dockerfile", "README.md"])


def test_a_missing_dockerfile_refuses_to_upload() -> None:
    """清单里没有 Dockerfile 时也拒传 —— 上传成功了 Space 也建不起来。"""
    with pytest.raises(SystemExit):
        _assert_sane_upload(["README.md", "pyproject.toml"])


def test_a_missing_readme_refuses_to_upload() -> None:
    """没有 README 的话 Space 连「我是个 Docker Space、听哪个端口」都不知道 ——
    它会被当成别的东西，而构建仍然是成功的。"""
    with pytest.raises(SystemExit):
        _assert_sane_upload(["Dockerfile", "pyproject.toml"])
