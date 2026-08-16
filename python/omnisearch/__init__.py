"""OmniSearch Python 后端包。

分层（architecture.md §14）：common/（唯一共享层）/ server/（FastAPI）/ worker/（AI Worker）。
依赖方向：server → common ✅、worker → common ✅；禁止 worker → server.repository、server → worker.pipeline。
"""
from __future__ import annotations

import os

# protobuf 兼容：paddle 2.6.2 约束 protobuf≤3.20.2，而 torch/transformers/onnxruntime 的
# 生成代码要求 ≥4.x —— 官方建议方案 2：纯 Python 解析（M4 实测踩坑，ADR 级决策）。
# 必须在任何 protobuf 生成代码 import 之前设置（包根导入即生效）。
os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")
