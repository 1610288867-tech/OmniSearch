"""database —— Database 层（迁移基础设施）。

连接类位于 common/database.py（server 与 worker 共享，见 common/database.py 的背景说明）；
本包仅保留版本化迁移（server 启动时执行，worker 不执行）。
"""
