/**
 * 新建项目页
 *
 * 作用：填写素材信息并一键启动复刻工作流。
 * 原视频 / 商品图 / 三视图 三类素材都支持两种提供方式（二选一）：
 *   - 粘贴公网 URL
 *   - 上传本地文件（先传到后端换取 OSS 公网 URL，再启动工作流）
 *
 * 提交流程：上传本地文件（如有）→ 调 /api/v1/start-workflow → 跳转项目详情页
 */
import { useState } from "react";
import { startWorkflow, uploadAsset } from "../api.js";

/**
 * 单个素材输入组件（URL / 本地文件 二选一）
 *
 * @param label    显示名称，如 "原视频"
 * @param required 是否必填
 * @param accept   文件选择器接受的类型，如 "video/*"
 * @param value    { mode: "url"|"file", url: string, file: File|null }
 * @param onChange 值变化回调
 */
function AssetInput({ label, required, accept, hint, value, onChange }) {
  const setMode = (mode) => onChange({ ...value, mode });

  return (
    <div className="asset-input">
      <div className="asset-head">
        <span className="asset-label">
          {label}
          {required && <span className="required-mark"> *</span>}
        </span>
        <div className="mode-switch">
          <button
            type="button"
            className={value.mode === "url" ? "active" : ""}
            onClick={() => setMode("url")}
          >
            填链接
          </button>
          <button
            type="button"
            className={value.mode === "file" ? "active" : ""}
            onClick={() => setMode("file")}
          >
            传本地文件
          </button>
        </div>
      </div>

      {value.mode === "url" ? (
        <input
          placeholder="https:// 开头的公网链接"
          value={value.url}
          onChange={(e) => onChange({ ...value, url: e.target.value })}
        />
      ) : (
        <label className="file-drop">
          <input
            type="file"
            accept={accept}
            style={{ display: "none" }}
            onChange={(e) =>
              onChange({ ...value, file: e.target.files?.[0] || null })
            }
          />
          {value.file ? (
            <span>
              已选择：{value.file.name}（
              {(value.file.size / 1024 / 1024).toFixed(1)} MB）
            </span>
          ) : (
            <span className="placeholder">点击选择本地文件</span>
          )}
        </label>
      )}
      {hint && <p className="asset-hint">{hint}</p>}
    </div>
  );
}

const emptyAsset = { mode: "url", url: "", file: null };

/** 商品图数量上限（与后端 StartWorkflowRequest.MAX_PRODUCT_IMAGES 保持一致） */
const MAX_PRODUCT_IMAGES = 5;

/**
 * 商品图多图输入组件（URL / 本地文件 二选一）
 *
 * 为什么支持多图：多角度真实照片会直接作为关键帧生成的产品形态锚点。
 * 上传 ≥2 张时后端跳过 AI 三视图生成（AI 三视图从单图脑补角度，可能失真，
 * 是产品逐帧变形的主要根源）。
 *
 * @param value    { mode: "url"|"file", urls: string, files: File[] }
 *                 urls 为多行文本，每行一个链接
 * @param onChange 值变化回调
 */
