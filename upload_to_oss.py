"""快速上传本地文件到 OSS 的辅助脚本"""
import asyncio
import sys
import os

# 加载环境变量
from dotenv import load_dotenv
load_dotenv(override=True)

from services.oss_service import OSSService
from config import settings


async def upload_file(local_path: str, oss_key: str):
    oss = OSSService(
        access_key_id=settings.OSS_ACCESS_KEY_ID,
        access_key_secret=settings.OSS_ACCESS_KEY_SECRET,
        bucket_name=settings.OSS_BUCKET_NAME,
        endpoint=settings.OSS_ENDPOINT,
        cdn_domain=settings.OSS_CDN_DOMAIN,
    )
    
    with open(local_path, "rb") as f:
        data = f.read()
    
    # 判断 content_type
    ext = os.path.splitext(local_path)[1].lower()
    content_type_map = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".webp": "image/webp",
        ".mp4": "video/mp4", ".mov": "video/quicktime",
    }
    content_type = content_type_map.get(ext, "application/octet-stream")
    
    url = await oss.upload_bytes(
        data=data, oss_key=oss_key, content_type=content_type, expires=86400 * 30
    )
    print(f"Uploaded: {local_path}")
    print(f"URL: {url}")
    return url


async def main():
    if len(sys.argv) < 3:
        print("Usage: python3 upload_to_oss.py <local_path> <oss_key>")
        sys.exit(1)
    
    await upload_file(sys.argv[1], sys.argv[2])


if __name__ == "__main__":
    asyncio.run(main())
