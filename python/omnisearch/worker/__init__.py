"""worker —— AI Worker 独立 Python 进程（architecture.md §10）。

依赖方向：worker → common 允许；禁止 worker → server.repository。
"""
