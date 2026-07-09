/**
 * 连接设置面板
 *
 * 作用：填写后端地址和 API Key，存到 localStorage。
 * 开发模式下后端地址留空即可（走 Vite 代理转发到 localhost:8000）。
 */
import { useState } from "react";
import { getApiBase, getApiKey, saveSettings } from "../api.js";

export default function SettingsPanel({ onClose }) {
  const [base, setBase] = useState(getApiBase());
  const [key, setKey] = useState(getApiKey());
  const [saved, setSaved] = useState(false);

  const handleSave = () => {
    saveSettings(base, key);
    setSaved(true);
    setTimeout(onClose, 600);
  };

  return (
    <div className="settings-panel card">
      <div>
        <strong>连接设置</strong>
        <p className="page-sub" style={{ margin: "4px 0 0" }}>
          配置只保存在本机浏览器中
        </p>
      </div>
      <label>
        后端地址（本地开发留空即可）
        <input
          placeholder="例如 https://api.example.com"
          value={base}
          onChange={(e) => setBase(e.target.value)}
        />
      </label>
      <label>
        API Key（.env 里 API_KEYS 的值）
        <input
          type="password"
          placeholder="X-API-Key"
          value={key}
          onChange={(e) => setKey(e.target.value)}
        />
      </label>
      <div className="row">
        <button className="primary" onClick={handleSave}>
          {saved ? "已保存 ✓" : "保存"}
        </button>
        <button onClick={onClose}>取消</button>
      </div>
    </div>
  );
}
