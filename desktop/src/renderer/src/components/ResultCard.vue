<script setup lang="ts">
/**
 * ResultCard —— M5 §14 产品化结果卡：
 * filename / path / type / size / modified / RRF score / match reasons（通道着色）/ time confidence。
 * 数据为 M5 Hybrid 响应（rrf_score/keyword_score/semantic_score/time_info/match_reasons，§12.5/§12.6）。
 */
import { computed } from "vue";
import type { MatchReason, SearchResultItem } from "../../../shared/contracts";

const props = defineProps<{ item: SearchResultItem }>();

const sizeText = computed(() => {
  const b = props.item.size_bytes;
  if (b >= 1024 * 1024 * 1024) return `${(b / 1024 ** 3).toFixed(1)} GB`;
  if (b >= 1024 * 1024) return `${(b / 1024 ** 2).toFixed(1)} MB`;
  if (b >= 1024) return `${(b / 1024).toFixed(1)} KB`;
  return `${b} B`;
});

const mtimeText = computed(() =>
  new Date(Math.floor(props.item.mtime_ns / 1_000_000)).toLocaleString(),
);

const timeInfoText = computed(() => {
  const t = props.item.time_info;
  if (!t.basis || !t.confidence) return null;
  const verb = { exif: "拍摄于", mtime: "修改于", ctime: "创建于" }[t.basis] ?? "时间";
  const conf = t.confidence === "exact" ? "exact" : "fallback";
  return `${verb} ${t.value ?? ""}（${conf}）`;
});

// 通道中文标签与颜色（§12.6）
const CHANNEL_LABEL: Record<string, string> = {
  keyword: "文件名", body: "正文", ocr: "OCR", semantic: "语义", metadata: "元数据",
};

function reasonLabel(r: MatchReason): string {
  return CHANNEL_LABEL[r.channel] ?? r.channel;
}
</script>

<template>
  <article class="result-card">
    <div class="row">
      <strong class="filename">{{ item.filename }}</strong>
      <span class="type-badge" :data-type="item.file_type">{{ item.file_type }}</span>
      <span v-if="item.rrf_score !== null" class="rrf">RRF {{ item.rrf_score.toFixed(4) }}</span>
    </div>
    <p class="path">{{ item.dir_path }}</p>
    <p class="meta">
      {{ sizeText }} · {{ mtimeText }}
      <template v-if="item.keyword_score !== null"> · 关键词 {{ item.keyword_score.toFixed(1) }}</template>
      <template v-if="item.semantic_score !== null"> · 语义 {{ item.semantic_score.toFixed(3) }}</template>
    </p>
    <div class="reasons">
      <span v-for="(r, i) in item.match_reasons" :key="i" class="reason" :data-channel="r.channel">
        <b>{{ reasonLabel(r) }}</b> {{ r.text }}
      </span>
    </div>
    <p v-if="timeInfoText" class="time-info">{{ timeInfoText }}</p>
  </article>
</template>

<style scoped>
.result-card {
  padding: 10px 14px;
  border: 1px solid #e2e2e2;
  border-radius: 8px;
  margin-bottom: 8px;
  background: #fff;
}
.row {
  display: flex;
  align-items: center;
  gap: 8px;
}
.filename {
  font-size: 14px;
  word-break: break-all;
}
.type-badge {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 10px;
  background: #eef2ff;
  color: #4338ca;
}
.rrf {
  font-size: 11px;
  color: #7c3aed;
  margin-left: auto;
}
.path {
  margin: 4px 0 0;
  font-size: 12px;
  color: #666;
  word-break: break-all;
}
.meta {
  margin: 4px 0 0;
  font-size: 12px;
  color: #999;
}
.reasons {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 6px;
}
.reason {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 8px;
  background: #f9fafb;
  color: #4b5563;
  border: 1px solid #e5e7eb;
}
.reason[data-channel="keyword"] { background: #ecfdf5; color: #047857; border-color: #a7f3d0; }
.reason[data-channel="body"]    { background: #eff6ff; color: #1d4ed8; border-color: #bfdbfe; }
.reason[data-channel="ocr"]     { background: #fdf2f8; color: #be185d; border-color: #fbcfe8; }
.reason[data-channel="semantic"]{ background: #faf5ff; color: #7c3aed; border-color: #e9d5ff; }
.reason[data-channel="metadata"]{ background: #fffbeb; color: #b45309; border-color: #fde68a; }
.time-info {
  margin: 6px 0 0;
  font-size: 12px;
  color: #b45309;
}
</style>
