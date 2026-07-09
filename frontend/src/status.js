/**
 * 项目状态映射
 *
 * 后端 ProjectStatus 枚举值 → 界面展示文案 / 徽章颜色 / 时间线进度。
 * 与 models/schemas.py 的 ProjectStatus 保持一致。
 */

export const STATUS_META = {
  素材准备中: { label: "素材准备中", tone: "running", step: 0 },
  ANALYZING: { label: "素材分析中", tone: "running", step: 0 },
  AWAITING_CONFIRMATION: { label: "等待确认 Brief", tone: "review", step: 0 },
  SCRIPT_GENERATING: { label: "脚本生成中", tone: "running", step: 1 },
  PROMPT_CONVERTING: { label: "提示词转换中", tone: "running", step: 2 },
  KEYFRAME_GENERATING: { label: "关键帧生成中", tone: "running", step: 3 },
  KEYFRAME_REVIEW: { label: "关键帧待审核", tone: "review", step: 4 },
  GENERATING: { label: "视频生成中", tone: "running", step: 5 },
  COMPOSING: { label: "剪辑合成中", tone: "running", step: 6 },
  REVIEWING: { label: "人工审核中", tone: "review", step: 6 },
  COMPLETED: { label: "已完成", tone: "done", step: 7 },
  FAILED: { label: "失败", tone: "failed", step: -1 },
};

/** 时间线阶段（与 step 序号对应） */
export const TIMELINE_STEPS = [
  "素材分析",
  "脚本生成",
  "提示词",
  "关键帧生成",
  "关键帧审核",
  "视频生成",
  "剪辑合成",
  "完成",
];

export function statusMeta(status) {
  return (
    STATUS_META[status] || { label: status || "未知", tone: "", step: -1 }
  );
}

/** 从记录 fields 里安全取附件 URL（Airtable 风格是 [{url}]，也兼容纯字符串） */
export function attachmentUrl(value) {
  if (!value) return "";
  if (typeof value === "string") return value;
  if (Array.isArray(value) && value.length > 0) {
    return value[0]?.url || "";
  }
  return "";
}
