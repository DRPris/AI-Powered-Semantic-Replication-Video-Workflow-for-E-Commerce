#!/usr/bin/env python3
"""
Tripo3D v2.5 3D 模型生成脚本
使用 fal.ai 的 Tripo3D API 从产品图生成 GLB 格式 3D 模型
"""

import sys
import os
import time
import asyncio
import httpx
from pathlib import Path

# 添加项目根目录到 Python 路径（基于本脚本位置推导）
_SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVICE_DIR))
from services.oss_service import OSSService
from config import settings

# ==================== 配置 ====================
# fal.ai API Key：在 https://fal.ai/dashboard/keys 申请，写入 .env 中的 FAL_KEY
FAL_KEY = os.environ.get("FAL_KEY", "")
# fal.ai 实时端点（subscribe 模式，阻塞等待结果）
API_ENDPOINT = "https://fal.run/tripo3d/tripo/v2.5/image-to-3d"
# fal.ai 文件上传端点
FAL_STORAGE_INITIATE = "https://rest.fal.ai/storage/upload/initiate"
# Airtable 项目 Record ID：通过命令行参数或环境变量 PROJECT_ID 指定
PROJECT_ID = os.environ.get("PROJECT_ID", "")
if not FAL_KEY:
    raise RuntimeError("FAL_KEY 未配置，请在 .env 中设置 FAL_KEY=<your-fal-key>")
if not PROJECT_ID:
    raise RuntimeError("PROJECT_ID 未配置，请通过环境变量传入 Airtable 项目 Record ID")
IMAGE_URL = "http://semantic-video-recreation.oss-cn-beijing.aliyuncs.com/test%2Fblender_cup_product.jpg"

# 输出目录
OUTPUT_DIR = Path(__file__).parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

# 请求超时（subscribe 模式需要较长时间）
REQUEST_TIMEOUT = 900  # 秒（15分钟，包含排队和处理时间）


def get_oss_service() -> OSSService:
    """初始化 OSS 服务"""
    return OSSService(
        access_key_id=settings.OSS_ACCESS_KEY_ID,
        access_key_secret=settings.OSS_ACCESS_KEY_SECRET,
        bucket_name=settings.OSS_BUCKET_NAME,
        endpoint=settings.OSS_ENDPOINT,
        cdn_domain=settings.OSS_CDN_DOMAIN,
    )


async def upload_to_fal_storage(client: httpx.AsyncClient, image_path: Path) -> str:
    """
    上传图片到 fal.ai storage
    
    Returns:
        fal.ai 文件访问 URL
    """
    headers = {
        "Authorization": f"Key {FAL_KEY}",
        "Content-Type": "application/json",
    }
    
    print(f"📤 上传图片到 fal.ai storage...")
    
    # 检测文件类型
    content_type = "image/jpeg"
    if image_path.suffix.lower() == ".png":
        content_type = "image/png"
    elif image_path.suffix.lower() == ".webp":
        content_type = "image/webp"
    
    # 1. 初始化上传
    file_name = image_path.name
    initiate_payload = {
        "file_name": file_name,
        "content_type": content_type,
    }
    
    response = await client.post(
        FAL_STORAGE_INITIATE,
        headers=headers,
        json=initiate_payload,
    )
    
    if response.status_code != 200:
        print(f"❌ 初始化上传失败: {response.status_code}")
        print(f"   响应内容: {response.text}")
        response.raise_for_status()
    
    initiate_data = response.json()
    upload_url = initiate_data.get("upload_url")
    file_url = initiate_data.get("file_url")
    
    if not upload_url or not file_url:
        raise ValueError(f"初始化上传响应缺少必要字段: {initiate_data}")
    
    print(f"   上传 URL: {upload_url[:60]}...")
    print(f"   文件 URL: {file_url[:60]}...")
    
    # 2. 上传文件内容
    with open(image_path, "rb") as f:
        image_data = f.read()
    
    upload_response = await client.put(
        upload_url,
        content=image_data,
        headers={"Content-Type": content_type},
    )
    
    if upload_response.status_code not in (200, 204):
        print(f"❌ 上传文件内容失败: {upload_response.status_code}")
        print(f"   响应内容: {upload_response.text}")
        upload_response.raise_for_status()
    
    print(f"   上传成功！")
    
    return file_url


async def download_image_from_oss(client: httpx.AsyncClient, url: str, save_path: Path) -> Path:
    """从 OSS 下载图片到本地"""
    print(f"📥 下载图片: {url}")
    
    response = await client.get(url, follow_redirects=True)
    response.raise_for_status()
    
    save_path.write_bytes(response.content)
    print(f"   已保存: {save_path} ({len(response.content)} bytes)")
    
    return save_path


