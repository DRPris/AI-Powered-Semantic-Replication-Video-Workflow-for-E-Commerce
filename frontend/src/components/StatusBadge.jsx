import { statusMeta } from "../status.js";

/** 项目/分镜状态徽章：根据状态自动配色 */
export default function StatusBadge({ status }) {
  const meta = statusMeta(status);
  return <span className={`badge ${meta.tone}`}>{meta.label}</span>;
}

/** 审核状态徽章（分镜的 已通过/已驳回/待审核 等） */
export function ReviewBadge({ status }) {
  const tone =
    status === "已通过"
      ? "done"
      : status === "已驳回" || status === "需重新生成"
        ? "failed"
        : status === "需修改"
          ? "review"
          : "";
  return <span className={`badge ${tone}`}>{status || "待审核"}</span>;
}
