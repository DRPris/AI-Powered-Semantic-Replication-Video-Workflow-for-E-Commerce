"""优质项目归档脚本

将指定 Airtable 项目(project_id)标记为优质样本，并将其中间素材、
Airtable 元数据和一份 Markdown 摘要下载/导出到本地 archive/ 目录。

用法:
    python3 scripts/archive_project.py <project_id> [--no-label] [--with-videos]

默认行为:
    1) 下载 OSS 中 products/, three_views/, keyframes/, frames/ 四类中间素材;
    2) 拉取 Airtable 的 Projects / Assets / Shots / Reviews 记录为 JSON;
    3) 生成 README.md 摘要;
    4) 在 Airtable 项目名称前加 "⭐" 前缀作为优质样本标记(可通过 --no-label 跳过)。

可选参数:
    --with-videos  同时下载 videos/ 与 final_videos/ 下的视频(体积较大,默认不下)。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# 让脚本可以从仓库根目录直接运行 (python3 scripts/archive_project.py)
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env", override=True)

from config import settings  # noqa: E402
from services.airtable_service import AirtableService  # noqa: E402
from services.oss_service import OSSService  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("archive_project")

# 归档涉及的 OSS 前缀；videos/final_videos 默认不含,由 --with-videos 控制
INTERMEDIATE_PREFIXES = ["products", "three_views", "keyframes", "frames"]
VIDEO_PREFIXES = ["videos", "final_videos"]

STAR_PREFIX = "⭐"


def _build_oss() -> OSSService:
    return OSSService(
        access_key_id=settings.OSS_ACCESS_KEY_ID,
        access_key_secret=settings.OSS_ACCESS_KEY_SECRET,
        bucket_name=settings.OSS_BUCKET_NAME,
        endpoint=settings.OSS_ENDPOINT,
        cdn_domain=settings.OSS_CDN_DOMAIN,
    )


def _build_airtable() -> AirtableService:
    return AirtableService(
        api_key=settings.AIRTABLE_API_KEY,
        base_id=settings.AIRTABLE_BASE_ID,
    )


async def _list_prefix(oss: OSSService, prefix: str) -> list[str]:
    """列出某 OSS 前缀下全部对象 key(一次性全列)"""
    bucket = oss._get_bucket()

    def _do_list() -> list[str]:
        import oss2
        keys: list[str] = []
        for obj in oss2.ObjectIterator(bucket, prefix=prefix):
            # 跳过目录占位
            if obj.key.endswith("/"):
                continue
            keys.append(obj.key)
        return keys

    return await asyncio.to_thread(_do_list)


async def _download_object(oss: OSSService, oss_key: str, local_path: Path) -> int:
    """下载单个 OSS 对象到本地,返回字节数"""
    bucket = oss._get_bucket()
    local_path.parent.mkdir(parents=True, exist_ok=True)

    def _do_get() -> int:
        bucket.get_object_to_file(oss_key, str(local_path))
        return local_path.stat().st_size

    size = await asyncio.to_thread(_do_get)
    return size


async def archive_oss_assets(
    oss: OSSService,
    project_id: str,
    out_dir: Path,
    prefixes: list[str],
) -> dict[str, list[dict[str, Any]]]:
    """下载所有 OSS 资产,返回按前缀分组的清单"""
    manifest: dict[str, list[dict[str, Any]]] = {}
    for category in prefixes:
        oss_prefix = f"{category}/{project_id}/"
        logger.info(f"列出 OSS 前缀: {oss_prefix}")
        keys = await _list_prefix(oss, oss_prefix)
        if not keys:
            logger.info(f"  (空) {oss_prefix}")
            manifest[category] = []
            continue

        cat_records: list[dict[str, Any]] = []
        for key in keys:
            rel = key[len(oss_prefix):]  # 去掉前缀
            local_path = out_dir / category / rel
            try:
                size = await _download_object(oss, key, local_path)
                cat_records.append({
                    "oss_key": key,
                    "local_path": str(local_path.relative_to(out_dir)),
                    "size_bytes": size,
                })
                logger.info(f"  ↓ {key} ({size} bytes)")
            except Exception as e:
                logger.error(f"  ✗ 下载失败 {key}: {e}")
                cat_records.append({
                    "oss_key": key,
                    "local_path": None,
                    "error": str(e),
                })
        manifest[category] = cat_records
    return manifest


async def export_airtable_metadata(
    at: AirtableService,
    project_id: str,
    out_dir: Path,
) -> dict[str, Any]:
    """导出项目相关 Airtable 数据为 JSON,返回整理好的 meta dict"""
    logger.info("拉取 Airtable 项目记录...")
    project = await at.get_project(project_id)
    if project is None:
        raise ValueError(f"Airtable 中找不到项目 {project_id}")

    logger.info("拉取素材 / 分镜 / 审核记录...")
    assets, shots, reviews = await asyncio.gather(
        at.get_project_assets(project_id),
        at.get_project_shots(project_id),
        at.get_project_reviews(project_id),
    )

    meta = {
        "project_id": project_id,
        "archived_at": datetime.now().isoformat(timespec="seconds"),
        "project": project,
        "assets": assets,
        "shots": shots,
        "reviews": reviews,
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "metadata.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info(f"已写入 metadata.json ({len(assets)} 素材 / {len(shots)} 分镜 / {len(reviews)} 审核)")
    return meta


def write_readme(
    out_dir: Path,
    meta: dict[str, Any],
    oss_manifest: dict[str, list[dict[str, Any]]],
) -> None:
    project_fields = meta["project"].get("fields", {})
    project_id = meta["project_id"]
    name = project_fields.get("项目名称", "(未命名)")
    status = project_fields.get("状态", "-")
    mode = project_fields.get("模式", "-")
    shots = meta["shots"]

    lines: list[str] = []
    lines.append(f"# ⭐ 优质项目归档 · {name}")
    lines.append("")
    lines.append(f"- **Project ID**: `{project_id}`")
    lines.append(f"- **项目名称**: {name}")
    lines.append(f"- **最新状态**: {status}")
    lines.append(f"- **模式**: {mode}")
    lines.append(f"- **归档时间**: {meta['archived_at']}")
    lines.append(f"- **分镜总数**: {len(shots)}")
    lines.append("")
    lines.append("## 中间素材清单")
    for cat, items in oss_manifest.items():
        lines.append(f"- `{cat}/` : {len(items)} 个文件")
    lines.append("")
    lines.append("## 分镜列表")
    lines.append("")
    lines.append("| # | 原镜头描述 | 新镜头描述 | Prompt 审核 | 视频审核 |")
    lines.append("|---|---|---|---|---|")
    for s in shots:
        f = s.get("fields", {})
        num = f.get("镜头序号", "?")
        orig = (f.get("原镜头描述") or "").replace("|", "/").replace("\n", " ")
        newd = (f.get("新镜头描述") or "").replace("|", "/").replace("\n", " ")
        p_status = f.get("提示词审核状态", "-")
        v_status = f.get("视频审核状态", "-")
        # 截断过长文本便于阅读
        orig = (orig[:60] + "…") if len(orig) > 60 else orig
        newd = (newd[:60] + "…") if len(newd) > 60 else newd
        lines.append(f"| {num} | {orig} | {newd} | {p_status} | {v_status} |")
    lines.append("")
    lines.append("## 各分镜生成 Prompt")
    lines.append("")
    for s in shots:
        f = s.get("fields", {})
        num = f.get("镜头序号", "?")
        prompt = f.get("生成提示词", "") or ""
        lines.append(f"### 镜头 {num}")
        lines.append("")
        lines.append("```")
        lines.append(prompt.strip() or "(空)")
        lines.append("```")
        lines.append("")

    (out_dir / "README.md").write_text("\n".join(lines), encoding="utf-8")
    logger.info("已写入 README.md")


async def mark_as_favorite(at: AirtableService, project_id: str) -> bool:
    """在项目名称前加 ⭐ 前缀(若已加则跳过)。返回是否实际更新。"""
    project = await at.get_project(project_id)
    if not project:
        logger.warning("Airtable 项目不存在,跳过打标")
        return False
    name = project.get("fields", {}).get("项目名称", "")
    if name.startswith(STAR_PREFIX):
        logger.info(f"项目名称已带 ⭐ 前缀,跳过打标: {name}")
        return False
    new_name = f"{STAR_PREFIX} {name}".strip()
    await at.update_project(project_id, {"项目名称": new_name})
    logger.info(f"已在 Airtable 中标记为优质样本: {name} → {new_name}")
    return True


async def main() -> None:
    parser = argparse.ArgumentParser(description="归档优质项目样本")
    parser.add_argument("project_id", help="Airtable 项目 ID (rec开头)")
    parser.add_argument("--no-label", action="store_true", help="跳过 Airtable 打标")
    parser.add_argument("--with-videos", action="store_true", help="同时下载视频 (体积大)")
    parser.add_argument("--out", default="archive", help="归档根目录 (默认 archive)")
    args = parser.parse_args()

    project_id: str = args.project_id
    out_root = ROOT / args.out / project_id
    out_root.mkdir(parents=True, exist_ok=True)
    logger.info(f"归档目录: {out_root}")

    at = _build_airtable()
    oss = _build_oss()

    # 1. 导出 Airtable 元数据
    meta = await export_airtable_metadata(at, project_id, out_root)

    # 2. 下载 OSS 素材
    prefixes = INTERMEDIATE_PREFIXES + (VIDEO_PREFIXES if args.with_videos else [])
    manifest = await archive_oss_assets(oss, project_id, out_root, prefixes)
    (out_root / "oss_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # 3. 生成 README
    write_readme(out_root, meta, manifest)

    # 4. Airtable 打标
    if not args.no_label:
        try:
            await mark_as_favorite(at, project_id)
        except Exception as e:
            logger.error(f"Airtable 打标失败(不影响归档): {e}")

    logger.info(f"✅ 归档完成: {out_root}")


if __name__ == "__main__":
    asyncio.run(main())
