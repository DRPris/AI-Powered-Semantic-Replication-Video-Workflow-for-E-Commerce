#!/usr/bin/env python3
"""
产品图 → 抠图 → 白底图 → 3D 模型 完整流程脚本

流程:
1. 下载原始产品图
2. 抠图（去除背景）- 优先使用 rembg，失败时使用 remove.bg API
3. 生成白底产品图（透明图 + 白色背景）
4. 上传到 OSS
5. 调用 Tripo3D 生成 3D 模型
6. 下载并上传结果到 OSS
"""

import sys
import os
import time
import asyncio
import httpx
from pathlib import Path
from io import BytesIO

# 添加项目根目录到 Python 路径（基于本脚本位置推导）
_SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVICE_DIR))
from services.oss_service import OSSService
from services.image_utils import remove_background
from config import settings

# ==================== 配置 ====================
# fal.ai API Key：在 https://fal.ai/dashboard/keys 申请，写入 .env 中的 FAL_KEY
FAL_KEY = os.environ.get("FAL_KEY", "")
API_ENDPOINT = "https://fal.run/tripo3d/tripo/v2.5/image-to-3d"
FAL_STORAGE_INITIATE = "https://rest.fal.ai/storage/upload/initiate"
# Airtable 项目 Record ID：通过命令行参数或环境变量 PROJECT_ID 指定
PROJECT_ID = os.environ.get("PROJECT_ID", "")
if not FAL_KEY:
    raise RuntimeError("FAL_KEY 未配置，请在 .env 中设置 FAL_KEY=<your-fal-key>")
if not PROJECT_ID:
    raise RuntimeError("PROJECT_ID 未配置，请通过环境变量传入 Airtable 项目 Record ID")

# 输入图片 URL
# OSS 图片路径（从 URL 中提取）
# 原始 URL: http://semantic-video-recreation.oss-cn-beijing.aliyuncs.com/test%2Fblender_cup_product.jpg
OSS_IMAGE_KEY = "test/blender_cup_product.jpg"

# 输出目录
OUTPUT_DIR = Path(__file__).parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

# 请求超时
REQUEST_TIMEOUT = 900  # 秒（15分钟）


def get_oss_service() -> OSSService:
    """初始化 OSS 服务"""
    return OSSService(
        access_key_id=settings.OSS_ACCESS_KEY_ID,
        access_key_secret=settings.OSS_ACCESS_KEY_SECRET,
        bucket_name=settings.OSS_BUCKET_NAME,
        endpoint=settings.OSS_ENDPOINT,
        cdn_domain=settings.OSS_CDN_DOMAIN,
    )


async def download_image(client: httpx.AsyncClient, url: str, save_path: Path) -> bytes:
    """下载图片并返回字节数据"""
    print(f"📥 下载图片: {url}")
    
    response = await client.get(url, follow_redirects=True)
    response.raise_for_status()
    
    save_path.write_bytes(response.content)
    print(f"   已保存: {save_path} ({len(response.content)} bytes)")
    
    return response.content


async def download_image_from_oss(
    client: httpx.AsyncClient,
    oss_service: OSSService,
    oss_key: str,
    save_path: Path
) -> bytes:
    """
    使用 OSS 签名 URL 下载图片
    
    Args:
        client: HTTP 客户端
        oss_service: OSS 服务实例
        oss_key: OSS 文件路径（如 "test/blender_cup_product.jpg"）
        save_path: 本地保存路径
    
    Returns:
        图片字节数据
    """
    print(f"📥 获取 OSS 签名 URL...")
    signed_url = oss_service.get_signed_url(oss_key, expires=3600)
    print(f"   签名 URL: {signed_url[:80]}...")
    
    print(f"📥 下载图片...")
    response = await client.get(signed_url, follow_redirects=True)
    response.raise_for_status()
    
    save_path.write_bytes(response.content)
    print(f"   已保存: {save_path} ({len(response.content)} bytes)")
    
    return response.content


