/**
 * 应用入口 + 极简 hash 路由
 *
 * 路由规则（不引入 react-router，保持零额外依赖）：
 *   #/                     → 项目列表页
 *   #/new                  → 新建项目页（支持本地文件上传）
 *   #/projects/{projectId} → 项目详情页
 */
import { useEffect, useState } from "react";
import ProjectList from "./pages/ProjectList.jsx";
import ProjectDetail from "./pages/ProjectDetail.jsx";
import NewProject from "./pages/NewProject.jsx";
import SettingsPanel from "./components/SettingsPanel.jsx";

function parseHash() {
  const hash = window.location.hash.replace(/^#/, "") || "/";
  const match = hash.match(/^\/projects\/([^/]+)/);
  if (match) {
    return { page: "detail", projectId: decodeURIComponent(match[1]) };
  }
  if (hash.startsWith("/new")) {
    return { page: "new" };
  }
  return { page: "list" };
}

export default function App() {
  const [route, setRoute] = useState(parseHash());
  const [showSettings, setShowSettings] = useState(false);

  useEffect(() => {
    const onHashChange = () => setRoute(parseHash());
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  return (
    <>
      <header className="topbar">
        <div
          className="brand"
          onClick={() => {
            window.location.hash = "/";
          }}
        >
          🎬 视频复刻控制台
        </div>
        <button onClick={() => setShowSettings((v) => !v)}>
          ⚙️ 连接设置
        </button>
      </header>
      {showSettings && (
        <SettingsPanel onClose={() => setShowSettings(false)} />
      )}
      <main className="container">
        {route.page === "list" && <ProjectList />}
        {route.page === "new" && <NewProject />}
        {route.page === "detail" && (
          <ProjectDetail projectId={route.projectId} />
        )}
      </main>
    </>
  );
}