function ProductImagesInput({ value, onChange }) {
  const setMode = (mode) => onChange({ ...value, mode });

  return (
    <div className="asset-input">
      <div className="asset-head">
        <span className="asset-label">
          商品图（可多张）<span className="required-mark"> *</span>
        </span>
        <div className="mode-switch">
          <button
            type="button"
            className={value.mode === "url" ? "active" : ""}
            onClick={() => setMode("url")}
          >
            填链接
          </button>
          <button
            type="button"
            className={value.mode === "file" ? "active" : ""}
            onClick={() => setMode("file")}
          >
            传本地文件
          </button>
        </div>
      </div>

      {value.mode === "url" ? (
        <textarea
          rows={3}
          placeholder={"每行一个 https:// 链接，最多 " + MAX_PRODUCT_IMAGES + " 张"}
          value={value.urls}
          onChange={(e) => onChange({ ...value, urls: e.target.value })}
        />
      ) : (
        <label className="file-drop">
          <input
            type="file"
            accept="image/*"
            multiple
            style={{ display: "none" }}
            onChange={(e) =>
              onChange({
                ...value,
                files: Array.from(e.target.files || []).slice(
                  0,
                  MAX_PRODUCT_IMAGES
                ),
              })
            }
          />
          {value.files.length > 0 ? (
            <span>
              已选择 {value.files.length} 张：
              {value.files.map((f) => f.name).join("、")}
            </span>
          ) : (
            <span className="placeholder">
              点击选择本地图片（可按住 Ctrl/Cmd 多选）
            </span>
          )}
        </label>
      )}
      <p className="asset-hint">
        强烈建议上传 2~4 张不同角度的商品实拍图（正面/侧面/俯视/细节）。
        多图时系统直接用真图锁定产品形态，视频里的产品更不容易变形
      </p>
    </div>
  );
}

