"""两种实现共用的契约测试 —— 被 tests/unit 和 tests/integration 同时导入。

``__init__.py`` 是为了让导入路径是 ``contracts.queue_contract``：两个测试层
在不同目录下，而 pytest 只把**测试文件自己所在的目录**塞进 ``sys.path``，
所以这些共享模块必须挂在 ``tests/`` 这个共同的父目录上。
"""