async def remove_background_and_create_white_bg(
    image_bytes: bytes,
    output_path: Path,
    removebg_api_key: str = ""
) -> bytes:
    """
    抠图并生成白底产品图
    
    Args:
        image_bytes: 原始图片字节
        output_path: 输出文件路径
        removebg_api_key: remove.bg API 密钥（可选）
    
    Returns:
        白底产品图字节数据
    """
    print("\n🔧 步骤 1: 抠图（去除背景）...")
    
    # 使用 image_utils 中的 remove_background 函数
    # 它会优先尝试 rembg，失败时降级到 remove.bg API
    transparent_bytes = await remove_background(image_bytes, removebg_api_key)
    
    # 检查是否成功去背景（如果返回的是原图，说明失败了）
    if transparent_bytes == image_bytes:
        print("⚠️  抠图失败，将使用原图继续...")
        transparent_bytes = image_bytes
    else:
        print(f"   ✓ 抠图完成，输出大小: {len(transparent_bytes)} bytes")
    
    print("\n🎨 步骤 2: 生成白底产品图...")
    
    # 使用 Pillow 在透明图下面添加白色背景
    from PIL import Image
    
    # 打开透明 PNG
    img = Image.open(BytesIO(transparent_bytes)).convert("RGBA")
    
    # 创建白色背景
    white_bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
    
    # 将透明图合成到白色背景上
    composite = Image.alpha_composite(white_bg, img)
    
    # 转换为 RGB（去除 alpha 通道）
    final_img = composite.convert("RGB")
    
    # 保存为 PNG
    buffer = BytesIO()
    final_img.save(buffer, format="PNG", quality=95)
    white_bg_bytes = buffer.getvalue()
    
    # 保存到文件
    output_path.write_bytes(white_bg_bytes)
    print(f"   ✓ 白底图已保存: {output_path} ({len(white_bg_bytes)} bytes)")
    
    return white_bg_bytes


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


async def generate_3d_model(client: httpx.AsyncClient, fal_image_url: str) -> dict:
    """
    使用 fal.ai subscribe 模式生成 3D 模型
    
    Args:
        client: HTTP 客户端
        fal_image_url: fal.ai 图片 URL
    
    Returns:
        任务结果字典
    """
    headers = {
        "Authorization": f"Key {FAL_KEY}",
        "Content-Type": "application/json",
    }
    
    # 提交生成任务
    payload = {
        "image_url": fal_image_url,
        "texture": "standard",
        "pbr": True,
    }
    
    print(f"\n🚀 提交 3D 生成任务（subscribe 模式，阻塞等待结果）...")
    print(f"   这可能需要几分钟时间（包含排队和处理）...")
    
    response = await client.post(API_ENDPOINT, headers=headers, json=payload)
    
    # 打印响应详情以便调试
    if response.status_code != 200:
        print(f"❌ 请求失败: {response.status_code}")
        print(f"   响应内容: {response.text}")
    
    response.raise_for_status()
    
    data = response.json()
    print(f"✅ 3D 生成任务完成！")
    
    return data


