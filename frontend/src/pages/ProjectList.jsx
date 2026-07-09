/**
 * 项目列表页
 *
 * 展示全部项目（按创建时间倒序），点击行进入详情页。
 * 每 15 秒自动刷新一次，方便挂着看进度。
 */
import { useCallback, useEffect, useState } from "react";
import { fetchProjects } from "../api.js";
import StatusBadge from "../components/StatusBadge.jsx";

const REFRESH_MS = 15000;

export default function ProjectList() {
  const [projects, setProjects] = useState(null);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      const data = await fetchProjects();
      setProjects(data.projects || []);
      setError("");
    } catch (err) {
      setError(err.message);
    }
  }, []);

  useEffect(() => {
    load();
    const timer = setInterval(load, REFRESH_MS);
    return () => clearInterval(timer);
  }, [load]);

  return (
    <>
      <h1 className="page-title">项目列表</h1>
      <p className="page-sub">点击项目查看进度、分镜与审核</p>

      {error && <div className="error-banner">{error}</div>}

      <div className="card" style={{ padding: 0 }}>
        {projects === null && !error && <div className="empty">加载中…</div>}
        {projects !== null && projects.length === 0 && (
          <div className="empty">
            还没有项目。通过 POST /api/v1/start-workflow 启动一个工作流后，这里就会出现。
          </div>
        )}
        {projects !== null && projects.length > 0 && (
          <table className="project-table">
            <thead>
              <tr>
                <th>项目名称</th>
                <th>状态</th>
                <th>模式</th>
                <th>项目 ID</th>
                <th>创建时间</th>
              </tr>
            </thead>
            <tbody>
              {projects.map((p) => {
                const fields = p.fields || {};
                return (
                  <tr
                    key={p.id}
                    onClick={() => {
                      window.location.hash = `/projects/${encodeURIComponent(p.id)}`;
                    }}
                  >
                    <td>{fields["项目名称"] || "（未命名）"}</td>
                    <td>
                      <StatusBadge status={fields["状态"]} />
                    </td>
                    <td>{fields["模式"] || "-"}</td>
                    <td className="mono">{p.id}</td>
                    <td className="mono">
                      {p.createdTime
                        ? new Date(p.createdTime).toLocaleString("zh-CN")
                        : "-"}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}
