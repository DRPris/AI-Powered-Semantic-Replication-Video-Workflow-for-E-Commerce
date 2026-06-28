# Semantic Video Replication Workflow

> 一键式语义视频复刻工作流：基于一段产品视频和一份新商品资料，自动复刻出新商品的成片。

[![Python](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-green.svg)](https://fastapi.tiangolo.com/)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

---

## 功能概览

将一支带货短视频「语义复刻」为一支新商品的同结构带货视频。系统理解原视频的节奏、镜头语言、动作序列，
然后用新商品的资料（图片、商详、3D 模型）重生成等价但合规的成片。

- **Stage 1 准备阶段**：商品理解（Product Brief Agent + 商详视频差分）、原视频节奏分析、视觉资产抠图与三视图生成
- **Stage 2 脚本生成**：基于原视频镜头脚本结构 + 新商品差异，重写脚本
- **Stage 3 提示词转换**：脚本 → 分镜级图像 / 视频 prompt（含审核）
- **Stage 3.5 关键帧生成**：图像编辑模型生成首/尾帧锚点（构图坍缩三层降级保护）
- **Stage 4 视频生成**：Seedance 主用 / Kling 首尾双锚定 / Wan 兜底
- **Stage 5 镜头合成**：FFmpeg 拼接 + OST/字幕叠加 + 镜头内环境音 + 可选 BGM

支持 **simple / full 双模式**：simple 模式跳过审核环节快速预览；full 模式启用全部审查模型保障质量。

---

## 快速开始

### 1. 环境要求
- Python **3.9 ~ 3.10**
- FFmpeg **≥ 4.0**（必需）：`brew install ffmpeg` / `apt install ffmpeg`
- Blender（可选，仅 3D 渲染脚本需要）

### 2. 安装

```bash
git clone <your-repo-url> video-replication-service
cd video-replication-service

python3.10 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

本地开发建议使用 `.python-version` 指定的 Python 3.10。不要直接用系统默认 `python3`，否则在 Python 3.14 等环境下依赖可能无法安装。

### 3. 配置

```bash
cp .env.example .env
```

编辑 `.env`，**至少配置以下密钥**才能跑通主流程：

| 变量 | 用途 | 申请入口 |
|---|---|---|
| `GEMINI_API_KEY` | 视频/图像理解、Prompt 转换、审核 | https://aistudio.google.com/apikey |
| `QWEN_API_KEY` | 分镜提示词自审核 | https://bailian.console.aliyun.com/ |
| `SEEDANCE_API_KEY` | 主用图生视频平台 | https://console.volcengine.com/ark |
| `KLING_ACCESS_KEY` / `KLING_SECRET_KEY` | 首尾双锚定视频平台（可选） | https://klingai.com/dev-center |
| `DATABASE_URL` / `REDIS_URL` | 生产状态库与任务队列 | Docker Compose 默认提供 |
| `API_KEYS` | API 访问密钥，保护高成本接口 | 自行生成强随机字符串 |
| `PROJECT_BUDGET_USD` / `DAILY_BUDGET_USD` | 项目级/日级成本上限 | 自行按预算配置 |
| `OSS_ACCESS_KEY_ID` / `OSS_ACCESS_KEY_SECRET` / `OSS_BUCKET_NAME` | 素材存储 | https://oss.console.aliyun.com |

可选：`TAVILY_API_KEY`（商品品牌检索）、`ELEVENLABS_API_KEY`（环境音）、`SUNO_API_KEY`（BGM）、`WAN_API_KEY`（兜底）。

> 完整变量列表与说明见 [.env.example](.env.example)。

### 4. 启动服务

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

打开 http://localhost:8000/docs 查看 Swagger API 文档。

### 5. 跑一个最小用例（Demo）

准备一支已上传到 OSS（或任意可公开访问 HTTPS）的原视频 + 一张新商品图，最小化触发：

```bash
curl -X POST http://localhost:8000/api/v1/start-workflow \
  -H "X-API-Key: your_internal_api_key_here" \
  -H "Content-Type: application/json" \
  -d '{
    "project_id": "demo_001",
    "project_name": "快速入门Demo",
    "video_url": "https://your-bucket.oss-cn-beijing.aliyuncs.com/demo/original.mp4",
    "product_image_url": "https://your-bucket.oss-cn-beijing.aliyuncs.com/demo/product.jpg",
    "product_listing_url": "https://www.lazada.com.my/products/your-product-i123456.html",
    "mode": "simple"
  }'