async def generate_3d_model(
    client: httpx.AsyncClient, 
    oss_service: OSSService,
    oss_key: str,
) -> dict:
    """
    使用 fal.ai subscribe 模式生成 3D 模型
    该模式会阻塞等待直到任务完成（包含排队时间）
    
    Args:
        client: HTTP 客户端
        oss_service: OSS 服务实例
        oss_key: OSS 文件路径（如 "test/blender_cup_product.jpg"）
    
    Returns:
        任务结果字典
    """
    headers = {
        "Authorization": f"Key {FAL_KEY}",
        "Content-Type": "application/json",
    }
    
    # 1. 获取 OSS 签名 URL（1小时有效期）
    print(f"📥 获取 OSS 签名 URL...")
    signed_url = oss_service.get_signed_url(oss_key, expires=3600)
    print(f"   签名 URL: {signed_url[:80]}...")
    
    # 2. 从 OSS 下载图片
    local_image_path = OUTPUT_DIR / "input_image.jpg"
    await download_image_from_oss(client, signed_url, local_image_path)
    
    # 3. 上传到 fal.ai storage
    fal_image_url = await upload_to_fal_storage(client, local_image_path)
    
    # 4. 提交生成任务
    payload = {
        "image_url": fal_image_url,
        "texture": "standard",
        "pbr": True,
    }
    
    print(f"🚀 提交生成任务（使用 subscribe 模式，阻塞等待结果）...")
    print(f"   这可能需要几分钟时间（包含排队和处理）...")
    
    response = await client.post(API_ENDPOINT, headers=headers, json=payload)
    
    # 打印响应详情以便调试
    if response.status_code != 200:
        print(f"❌ 请求失败: {response.status_code}")
        print(f"   响应内容: {response.text}")
    
    response.raise_for_status()
    
    data = response.json()
    print(f"✅ 任务完成！")
    
    return data


async def download_file(client: httpx.AsyncClient, url: str, save_path: Path) -> Path:
    """下载文件到本地"""
    print(f"📥 下载: {url}")
    
    response = await client.get(url, follow_redirects=True)
    response.raise_for_status()
    
    save_path.write_bytes(response.content)
    print(f"   已保存: {save_path} ({len(response.content)} bytes)")
    
    return save_path


async def upload_to_oss(oss_service: OSSService, local_path: Path, oss_key: str) -> str:
    """上传文件到 OSS"""
    print(f"📤 上传到 OSS: {oss_key}")
    
    # 根据文件类型设置 content_type
    content_type = None
    if local_path.suffix == ".glb":
        content_type = "model/gltf-binary"
    elif local_path.suffix == ".png":
        content_type = "image/png"
    
    url = await oss_service.upload_file(
        local_path=str(local_path),
        oss_key=oss_key,
        content_type=content_type,
        expires=7200,
    )
    
    print(f"   OSS URL: {url}")
    return url


async def main():
    """主函数"""
    print("=" * 60)
    print("🎨 Tripo3D v2.5 3D 模型生成")
    print("=" * 60)
    
    # 检查代理设置
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if proxy:
        print(f"🌐 使用代理: {proxy}")
    else:
        print("🌐 无代理设置")
    
    # 初始化 OSS 服务
    oss_service = get_oss_service()
    
    # OSS 图片路径（从 URL 中提取）
    OSS_IMAGE_KEY = "test/blender_cup_product.jpg"
    
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        # 1. 生成 3D 模型（subscribe 模式，阻塞等待）
        result = await generate_3d_model(client, oss_service, OSS_IMAGE_KEY)
        
        print("\n📋 任务结果:")
        print(f"   Task ID: {result.get('task_id', 'N/A')}")
        
        # 3. 获取下载链接
        # model_mesh 可能是 null，使用 pbr_model 作为替代
        model_mesh = result.get("model_mesh")
        pbr_model = result.get("pbr_model")
        rendered_image = result.get("rendered_image")
        
        # 优先使用 model_mesh，如果没有则使用 pbr_model
        model_file = model_mesh or pbr_model
        
        if not model_file:
            raise ValueError("结果中缺少模型文件字段（model_mesh 和 pbr_model 都为空）")
        
        # 处理嵌套结构（pbr_model 可能是对象，包含 url 字段）
        if isinstance(model_file, dict):
            model_url = model_file.get("url")
        else:
            model_url = model_file
        
        print(f"\n🔗 模型文件 URL: {model_url}")
        
        # 处理预览图 URL
        preview_url = None
        if rendered_image:
            if isinstance(rendered_image, dict):
                preview_url = rendered_image.get("url")
            else:
                preview_url = rendered_image
            print(f"🔗 预览图 URL: {preview_url}")
        
        # 4. 下载文件
        print("\n📥 下载文件...")
        model_path = OUTPUT_DIR / "model.glb"
        preview_path = OUTPUT_DIR / "preview.png"
        
        await download_file(client, model_url, model_path)
        
        if preview_url:
            await download_file(client, preview_url, preview_path)
        
        # 5. 上传到 OSS
        print("\n📤 上传到 OSS...")
        model_oss_key = f"3d_models/{PROJECT_ID}/model.glb"
        preview_oss_key = f"3d_models/{PROJECT_ID}/preview.png"
        
        model_oss_url = await upload_to_oss(oss_service, model_path, model_oss_key)
        
        preview_oss_url = None
        if preview_url and preview_path.exists():
            preview_oss_url = await upload_to_oss(oss_service, preview_path, preview_oss_key)
        
        # 6. 打印结果
        print("\n" + "=" * 60)
        print("✅ 完成！")
        print("=" * 60)
        print(f"\n📁 本地文件:")
        print(f"   模型: {model_path}")
        if preview_path.exists():
            print(f"   预览: {preview_path}")
        
        print(f"\n☁️  OSS 文件:")
        print(f"   模型: {model_oss_url}")
        if preview_oss_url:
            print(f"   预览: {preview_oss_url}")
        
        print(f"\n🔗 原始 URL:")
        print(f"   模型: {model_url}")
        if preview_url:
            print(f"   预览: {preview_url}")
    
    return result


if __name__ == "__main__":
    try:
        result = asyncio.run(main())
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ 错误: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