export default function NewProject() {
  const [name, setName] = useState("");
  const [video, setVideo] = useState({ ...emptyAsset });
  const [productImages, setProductImages] = useState({
    mode: "url",
    urls: "",
    files: [],
  });
  const [threeView, setThreeView] = useState({ ...emptyAsset });
  const [listingUrl, setListingUrl] = useState("");
  const [mode, setMode] = useState("full");
  const [submitting, setSubmitting] = useState(false);
  const [progress, setProgress] = useState("");
  const [error, setError] = useState("");

  /** 校验单个素材：返回错误文案，通过则返回空串 */
  const checkAsset = (label, asset, required) => {
    if (asset.mode === "url") {
      if (!asset.url.trim()) {
        return required ? `请填写「${label}」的链接，或改为上传本地文件` : "";
      }
      if (!/^https?:\/\//i.test(asset.url.trim())) {
        return `「${label}」的链接必须以 http(s):// 开头`;
      }
    } else if (!asset.file) {
      return required ? `请选择「${label}」的本地文件，或改为填写链接` : "";
    }
    return "";
  };

  /** 把素材解析成公网 URL：URL 模式直接返回，文件模式先上传 */
  const resolveAsset = async (label, kind, asset) => {
    if (asset.mode === "url") {
      const url = asset.url.trim();
      return url || null;
    }
    if (!asset.file) return null;
    setProgress(`正在上传${label}（${asset.file.name}）…`);
    const res = await uploadAsset(kind, asset.file);
    return res.url;
  };

  /** 校验并解析商品图输入：返回 { error } 或 { urls, files } */
  const checkProductImages = () => {
    if (productImages.mode === "url") {
      const urls = productImages.urls
        .split("\n")
        .map((s) => s.trim())
        .filter(Boolean);
      if (urls.length === 0) {
        return { error: "请至少填写一张商品图链接，或改为上传本地文件" };
      }
      if (urls.length > MAX_PRODUCT_IMAGES) {
        return { error: `商品图最多 ${MAX_PRODUCT_IMAGES} 张，当前填了 ${urls.length} 张` };
      }
      const bad = urls.find((u) => !/^https?:\/\//i.test(u));
      if (bad) {
        return { error: `商品图链接必须以 http(s):// 开头：${bad}` };
      }
      return { urls, files: [] };
    }
    if (productImages.files.length === 0) {
      return { error: "请选择至少一张商品图本地文件，或改为填写链接" };
    }
    return { urls: [], files: productImages.files };
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError("");

    for (const [label, asset, required] of [
      ["原视频", video, true],
      ["三视图", threeView, false],
    ]) {
      const msg = checkAsset(label, asset, required);
      if (msg) {
        setError(msg);
        return;
      }
    }
    const productCheck = checkProductImages();
    if (productCheck.error) {
      setError(productCheck.error);
      return;
    }
    if (listingUrl.trim() && !/^https?:\/\//i.test(listingUrl.trim())) {
      setError("商品详情页链接必须以 http(s):// 开头");
      return;
    }

    setSubmitting(true);
    try {
      // 1) 本地文件先换取公网 URL（视频可能较大，逐个上传方便展示进度）
      const videoUrl = await resolveAsset("原视频", "video", video);

      // 商品图：URL 直接用，本地文件逐张上传换取 OSS URL
      const productImageUrls = [...productCheck.urls];
      for (let i = 0; i < productCheck.files.length; i++) {
        const f = productCheck.files[i];
        setProgress(
          `正在上传商品图 ${i + 1}/${productCheck.files.length}（${f.name}）…`
        );
        const res = await uploadAsset("product_image", f);
        productImageUrls.push(res.url);
      }

      const threeViewUrl = await resolveAsset("三视图", "three_view", threeView);

      // 2) 启动工作流。project_id 用时间戳+随机数自动生成，保证幂等去重
      setProgress("素材就绪，正在启动工作流…");
      const projectId = `web_${Date.now()}_${Math.random()
        .toString(36)
        .slice(2, 8)}`;
      const res = await startWorkflow({
        project_id: projectId,
        project_name: name.trim(),
        video_url: videoUrl,
        product_image_url: productImageUrls[0],
        product_image_urls: productImageUrls,
        mode,
        ...(threeViewUrl ? { three_view_image_url: threeViewUrl } : {}),
        ...(listingUrl.trim() ? { product_listing_url: listingUrl.trim() } : {}),
      });

      // 3) 跳到详情页挂机看进度（后端返回的是 Airtable 记录 ID）
      window.location.hash = `/projects/${encodeURIComponent(res.project_id)}`;
    } catch (err) {
      setError(err.message);
    } finally {
      setSubmitting(false);
      setProgress("");
    }
  };

  return (
    <>
      <p style={{ margin: "0 0 12px" }}>
        <a href="#/">← 返回项目列表</a>
      </p>
      <h1 className="page-title">新建复刻项目</h1>
      <p className="page-sub">
        原视频和商品图必填，支持粘贴公网链接或直接上传本地文件
      </p>

      {error && <div className="error-banner">{error}</div>}
      {progress && <div className="info-banner">{progress}</div>}

      <form className="card new-project-form" onSubmit={handleSubmit}>
        <label>
          项目名称（可选）
          <input
            placeholder="例如：折叠水壶-复刻宠物饮水器视频"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </label>

        <AssetInput
          label="原视频"
          required
          accept="video/*"
          hint="要复刻的参考视频（mp4/mov/webm，本地上传上限 300MB）"
          value={video}
          onChange={setVideo}
        />

        <ProductImagesInput value={productImages} onChange={setProductImages} />

        <AssetInput
          label="三视图（可选，高级选项）"
          accept="image/*"
          hint="商品正/侧/顶三视图拼合图。已上传多张商品图时无需提供；只传 1 张商品图且不提供三视图时，系统会用 AI 自动生成"
          value={threeView}
          onChange={setThreeView}
        />

        <label>
          商品详情页链接（可选，强烈建议填写）
          <input
            placeholder="https://... 电商详情页，用于提取卖点和官方使用方式"
            value={listingUrl}
            onChange={(e) => setListingUrl(e.target.value)}
          />
        </label>

        <label>
          复刻模式
          <select value={mode} onChange={(e) => setMode(e.target.value)}>
            <option value="full">full - 完整流程（脚本+关键帧+审核）</option>
            <option value="simple">simple - 简单模式（跳过脚本环节）</option>
          </select>
        </label>

        <div className="row">
          <button className="primary" type="submit" disabled={submitting}>
            {submitting ? "提交中…" : "🚀 启动复刻工作流"}
          </button>
          <span className="page-sub" style={{ margin: 0 }}>
            启动后会产生模型调用费用
          </span>
        </div>
      </form>
    </>
  );
}