```

返回示例：
```json
{"project_id": "550e8400-e29b-41d4-a716-446655440000", "status": "started", "job_id": "job_abc123"}
```

**预期产出节奏**（simple 模式，单镜头 5s × 5 个镜头为例）：

| 阶段 | 耗时（约） | 看哪里 |
|---|---|---|
| Stage 1 视频/商品分析 | 1~3 分钟 | PostgreSQL `projects/assets` 状态 |
| Stage 2 脚本生成 | 30 秒 | PostgreSQL `shots` 分镜字段 |
| Stage 3 提示词 | 30 秒 | PostgreSQL `shots` prompt 字段 |
| Stage 3.5 关键帧 | 2~5 分钟 | PostgreSQL `shots` 关键帧 URL |
| **人审关键帧** | 取决于你 | 通过 API / 后续管理后台更新审核状态 |
| Stage 4 视频生成 | 3~8 分钟 | PostgreSQL `shots` 生成视频 URL |
| Stage 5 合成 | 1~2 分钟 | PostgreSQL `projects` 最终视频字段 |

> 💡 第一次跑建议用 `simple` 模式跳过部分审核以快速验证链路；正式交付切 `full`。

---

## 数据库配置

生产默认使用 PostgreSQL 作为状态库，Redis 作为任务队列通知层：

- `DATA_BACKEND=postgres`
- `JOB_BACKEND=durable`
- `DATABASE_URL=postgresql+asyncpg://...`
- `REDIS_URL=redis://...`

Airtable 已降级为 legacy adapter；只有显式设置 `DATA_BACKEND=airtable` 时才需要 `AIRTABLE_API_KEY` / `AIRTABLE_BASE_ID`。生产上线不建议依赖 Airtable 作为核心数据库。

如需运行旧 Airtable demo，再额外安装：

```bash
pip install -r requirements-airtable.txt
```

## API 鉴权

生产默认开启 API 鉴权：

- `API_AUTH_ENABLED=true`
- `API_KEYS=key1,key2`

请求任意 `/api/v1/*` 接口时，需要携带其中一种 header：

```bash
X-API-Key: key1
```

或：

```bash
Authorization: Bearer key1
```

`/health` 和 `/ready` 不需要鉴权，便于负载均衡和容器健康检查。开发环境如需临时关闭，可设置 `API_AUTH_ENABLED=false`；生产不建议关闭。

## 成本治理

生产默认将 token 与估算成本明细写入 PostgreSQL `token_usage` 表，并保留旧 JSON 记录作为本地兼容 fallback。

关键配置：

- `COST_TRACKING_BACKEND=database`
- `ENABLE_COST_GUARD=true`
- `PROJECT_BUDGET_USD=20`
- `DAILY_BUDGET_USD=50`

当项目累计成本或当日总成本达到预算上限时，高成本入口会返回 `402`，避免继续触发模型调用。成本查询接口：

- `GET /api/v1/token-usage/{project_id}`
- `GET /api/v1/token-usage`

---

## 项目结构

```
.
├── agents/              # Product Brief Agent / Clip Editor Agent
├── workflows/           # 5 个 Stage 的编排逻辑
├── services/            # 各 AI/云平台服务封装（gemini / kling / seedance / wan / oss / database / ffmpeg ...）
├── prompts/             # 所有 prompt 模板（按 stage / 任务分类）
├── models/              # Pydantic 数据模型
├── scripts/             # 通用运维工具（见 scripts/README.md）
├── assets/fonts/        # OST/字幕字体（OFL 协议）
├── main.py              # FastAPI 入口
├── config.py            # pydantic-settings 配置加载
└── .env.example         # 环境变量模板
```

---

## API 调用流程

