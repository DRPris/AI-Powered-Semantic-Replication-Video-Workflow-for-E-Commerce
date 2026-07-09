/**
 * 项目详情页
 *
 * 组成部分：
 *   1. 项目信息 + 进度时间线（10 秒自动刷新）
 *   2. 成品视频预览（COMPLETED 后显示）
 *   3. 关键帧审核操作条（KEYFRAME_REVIEW 状态时显示"全部通过并开始生成"）
 *   4. 分镜卡片墙
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  approveKeyframes,
  fetchProject,
  fetchShots,
  fetchTokenUsage,
} from "../api.js";
import StatusBadge from "../components/StatusBadge.jsx";
import Timeline from "../components/Timeline.jsx";
import ShotCard from "../components/ShotCard.jsx";
import { attachmentUrl } from "../status.js";

const REFRESH_MS = 10000;

export default function ProjectDetail({ projectId }) {
  const [project, setProject] = useState(null);
  const [shots, setShots] = useState([]);
  const [usage, setUsage] = useState(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [approving, setApproving] = useState(false);

  const load = useCallback(async () => {
    try {
      const [projectData, shotsData] = await Promise.all([
        fetchProject(projectId),
        fetchShots(projectId),
      ]);
      setProject(projectData);
      setShots(shotsData.shots || []);
      setError("");
      // token 用量是次要信息，失败不影响页面
      fetchTokenUsage(projectId)
        .then(setUsage)
        .catch(() => {});
    } catch (err) {
      setError(err.message);
    }
  }, [projectId]);

  useEffect(() => {
    load();
    const timer = setInterval(load, REFRESH_MS);
    return () => clearInterval(timer);
  }, [load]);

  const fields = project?.fields || {};
  const status = fields["状态"] || "";
  const finalVideoUrl = attachmentUrl(fields["成片链接"]);

  const approvedCount = useMemo(
    () =>
      shots.filter((s) => (s.fields || {})["提示词审核状态"] === "已通过")
        .length,
    [shots]
  );
  const allApproved = shots.length > 0 && approvedCount === shots.length;

  const handleApproveKeyframes = async () => {
    setApproving(true);
    setNotice("");
    setError("");
    try {
      const res = await approveKeyframes(projectId);
      setNotice(
        `已触发视频生成（任务 ${res.job_id || "已提交"}），可以在此页面挂机等进度。`
      );
      load();
    } catch (err) {
      setError(err.message);
    } finally {
      setApproving(false);
    }
  };

  return (
    <>
      <p style={{ margin: "0 0 12px" }}>
        <a href="#/">← 返回项目列表</a>
      </p>

      {error && <div className="error-banner">{error}</div>}
      {notice && <div className="info-banner">{notice}</div>}

      {/* 项目信息 + 时间线 */}
      <div className="card">
        <div className="row">
          <h1 className="page-title" style={{ margin: 0 }}>
            {fields["项目名称"] || "（未命名项目）"}
          </h1>
          <StatusBadge status={status} />
          <div className="spacer" />
          {usage?.summary?.total_cost_usd != null && (
            <span className="mono">
              成本 ${Number(usage.summary.total_cost_usd).toFixed(3)}
            </span>
          )}
        </div>
        <div className="mono" style={{ margin: "4px 0 12px" }}>
          {projectId}
        </div>
        <Timeline status={status} />
      </div>

      {/* 成品视频 */}
      {finalVideoUrl && (
        <>
          <h2 className="section-title">🎉 成品视频</h2>
          <div className="card">
            <video className="final-video" src={finalVideoUrl} controls />
            <p>
              <a href={finalVideoUrl} target="_blank" rel="noreferrer">
                在新窗口打开 / 下载
              </a>
            </p>
          </div>
        </>
      )}

      {/* 关键帧审核操作条 */}
      {status === "KEYFRAME_REVIEW" && (
        <>
          <h2 className="section-title">关键帧审核</h2>
          <div className="card row">
            <span>
              审核进度：{approvedCount} / {shots.length} 个镜头已通过
            </span>
            <div className="spacer" />
            <button
              className="primary"
              disabled={!allApproved || approving}
              title={
                allApproved
                  ? "确认后进入视频生成（会产生模型调用费用）"
                  : "所有镜头都通过后才能继续"
              }
              onClick={handleApproveKeyframes}
            >
              {approving ? "提交中…" : "✓ 审核完成，开始生成视频"}
            </button>
          </div>
        </>
      )}

      {/* 分镜卡片墙 */}
      <h2 className="section-title">分镜（{shots.length}）</h2>
      {shots.length === 0 ? (
        <div className="card empty">
          还没有分镜记录（脚本生成完成后会出现在这里）
        </div>
      ) : (
        <div className="shot-grid">
          {shots.map((shot) => (
            <ShotCard key={shot.id} shot={shot} onChanged={load} />
          ))}
        </div>
      )}
    </>
  );
}
