/**
 * 分镜卡片
 *
 * 展示单个镜头的：关键帧图 / 生成视频（有视频优先播视频）、描述、
 * 审核状态，以及"通过 / 驳回"操作。驳回时展开意见输入框。
 */
import { useState } from "react";
import { reviewShot } from "../api.js";
import { ReviewBadge } from "./StatusBadge.jsx";
import { attachmentUrl } from "../status.js";

export default function ShotCard({ shot, onChanged }) {
  const fields = shot.fields || {};
  const shotNumber = fields["镜头序号"] ?? "?";
  const desc = fields["新镜头描述"] || fields["原镜头描述"] || "（无描述）";
  const keyframeUrl = attachmentUrl(fields["关键帧图片"]);
  const videoUrl = attachmentUrl(fields["生成视频"]);
  const promptStatus = fields["提示词审核状态"] || "待审核";
  const reviewComment = fields["提示词审核意见"] || "";
  const editPlan = fields["剪辑指令"] || "";

  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [showReject, setShowReject] = useState(false);
  const [comment, setComment] = useState("");
  const [expanded, setExpanded] = useState(false);

  const submit = async (status, commentText = "") => {
    setBusy(true);
    setError("");
    try {
      await reviewShot(shot.id, { status, comment: commentText });
      setShowReject(false);
      setComment("");
      onChanged?.();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="shot-card">
      <div className="media">
        {videoUrl ? (
          <video src={videoUrl} controls preload="metadata" />
        ) : keyframeUrl ? (
          <img src={keyframeUrl} alt={`镜头 ${shotNumber} 关键帧`} loading="lazy" />
        ) : (
          <span className="placeholder">尚未生成关键帧</span>
        )}
      </div>
      <div className="body">
        <div className="head">
          <span className="title">镜头 {shotNumber}</span>
          <ReviewBadge status={promptStatus} />
        </div>
        <div
          className={`desc ${expanded ? "expanded" : ""}`}
          title="点击展开/收起"
          onClick={() => setExpanded((v) => !v)}
          style={{ cursor: "pointer" }}
        >
          {desc}
        </div>
        {editPlan && (
          <div className="mono" style={{ fontSize: 11 }}>
            剪辑指令：{String(editPlan).slice(0, 80)}…
          </div>
        )}
        {reviewComment && (
          <div style={{ fontSize: 12, color: "var(--amber)" }}>
            审核意见：{reviewComment}
          </div>
        )}
        {error && (
          <div style={{ fontSize: 12, color: "var(--red)" }}>{error}</div>
        )}

        {showReject ? (
          <div className="comment-box">
            <textarea
              rows={2}
              placeholder="填写驳回原因（会写入审核意见）"
              value={comment}
              onChange={(e) => setComment(e.target.value)}
            />
            <div className="actions">
              <button
                className="reject"
                disabled={busy}
                onClick={() => submit("已驳回", comment)}
              >
                确认驳回
              </button>
              <button disabled={busy} onClick={() => setShowReject(false)}>
                取消
              </button>
            </div>
          </div>
        ) : (
          <div className="actions">
            <button
              className="approve"
              disabled={busy || promptStatus === "已通过"}
              onClick={() => submit("已通过")}
            >
              ✓ 通过
            </button>
            <button
              className="reject"
              disabled={busy}
              onClick={() => setShowReject(true)}
            >
              ✕ 驳回
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