工作流不是一键全自动 —— 中间有 **2 个人审卡点**（Product Brief 与 Keyframe），所以是「分阶段触发」模式。

```mermaid
graph TB
    A[POST /start-workflow] --> B[Stage 1 视频+商品分析]
    B --> C{Product Brief<br/>是否有待确认问题?}
    C -->|有| D[人审: API/管理后台<br/>填写「用户答复」]
    D --> E[POST /confirm-brief]
    C -->|无| F[Stage 2 脚本生成]
    E --> F
    F --> G[Stage 3 提示词转换]
    G --> H[Stage 3.5 关键帧生成]
    H --> I[人审: API/管理后台<br/>批准「关键帧状态」]
    I --> J[POST /generate-shots]
    J --> K[Stage 4 视频生成]
    K --> L[GET /generation-status/job_id<br/>轮询直到 done]
    L --> M[POST /compose-video]
    M --> N[Stage 5 拼接+OST+BGM+环境音]
    N --> O[GET /compose-status/job_id]
    O --> P[数据库项目记录拿成片 URL]
```

### 接口清单

| 阶段 | 方法 + 路径 | 触发时机 |
|---|---|---|
| 启动 | `POST /api/v1/start-workflow` | 提交 video_url + product_image_url 启动 Stage 1 |
| 确认 Brief | `POST /api/v1/projects/{project_id}/confirm-brief` | 看到项目有待确认问题时填答案后调用 |
| 触发视频生成 | `POST /api/v1/generate-shots` | 关键帧人审通过后调用 |
| 查 Stage 4 进度 | `GET /api/v1/generation-status/{job_id}` | 轮询 |
| 查一键工作流进度 | `GET /api/v1/jobs/{job_id}` | `/start-workflow` 返回 job_id 后轮询 |
| 触发合成 | `POST /api/v1/compose-video` | Stage 4 全部 done 后调用 |
| 查 Stage 5 进度 | `GET /api/v1/compose-status/{job_id}` | 轮询 |
| 查项目人审状态 | `GET /api/v1/project/{project_id}/review-status` | 任意时刻 |
| 健康检查 | `GET /health` | 启动后烟测 |

当 `JOB_BACKEND=durable` 时，`/start-workflow`、`/generate-shots`、`/approve-keyframes` 和 `/compose-video` 都会创建 PostgreSQL job 并交给 worker 执行；内存任务状态只作为本地开发 fallback。

完整接口与参数见 http://localhost:8000/docs（Swagger UI）。

---

## 双模式

```python
# simple 模式：跳过部分审核，快速预览
POST /api/v1/projects/{id}/start  body: {"mode": "simple"}

# full 模式：启用全部审查模型，质量保障
POST /api/v1/projects/{id}/start  body: {"mode": "full"}
```

---

## 部署

推荐 Docker 部署。当前 Compose 栈包含 FastAPI、Worker、PostgreSQL、Redis 和迁移任务。容器健康检查使用 `/ready`，只有核心配置、FFmpeg、运行目录和持久化基础设施均就绪时才返回 200。注意：
- FFmpeg 必须在容器中可用
- `tmp/` 目录需要可写
- OSS Endpoint 与 Bucket 区域需一致
- 海外 API（Gemini/ElevenLabs/Tripo3D）若部署在国内服务器需配置代理

生产任务后端：

```bash
JOB_BACKEND=durable  # PostgreSQL + Redis + worker
JOB_BACKEND=memory   # 本地开发回退，不可用于生产
```

更多说明见 [docs/production-infrastructure.md](docs/production-infrastructure.md)。

基础存活检查与部署就绪检查：

```bash
curl http://localhost:8000/health
curl http://localhost:8000/ready
```

`/health` 只表示进程存活；`/ready` 用于负载均衡和部署验证。

---

## 字体许可