async def download_file(client: httpx.AsyncClient, url: str, save_path: Path) -> Path:
    """下载文件到本地"""
    print(f"📥 下载: {url[:80]}...")
    
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
    print("=" * 70)
    print("🎨 产品图 → 抠图 → 白底图 → 3D 模型 完整流程")
    print("=" * 70)
    
    # 检查代理设置
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if proxy:
        print(f"🌐 使用代理: {proxy}")
    else:
        print("🌐 无代理设置")
    
    # 初始化 OSS 服务
    oss_service = get_oss_service()
    
    # 文件路径配置
    original_image_path = OUTPUT_DIR / "original_product.jpg"
    white_bg_path = OUTPUT_DIR / "product_white_bg.png"
    model_path = OUTPUT_DIR / "model.glb"
    preview_path = OUTPUT_DIR / "preview.png"
    
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        # ==================== 步骤 1: 下载原始产品图 ====================
        print("\n" + "-" * 70)
        print("📋 步骤 1: 下载原始产品图")
        print("-" * 70)
        
        original_bytes = await download_image_from_oss(
            client, oss_service, OSS_IMAGE_KEY, original_image_path
        )
        
        # ==================== 步骤 2: 抠图并生成白底图 ====================
        print("\n" + "-" * 70)
        print("📋 步骤 2: 抠图并生成白底产品图")
        print("-" * 70)
        
        removebg_api_key = settings.REMOVEBG_API_KEY
        white_bg_bytes = await remove_background_and_create_white_bg(
            original_bytes,
            white_bg_path,
            removebg_api_key
        )
        
        # ==================== 步骤 3: 上传白底图到 OSS ====================
        print("\n" + "-" * 70)
        print("📋 步骤 3: 上传白底图到 OSS")
        print("-" * 70)
        
        white_bg_oss_key = f"3d_models/{PROJECT_ID}/product_white_bg.png"
        white_bg_oss_url = await upload_to_oss(oss_service, white_bg_path, white_bg_oss_key)
        
        # ==================== 步骤 4: 上传图片到 fal.ai ====================
        print("\n" + "-" * 70)
        print("📋 步骤 4: 上传图片到 fal.ai storage")
        print("-" * 70)
        
        fal_image_url = await upload_to_fal_storage(client, white_bg_path)
        
        # ==================== 步骤 5: 生成 3D 模型 ====================
        print("\n" + "-" * 70)
        print("📋 步骤 5: 调用 Tripo3D 生成 3D 模型")
        print("-" * 70)
        
        result = await generate_3d_model(client, fal_image_url)
        
        print(f"\n📋 任务结果:")
        print(f"   Task ID: {result.get('task_id', 'N/A')}")
        
        # 获取下载链接
        model_mesh = result.get("model_mesh")
        pbr_model = result.get("pbr_model")
        rendered_image = result.get("rendered_image")
        
        # 优先使用 model_mesh，如果没有则使用 pbr_model
        model_file = model_mesh or pbr_model
        
        if not model_file:
            raise ValueError("结果中缺少模型文件字段（model_mesh 和 pbr_model 都为空）")
        
        # 处理嵌套结构
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
        
        # ==================== 步骤 6: 下载文件 ====================
        print("\n" + "-" * 70)
        print("📋 步骤 6: 下载生成的文件")
        print("-" * 70)
        
        await download_file(client, model_url, model_path)
        
        if preview_url:
            await download_file(client, preview_url, preview_path)
        
        # ==================== 步骤 7: 上传到 OSS ====================
        print("\n" + "-" * 70)
        print("📋 步骤 7: 上传结果到 OSS")
        print("-" * 70)
        
        model_oss_key = f"3d_models/{PROJECT_ID}/model.glb"
        preview_oss_key = f"3d_models/{PROJECT_ID}/preview.png"
        
        model_oss_url = await upload_to_oss(oss_service, model_path, model_oss_key)
        
        preview_oss_url = None
        if preview_url and preview_path.exists():
            preview_oss_url = await upload_to_oss(oss_service, preview_path, preview_oss_key)
        
        # ==================== 完成 ====================
        print("\n" + "=" * 70)
        print("✅ 全部完成！")
        print("=" * 70)
        
        print(f"\n📁 本地文件:")
        print(f"   原始图: {original_image_path}")
        print(f"   白底图: {white_bg_path}")
        print(f"   模型:   {model_path}")
        if preview_path.exists():
            print(f"   预览:   {preview_path}")
        
        print(f"\n☁️  OSS 文件:")
        print(f"   白底图: {white_bg_oss_url}")
        print(f"   模型:   {model_oss_url}")
        if preview_oss_url:
            print(f"   预览:   {preview_oss_url}")
        
        print(f"\n🔗 原始 URL:")
        print(f"   模型: {model_url}")
        if preview_url:
            print(f"   预览: {preview_url}")
        
        # 验证预览图质量
        print(f"\n🔍 预览图验证:")
        if preview_path.exists():
            print(f"   ✓ 预览图已生成: {preview_path}")
            print(f"   请查看该文件以验证 3D 模型质量")
        else:
            print(f"   ⚠️  预览图未生成")
    
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
