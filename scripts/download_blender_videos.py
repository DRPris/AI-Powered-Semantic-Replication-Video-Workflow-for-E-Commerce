"""下载指定项目的镜头视频到本地供预览。

用法：
    PROJECT_ID=recXXXXXXXXXXXXXX SHOTS=7 OUT_DIR=/tmp/preview \\
        python scripts/download_blender_videos.py
或：
    python scripts/download_blender_videos.py recXXXXXXXXXXXXXX 7
"""
from __future__ import annotations
import asyncio, os, sys
from pathlib import Path

_SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVICE_DIR))
_env_path = _SERVICE_DIR / ".env"
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

import httpx
from config import settings
from services.oss_service import OSSService

# 项目 Record ID 与镜头数可通过命令行参数或环境变量传入
PROJECT_ID = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("PROJECT_ID", "")
SHOTS_COUNT = int(sys.argv[2] if len(sys.argv) > 2 else os.environ.get("SHOTS", "7"))
OUT_DIR = Path(os.environ.get("OUT_DIR", "/tmp/shot_videos"))
if not PROJECT_ID:
    raise SystemExit("请传入 PROJECT_ID：python scripts/download_blender_videos.py <record_id>")


async def main():
    oss = OSSService(
        access_key_id=settings.OSS_ACCESS_KEY_ID,
        access_key_secret=settings.OSS_ACCESS_KEY_SECRET,
        bucket_name=settings.OSS_BUCKET_NAME,
        endpoint=settings.OSS_ENDPOINT,
        cdn_domain=getattr(settings, "OSS_CDN_DOMAIN", ""),
    )
    OUT_DIR.mkdir(exist_ok=True)
    async with httpx.AsyncClient(timeout=180.0) as client:
        for n in range(1, SHOTS_COUNT + 1):
            key = f"videos/{PROJECT_ID}/shot_{n}.mp4"
            url = oss.get_signed_url(key, expires=3600)
            print(f"downloading shot_{n}...")
            r = await client.get(url)
            r.raise_for_status()
            p = OUT_DIR / f"shot_{n}.mp4"
            p.write_bytes(r.content)
            print(f"  ✓ {p}  ({p.stat().st_size/1024:.1f} KB)")
    print(f"\n✅ 全部下载到 {OUT_DIR}")


if __name__ == "__main__":
    asyncio.run(main())