`assets/fonts/` 中的字体均来自 [Google Fonts](https://fonts.google.com)，遵循 **SIL Open Font License (OFL) 1.1**：
- Anton — Vernon Adams
- Archivo Black — Omnibus-Type
- Oswald — Vernon Adams
- Poppins — Indian Type Foundry

---

## License

[MIT](LICENSE)

---

## 常见问题 FAQ

### Q1. 启动后 Stage 1 一直没动 / 报 Gemini 连接超时
**原因**：Gemini API 在国内需要走代理。
**解法**：在 `.env` 中配置 `HTTP_PROXY` 与 `HTTPS_PROXY`，例如：
```bash
HTTP_PROXY=socks5h://127.0.0.1:7890
HTTPS_PROXY=socks5h://127.0.0.1:7890
```

### Q2. Stage 4 某个 shot 在 Seedance 后台是 succeeded，但 Airtable 里却是 failed
**原因**：本地轮询遭遇 httpx 网络抖动，吞掉了 success 响应。
**解法**：参考 [.qoder/skills/incident-recovery/recipes.md](.qoder/skills/incident-recovery/recipes.md) 的 recipe-1：用 task_id 直接重拉 task 结果（48h 内有效）。

### Q3. Airtable 写「生成视频」字段报 422 `INVALID_ATTACHMENT_OBJECT`
**原因**：该字段是字符串 URL 类型，不是 attachment 数组。
**解法**：写入时传字符串 `{"生成视频": "https://..."}`，**不要**传 `[{"url": "..."}]`。

### Q4. 视频 URL 过期下载失败 / Kling URL 返回 403
- **过期**：OSS 签名 URL 默认 7 天有效，过期后用 `OSSService.get_signed_url()` 重签即可。
- **403**：Kling CDN 强校验 User-Agent，下载请求需要带 `User-Agent: Mozilla/5.0 ...` 头部。

### Q5. 关键帧画错了产品形态/构图（如按钮位置错、凭空多了人物）
**原因**：通常是 Stage 2 脚本改写时丢失了原视频的物理约束。
**解法**：使用 `visual-content-refinement` skill 的诊断决策树，定位是 Stage 2 还是 Stage 3 的问题，然后手工修正对应字段并重生关键帧。详见 [.qoder/skills/visual-content-refinement](.qoder/skills/visual-content-refinement)。

### Q6. 重跑 Stage 4 时 worker 跳过了所有 shot
**原因**：worker 检查到 Airtable「生成视频」字段非空就会跳过。
**解法**：先把所有 shot 的「生成视频」字段清空，然后再调 `/generate-shots`。可写一个 `reset_shots_for_restage4` 脚本，参考 incident-recovery skill 的 recipe-9。

### Q7. 镜头数量不符合预期（原视频 12 镜头，复刻只有 8 镜头）
**原因**：Gemini 视频分割结果不稳定，且 Stage 2 会做镜头合并。
**解法**：检查 Airtable Assets 表 `video_analysis` 字段；如需要严格保持原镜头数，在 Stage 2 prompt 中加约束（不推荐，会牺牲质量）。

### Q8. FFmpeg 报字体找不到 / OST 渲染乱码
**原因**：`assets/fonts/` 目录里字体文件丢失，或字体名拼写错误。
**解法**：确认 4 个字体文件存在（Anton/ArchivoBlack/Oswald/Poppins）；中文字幕需要额外提供中文字体，配置 `SUBTITLE_FONT` 环境变量。

### Q9. ElevenLabs 环境音 401 unauthorized
**原因**：API Key 缺少 `sound_effects` 权限。
**解法**：去 ElevenLabs Dashboard → API Keys → 编辑 → 勾选 `sound_effects` 权限。

### Q10. 想知道某个 shot 现在卡在哪里
```bash
curl http://localhost:8000/api/v1/project/recXXX/review-status
```
返回会告诉你当前 Stage、有没有待人审、各 shot 的状态。

> 更多事故应对见 `incident-recovery` skill；视觉质量调整见 `visual-content-refinement` skill。

---

## 安全声明

- **永远不要**将 `.env` 文件提交到版本控制
- 所有外部 API Key 都应该走 RAM 子账号 / 最小权限原则（尤其是 OSS 与 ElevenLabs）
- 生产部署建议使用密钥管理服务（KMS / Secrets Manager）替代 `.env`
