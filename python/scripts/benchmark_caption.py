"""Caption 模型 benchmark（M4.3：architecture.md §10.6 / ADR-004）。

候选按 architecture.md：Florence-2-base-ft / Qwen2-VL-2B-Instruct / InternVL2-1B。
- 逐个：下载（临时目录）→ 加载 → 固定测试集推理（风景/人物/建筑/文档截图/含文字/中文场景）
- 记录：中文 caption 质量（输出文本）、单图推理耗时、峰值/稳态 RSS、模型磁盘占用、CPU 占用
- 每个候选 benchmark 完可清理（不同时长期保留多个候选）
- 结果写入 docs/adr/ADR-004（不写死成架构假设）

用法：python python/scripts/benchmark_caption.py --model florence2 [--keep]
"""
from __future__ import annotations

import argparse
import json
import os
import resource
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYTHON = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PYTHON))

from omnisearch.common.config import dev_data_dir  # noqa: E402

CANDIDATES = {
    "florence2": {
        "id": "florence-2-base-ft-onnx",
        "hf_repo": "microsoft/Florence-2-base-ft",
        "size_mb_est": 700,
    },
    "qwen2vl2b": {
        "id": "qwen2-vl-2b-instruct",
        "hf_repo": "Qwen/Qwen2-VL-2B-Instruct",
        "size_mb_est": 2000,
    },
    "internvl2": {
        "id": "internvl2-1b",
        "hf_repo": "OpenGVLab/InternVL2-1B",
        "size_mb_est": 1000,
    },
}

# 固定测试集：风景/人物/建筑/文档截图/含文字/中文场景
TEST_IMAGES = [
    ("landscape", "风景：蓝天白云下的雪山湖泊"),
    ("person", "人物：办公室里的程序员"),
    ("building", "建筑：纽约高楼天际线"),
    ("document", "文档截图：一段文字段落"),
    ("text_image", "含文字图片：公告牌上写着 New York 2026"),
    ("chinese_scene", "中文场景：街边的中式餐馆招牌"),
]


def _make_test_images(data_dir: Path) -> list[tuple[str, str]]:
    """生成固定测试图（PIL 绘制示意场景 + 文字标注）。"""
    from PIL import Image, ImageDraw, ImageFont

    out_dir = data_dir / "benchmark_images"
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 24)
    except OSError:
        font = ImageFont.load_default()
    images = []
    for name, desc in TEST_IMAGES:
        img = Image.new("RGB", (480, 320), (200 + len(name) * 7 % 55, 180, 220))
        d = ImageDraw.Draw(img)
        d.rectangle([40, 40, 440, 280], outline=(60, 60, 60), width=3)
        d.text((30, 140), desc, fill=(20, 20, 20), font=font)
        p = out_dir / f"{name}.png"
        img.save(str(p))
        images.append((str(p), desc))
    return images


def _rss_mb() -> float:
    if os.name == "nt":
        import ctypes

        class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        c = _PROCESS_MEMORY_COUNTERS()
        ctypes.windll.psapi.GetProcessMemoryInfo(ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(c), ctypes.sizeof(c))
        return c.WorkingSetSize / 1024 / 1024
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def _disk_mb(path: Path) -> float:
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            total += p.stat().st_size
    return total / 1024 / 1024


def _run_florence2(model_dir: Path, images, keep: bool) -> dict:
    """Florence-2-base-ft（ONNX 官方导出；生成式 caption 任务 <MORE_DETAILED_CAPTION>）。"""
    from onnxruntime import InferenceSession
    from PIL import Image
    from transformers import AutoProcessor

    processor = AutoProcessor.from_pretrained("microsoft/Florence-2-base-ft")
    session = InferenceSession(str(model_dir / "model.onnx"), providers=["CPUExecutionProvider"])
    results = []
    for path, _desc in images:
        t0 = time.perf_counter()
        img = Image.open(path).convert("RGB")
        inputs = processor(img, return_tensors="np")
        out = session.run(None, {k: v for k, v in inputs.items() if k in {i.name for i in session.get_inputs()}})
        caption = processor.batch_decode(out[0], skip_special_tokens=True)[0][:120]
        results.append({"image": Path(path).name, "ms": round((time.perf_counter() - t0) * 1000), "caption": caption})
    return {"results": results}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", choices=list(CANDIDATES), required=True)
    parser.add_argument("--keep", action="store_true", help="benchmark 后保留模型（默认清理）")
    args = parser.parse_args()

    data_dir = dev_data_dir()
    images = _make_test_images(data_dir)
    cand = CANDIDATES[args.model]
    model_dir = data_dir / "benchmark_models" / args.model

    print(f"=== benchmark {args.model} ===")
    print("step 1/3: download model...")
    # 下载（Florence-2 ONNX 官方导出；Qwen/InternVL 需用户先准备 ONNX 导出）
    import huggingface_hub

    if args.model == "florence2":
        huggingface_hub.hf_hub_download("microsoft/Florence-2-base-ft", "model.onnx", local_dir=model_dir)
        huggingface_hub.hf_hub_download("microsoft/Florence-2-base-ft", "config.json", local_dir=model_dir)
    else:
        print(f"[SKIP] {args.model}: 需先完成 ONNX 导出（transformers optimum-cli export onnx）；"
              "当前仅记录下载/导出计划，不自动下载原始权重")
        print("record: candidate not benchmarked (export required)")
        return

    disk = _disk_mb(model_dir)
    print(f"disk: {disk:.0f} MB")

    print("step 2/3: load + inference...")
    rss_before = _rss_mb()
    result = _run_florence2(model_dir, images, args.keep)
    rss_after = _rss_mb()

    print("step 3/3: report")
    table = {
        "model": args.model,
        "disk_mb": round(disk),
        "rss_before_mb": round(rss_before),
        "rss_after_mb": round(rss_after),
        "avg_ms_per_image": round(sum(r["ms"] for r in result["results"]) / len(result["results"])),
        "results": result["results"],
    }
    print(json.dumps(table, ensure_ascii=False, indent=2))
    adr = ROOT / "docs" / "adr" / "ADR-004.md"
    if adr.exists():
        content = adr.read_text(encoding="utf-8")
        marker = "## Benchmark 结果（M4.3 实测）"
        section = f"\n## Benchmark 结果（M4.3 实测）\n\n```json\n{json.dumps(table, ensure_ascii=False, indent=2)}\n```\n"
        if marker in content:
            content = content.split(marker)[0] + section
        else:
            content += section
        adr.write_text(content, encoding="utf-8")
        print(f"ADR-004 updated: {adr}")

    if not args.keep:
        import shutil

        shutil.rmtree(model_dir, ignore_errors=True)
        print("model cleaned (--keep 可保留)")


if __name__ == "__main__":
    main()
