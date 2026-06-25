"""
图像处理工具服务
提供背景去除（抠图）等图像预处理功能

策略：rembg 本地处理为主，remove.bg API 作为降级方案
"""

import base64
import io
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# rembg 懒加载（避免启动时加载大模型）
_rembg_session = None


def _get_rembg_session():
    """
    懒加载 rembg session，避免启动时加载模型
    """
    global _rembg_session
    if _rembg_session is None:
        try:
            from rembg import new_session
            _rembg_session = new_session("u2net")
            logger.info("rembg session initialized with u2net model")
        except ImportError:
            logger.warning("rembg not installed, background removal will use API fallback")
            _rembg_session = False  # 标记为不可用
        except Exception as e:
            logger.error(f"Failed to initialize rembg session: {e}")
            _rembg_session = False
    return _rembg_session if _rembg_session is not False else None


async def remove_background_rembg(image_bytes: bytes) -> Optional[bytes]:
    """
    使用 rembg 本地模型去除图片背景
    
    Args:
        image_bytes: 原始图片字节数据
        
    Returns:
        去除背景后的 PNG 图片字节数据，失败返回 None
    """
    session = _get_rembg_session()
    if session is None:
        logger.warning("rembg not available, skipping local background removal")
        return None
    
    try:
        from rembg import remove
        
        # rembg.remove 是同步函数，在异步上下文中直接调用
        # 对于 CPU 密集型操作，生产环境可考虑使用 run_in_executor
        result = remove(image_bytes, session=session)
        
        logger.info(f"rembg background removal successful, output size: {len(result)} bytes")
        return result
        
    except Exception as e:
        logger.error(f"rembg background removal failed: {e}")
        return None


async def remove_background_api(
    image_bytes: bytes, 
    api_key: str,
    api_url: str = "https://api.remove.bg/v1.0/removebg"
) -> Optional[bytes]:
    """
    使用 remove.bg API 去除图片背景（降级方案）
    
    Args:
        image_bytes: 原始图片字节数据
        api_key: remove.bg API 密钥
        api_url: API 地址
        
    Returns:
        去除背景后的 PNG 图片字节数据，失败返回 None
    """
    if not api_key:
        logger.warning("remove.bg API key not configured, skipping API background removal")
        return None
    
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                api_url,
                files={"image_file": ("image.png", image_bytes, "image/png")},
                data={"size": "auto"},
                headers={"X-Api-Key": api_key},
            )
            
            if response.status_code == 200:
                logger.info(f"remove.bg API background removal successful, output size: {len(response.content)} bytes")
                return response.content
            else:
                logger.error(f"remove.bg API failed with status {response.status_code}: {response.text}")
                return None
                
    except Exception as e:
        logger.error(f"remove.bg API request failed: {e}")
        return None


async def remove_background(
    image_bytes: bytes,
    removebg_api_key: str = "",
) -> bytes:
    """
    去除图片背景（双保障策略）
    
    优先使用 rembg 本地处理，失败时降级到 remove.bg API
    如果都失败，返回原始图片
    
    Args:
        image_bytes: 原始图片字节数据
        removebg_api_key: remove.bg API 密钥（可选）
        
    Returns:
        处理后的图片字节数据（去背景或原图）
    """
    # 1. 尝试 rembg 本地处理
    result = await remove_background_rembg(image_bytes)
    if result:
        return result
    
    # 2. 降级到 remove.bg API
    if removebg_api_key:
        result = await remove_background_api(image_bytes, removebg_api_key)
        if result:
            return result
    
    # 3. 都失败，返回原图
    logger.warning("All background removal methods failed, using original image")
    return image_bytes


def image_bytes_to_base64_data_uri(image_bytes: bytes, mime_type: str = "image/png") -> str:
    """
    将图片字节转换为 base64 data URI 格式
    
    Args:
        image_bytes: 图片字节数据
        mime_type: MIME 类型
        
    Returns:
        data URI 字符串，如 "data:image/png;base64,xxxxx"
    """
    b64_data = base64.b64encode(image_bytes).decode("utf-8")
    return f"data:{mime_type};base64,{b64_data}"


def standardize_image_to_9_16(image_bytes: bytes, target_width: int = 720, target_height: int = 1280) -> bytes:
    """
    将图片标准化为 9:16 尺寸。
    产品居中，白色背景填充。
    
    策略：
    1. 按比例缩放使产品完整填入目标区域
    2. 白色背景填充多余空间（不裁剪产品）
    """
    from PIL import Image
    
    img = Image.open(io.BytesIO(image_bytes))
    
    # 如果有 alpha 通道，先合成到白色背景上
    if img.mode == 'RGBA':
        background = Image.new('RGB', img.size, (255, 255, 255))
        background.paste(img, mask=img.split()[3])
        img = background
    elif img.mode != 'RGB':
        img = img.convert('RGB')
    
    # 按比例缩放使整个图片能放入目标区域（不裁剪）
    ratio = min(target_width / img.width, target_height / img.height)
    new_width = int(img.width * ratio)
    new_height = int(img.height * ratio)
    img_resized = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
    
    # 居中放置在白色画布上
    canvas = Image.new("RGB", (target_width, target_height), (255, 255, 255))
    offset_x = (target_width - new_width) // 2
    offset_y = (target_height - new_height) // 2
    canvas.paste(img_resized, (offset_x, offset_y))
    
    output = io.BytesIO()
    canvas.save(output, format="PNG")
    return output.getvalue()


def base64_data_uri_to_bytes(data_uri: str) -> tuple[bytes, str]:
    """
    将 base64 data URI 转换为字节数据
    
    Args:
        data_uri: data URI 字符串
        
    Returns:
        (图片字节数据, MIME 类型)
    """
    # 解析 data URI: data:image/png;base64,xxxx
    header, b64_data = data_uri.split(",", 1)
    mime_type = header.split(";")[0].split(":")[1]
    image_bytes = base64.b64decode(b64_data)
    return image_bytes, mime_type
