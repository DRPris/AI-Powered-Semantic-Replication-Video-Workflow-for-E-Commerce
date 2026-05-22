# scripts/

通用运维工具集合。所有脚本都基于 `Path(__file__).resolve().parent.parent` 推导项目根目录，可直接在任意机器上运行。

> 项目历史上还存在大量针对**特定项目特定事故**的一次性补救脚本（recover_/regen_/retry_/patch_ 等），
> 因为含具体 Airtable Record ID 且对外部用户无价值，已在发布时全部剔除。
> 如果你遇到类似问题（task 已成功但 URL 过期 / CDN 403 / Airtable 写入失败 / 单镜头需重生），
> 请基于 `archive_project.py` 等模板自行编写补救脚本。

---

## 脚本清单

| 脚本 | 用途 | 依赖 |
|---|---|---|
| [archive_project.py](archive_project.py) | 把指定 Airtable 项目（任意 record id）归档为优质样本，下载素材+元数据到 `archive/` | Airtable / OSS |
| [debug_listing.py](debug_listing.py) | 商品链接抓取与解析诊断（HTTP / SPA / JSON-LD / Lazada 等多层产物） | - |
| [download_blender_videos.py](download_blender_videos.py) | 批量下载某项目的所有镜头视频到本地预览 | Airtable / OSS |
| [generate_3d_model.py](generate_3d_model.py) | Tripo3D v2.5 从产品图生成 GLB 3D 模型 | `FAL_KEY`（fal.ai） |
| [product_to_3d.py](product_to_3d.py) | 完整流程：产品图 → 抠图 → 白底图 → 上传 OSS → 3D 建模 | `FAL_KEY` + OSS |
| [render_3d_views.py](render_3d_views.py) | 从 GLB 模型渲染 5 张多角度白底参考图（正/侧/45°/俯） | trimesh + matplotlib |

---

## 使用示例

### 归档一个优质项目
```bash
python scripts/archive_project.py recXXXXXXXXXXXXXX --with-videos
```

### 抓取商品页诊断
```bash
python scripts/debug_listing.py "https://www.lazada.com/products/..."
```

### 跑 3D 建模流程

先在 `.env` 中配置：
```
FAL_KEY=your-fal-api-key
PROJECT_ID=recXXXXXXXXXXXXXX
```

然后：
```bash
python scripts/product_to_3d.py
```

### 自定义 3D 渲染参数
```bash
GLB_PATH=/path/to/model.glb \
RENDER_OUTPUT_DIR=/path/to/output \
python scripts/render_3d_views.py
```
