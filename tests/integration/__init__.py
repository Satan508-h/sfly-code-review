"""``__init__.py`` 在这里**只有一个作用**：让 mypy 能区分两个同名的 ``conftest``。

仓库里有两个 ``conftest.py``（``tests/`` 和 ``tests/integration/``）。没有
``__init__.py`` 时，mypy 按「往上找到第一个没有 ``__init__.py`` 的目录」来算
模块名，于是两个文件都叫 ``conftest`` —— 它直接报
``Duplicate module named "conftest"`` 并**停止检查**（连后面的错误都看不见，
这一点比报错本身更麻烦）。

补上这一层之后，``tests/integration/conftest.py`` 的模块名变成
``integration.conftest``，与 ``tests/conftest.py`` 不再撞名。

对 pytest 没有影响：它给测试文件算模块名时，往上找的第一层
（``tests/integration/bus/``）就没有 ``__init__.py``，于是停在那儿 ——
``tests/integration/bus/*.py`` 仍然是各自独立的顶层模块。
"""
