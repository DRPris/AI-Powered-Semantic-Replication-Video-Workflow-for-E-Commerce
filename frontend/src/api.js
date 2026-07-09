/**
 * API 请求层
 *
 * 作用：统一封装对后端 FastAPI 的请求，自动带上 API Key。
 * 配置存在浏览器 localStorage 里（仅本机可见）：
 *   - console_api_base：后端地址，开发时留空走 Vite 代理即可
 *   - console_api_key ：X-API-Key 的值（.env 中 API_KEYS 配置的 key）
 */

const BASE_KEY = "console_api_base";
const APIKEY_KEY = "console_api_key";

export function getApiBase() {
  return localStorage.getItem(BASE_KEY) || "";
}

export function getApiKey() {
  return localStorage.getItem(APIKEY_KEY) || "";
}

export function saveSettings(base, key) {
  localStorage.setItem(BASE_KEY, base.trim().replace(/\/+$/, ""));
  localStorage.setItem(APIKEY_KEY, key.trim());
}

/** 统一请求函数：拼 URL、带 Key、抛出可读的错误信息 */
async function request(path, options = {}) {
  const url = `${getApiBase()}${path}`;
  const headers = {
    "Content-Type": "application/json",
    ...(getApiKey() ? { "X-API-Key": getApiKey() } : {}),
    ...options.headers,
  };
  let response;
  try {
    response = await fetch(url, { ...options, headers });
  } catch (err) {
    throw new Error(`无法连接后端服务（${url}）：${err.message}`);
  }
  const text = await response.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = { detail: text };
  }
  if (!response.ok) {
    const detail =
      typeof data?.detail === "string"
        ? data.detail
        : JSON.stringify(data?.detail ?? data);
    throw new Error(`请求失败（HTTP ${response.status}）：${detail}`);
  }
  return data;
}

// ---- 项目 ----
export const fetchProjects = () => request("/api/v1/projects");
export const fetchProject = (id) =>
  request(`/api/v1/projects/${encodeURIComponent(id)}`);
export const fetchShots = (id) =>
  request(`/api/v1/projects/${encodeURIComponent(id)}/shots`);
export const fetchTokenUsage = (id) =>
  request(`/api/v1/token-usage/${encodeURIComponent(id)}`);

// ---- 审核 ----
export const reviewShot = (shotId, { reviewType = "prompt", status, comment = "" }) =>
  request(`/api/v1/shots/${encodeURIComponent(shotId)}/review`, {
    method: "POST",
    body: JSON.stringify({ review_type: reviewType, status, comment }),
  });

export const approveKeyframes = (projectId) =>
  request(`/api/v1/projects/${encodeURIComponent(projectId)}/approve-keyframes`, {
    method: "POST",
  });
