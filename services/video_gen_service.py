"""
视频生成服务封装
通过火山引擎方舟平台直连 Seedance 2.0 API
（保留 KIE AI 作为备选）
"""

import httpx
import asyncio
import logging
import cv2
import tempfile
import os
import shutil
import uuid
from enum import Enum
from typing import Optional, List, Union
from pathlib import Path

from config import settings

logger = logging.getLogger(__name__)


class VideoModel(str, Enum):
    """支持的视频生成模型"""
    KLING_V3 = "kling-3.0/video"  # Kling 3.0 视频生成
    KLING_V2_6 = "kling-2.6/video"  # Kling 2.6 视频生成
    KLING_V2_1 = "kling/v2-1-standard"  # Kling 2.1 标准版
    SEEDANCE_2 = "doubao-seedance-2-0-260128"  # Seedance 2.0（火山方舟直连）
    SEEDANCE_1_5 = "bytedance/seedance-1.5-pro"  # Seedance 1.5 Pro（KIE AI）


class VideoGenService:
    """视频生成服务（默认火山方舟 Seedance 2.0 直连，备选 KIE AI）"""
    
    def __init__(self, api_key: str, base_url: str = "https://ark.cn-beijing.volces.com/api/v3"):
        self.api_key = api_key
        self.base_url = base_url
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        # 静态文件目录（用于存储提取的帧图片）
        self.frames_dir = Path("static/frames")
        self.frames_dir.mkdir(parents=True, exist_ok=True)
    
    async def generate_video(
        self,
        model: str = settings.SEEDANCE_MODEL,
        prompt: str = "",
        image_urls: Optional[List[str]] = None,
        first_frame_url: Optional[str] = None,
        last_frame_url: Optional[str] = None,
        duration: int = 5,
        aspect_ratio: str = "9:16",
        negative_prompt: str = "",
        mode: str = "std",
    ) -> str:
        """
        提交视频生成任务。
        默认使用火山方舟 Seedance 2.0 直连 API。
            
        Args:
            model: 模型名称 (doubao-seedance-2-0-260128, ep-xxx, kling-3.0/video 等)
            prompt: 生成提示词
            image_urls: 参考图片 URL 列表（图生视频模式）
            first_frame_url: 首帧图片 URL（首尾帧衍接用）
            last_frame_url: 尾帧图片 URL（首尾帧衍接用）
            duration: 视频时长（秒）
            aspect_ratio: 宽高比
            negative_prompt: 负面提示词（Seedance 直连不支持，会拼接到 prompt 末尾）
            mode: 生成模式 (std/pro)，Seedance 直连时忽略
            
        Returns:
            task_id: 异步任务ID
        """
        # 确保 model 是字符串值
        model_str = model.value if isinstance(model, VideoModel) else str(model)
            
        # 确认日志：打印最终使用的模型
        logger.info(f"[VideoGen] 生成视频使用模型: {model_str}, 原始model参数: {model}")
            
        # 判断是否为 Seedance 模型（参数格式与 Kling 不同）
        # 支持：doubao-seedance-*（标准模型名）、ep-*（推理接入点）、bytedance/seedance-*（KIE AI）
        is_seedance = "seedance" in model_str.lower() or model_str.startswith("ep-")
            
        if is_seedance:
            # Seedance 2.0 直连参数格式（包括 ep- 推理接入点）
            is_seedance_2 = ("doubao-seedance" in model_str.lower()) or model_str.startswith("ep-")
            if is_seedance_2:
                # 处理 negative_prompt：Seedance 直连不支持，拼接到 prompt 末尾
                final_prompt = prompt
                if negative_prompt:
                    final_prompt = f"{prompt}\nAvoid: {negative_prompt}"
                    
                input_payload = {
                    "prompt": final_prompt,
                    "duration": max(4, min(15, duration)),  # Seedance 2.0: 4-15秒，数字类型
                    "aspect_ratio": aspect_ratio,
                    "generate_audio": False,
                    "resolution": settings.SEEDANCE_RESOLUTION,
                    "nsfw_checker": False,
                }
                if first_frame_url:
                    input_payload["first_frame_url"] = first_frame_url
                if last_frame_url:
                    input_payload["last_frame_url"] = last_frame_url
                # Seedance 2.0: reference_image_urls 与 first/last_frame_url 互斥
                if image_urls and not first_frame_url and not last_frame_url:
                    input_payload["reference_image_urls"] = image_urls
            else:
                # Seedance 1.5 (KIE AI): duration 为字符串, 图片用 input_urls
                input_payload = {
                    "prompt": prompt,
                    "duration": str(max(8, duration)),
                    "aspect_ratio": aspect_ratio,
                    "fixed_lens": False,
                    "generate_audio": False,
                    "nsfw_checker": False,
                }
                if first_frame_url:
                    input_payload["input_urls"] = [first_frame_url]
                elif image_urls:
                    input_payload["input_urls"] = image_urls
        else:
            # Kling 参数格式：duration 为字符串，图片用 image_urls
            input_payload = {
                "prompt": prompt,
                "duration": str(duration),
                "aspect_ratio": aspect_ratio,
                "mode": mode,
                "multi_shots": False,
                "sound": False,
            }
            if first_frame_url:
                input_payload["image_urls"] = [first_frame_url]
            elif image_urls:
                input_payload["image_urls"] = image_urls
            if last_frame_url:
                input_payload["last_frame_url"] = last_frame_url
            if negative_prompt:
                input_payload["negative_prompt"] = negative_prompt
            
        # 根据模型选择端点和 payload 格式
        is_seedance_2_direct = is_seedance and (("doubao-seedance" in model_str.lower()) or model_str.startswith("ep-"))
        
        if is_seedance_2_direct:
            # Seedance 2.0 直连：content + extra 格式
            content_items = []
            if first_frame_url:
                content_items.append({"type": "image_url", "image_url": {"url": first_frame_url}})
            content_items.append({"type": "text", "text": input_payload.get("prompt", prompt)})
            
            extra_payload = {
                "duration": input_payload.get("duration", max(4, min(15, duration))),
                "aspect_ratio": aspect_ratio,
                "resolution": settings.SEEDANCE_RESOLUTION,
                "generate_audio": False,
                "nsfw_checker": False,
            }
            if last_frame_url:
                extra_payload["last_frame_url"] = last_frame_url
            
            payload = {
                "model": model_str,
                "content": content_items,
                "extra": extra_payload,
            }
            create_url = f"{self.base_url}/contents/generations/tasks"
        else:
            # KIE AI / Kling：input 格式
            payload = {
                "model": model_str,
                "input": input_payload,
            }
            create_url = f"{self.base_url}/jobs/createTask"
            
        logger.info(f"[VideoGen] createTask 请求: url={create_url}, payload={payload}")
            
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                create_url,
                headers=self.headers,
                json=payload,
            )
            response.raise_for_status()
            result = response.json()
                
            logger.info(f"[VideoGen] createTask 响应: {result}")
                
            if is_seedance_2_direct:
                # 火山方舟直连响应：根级别 {"id": "cgt-xxx", ...}
                task_id = result.get("id")
                if not task_id:
                    raise RuntimeError(f"Seedance 直连响应缺少 id: {result}")
            else:
                # KIE AI 响应：{"code": 200, "data": {"taskId": "..."}}
                if result.get("code") != 200:
                    raise RuntimeError(f"创建任务失败: code={result.get('code')}, message={result.get('msg') or result.get('message')}, 完整响应={result}")
                data = result.get("data", {})
                task_id = data.get("taskId")
                if not task_id:
                    raise RuntimeError(f"KIE AI 响应缺少 taskId: {result}")
                
            logger.info(f"创建视频生成任务: {task_id}, 模型: {model_str}")
            return task_id
    
    async def check_status(self, task_id: str) -> dict:
        """
        查询任务状态。
        火山方舟 Seedance 直连: GET /contents/generations/tasks/{task_id}
        KIE AI 备选: GET /jobs/recordInfo?taskId={taskId}
        
        Returns:
            {"status": "PENDING|IN_PROGRESS|SUCCESS|FAILED", "video_url": str|None, 
             "last_frame_image_url": str|None, "error": str|None}
        """
        # 根据 base_url 判断是直连还是 KIE AI
        is_direct = "volces.com" in self.base_url
        
        async with httpx.AsyncClient(timeout=30) as client:
            if is_direct:
                # 火山方舟 Seedance 直连
                response = await client.get(
                    f"{self.base_url}/contents/generations/tasks/{task_id}",
                    headers=self.headers,
                )
            else:
                # KIE AI 备选
                response = await client.get(
                    f"{self.base_url}/jobs/recordInfo",
                    headers=self.headers,
                    params={"taskId": task_id},
                )
            response.raise_for_status()
            result = response.json()
            
            logger.info(f"[VideoGen] 状态查询响应: {result}")
            
            video_url = None
            last_frame_image_url = None
            error = None
            
            if is_direct:
                # 火山方舟直连：根级别响应 {"id": ..., "status": ..., "content": {"video_url": ...}}
                raw_state = result.get("status", "unknown")
                state_map = {
                    "pending": "PENDING",
                    "running": "IN_PROGRESS",
                    "processing": "IN_PROGRESS",
                    "succeeded": "SUCCESS",
                    "success": "SUCCESS",
                    "failed": "FAILED",
                    "fail": "FAILED",
                }
                status = state_map.get(raw_state, "UNKNOWN")
                
                if status == "SUCCESS":
                    content = result.get("content", {})
                    video_url = content.get("video_url")
                    last_frame_image_url = content.get("last_frame_image_url")
                elif status == "FAILED":
                    error_info = result.get("error", {})
                    if isinstance(error_info, dict):
                        error = error_info.get("message", "未知错误")
                    else:
                        error = str(error_info) or "未知错误"
            else:
                # KIE AI 备选：{"code": 200, "data": {...}}
                if result.get("code") != 200:
                    raise RuntimeError(f"查询状态失败: code={result.get('code')}, msg={result.get('msg') or result.get('message')}, 完整响应={result}")
                data = result.get("data", {})
                
                # KIE AI 状态映射: waiting/running/success/fail(ed)
                raw_state = data.get("state", "unknown")
                state_map = {
                    "waiting": "PENDING",
                    "running": "IN_PROGRESS",
                    "success": "SUCCESS",
                    "failed": "FAILED",
                    "fail": "FAILED",
                }
                status = state_map.get(raw_state, "UNKNOWN")
                
                if status == "SUCCESS":
                    result_json_str = data.get("resultJson", "")
                    if result_json_str:
                        try:
                            import json
                            result_data = json.loads(result_json_str)
                            result_urls = result_data.get("resultUrls", [])
                            if result_urls:
                                video_url = result_urls[0] if isinstance(result_urls, list) and result_urls else result_urls
                            else:
                                raw_url = (
                                    result_data.get("video_url") or 
                                    result_data.get("videoUrl") or
                                    result_data.get("url")
                                )
                                if isinstance(raw_url, list) and raw_url:
                                    video_url = raw_url[0]
                                else:
                                    video_url = raw_url
                        except Exception as e:
                            logger.warning(f"解析 resultJson 失败: {e}, 原始数据: {result_json_str}")
                elif status == "FAILED":
                    error = data.get("failMsg") or data.get("failCode") or "未知错误"
            
            return {
                "status": status,
                "video_url": video_url,
                "last_frame_image_url": last_frame_image_url,
                "error": error,
                "progress": result.get("progress") if is_direct else data.get("progress"),
                "raw_state": raw_state,
            }
    
    async def wait_for_completion(
        self, task_id: str, poll_interval: int = 10, max_wait: int = 600
    ) -> dict:
        """
        轮询等待任务完成，返回结果字典。
        使用指数退避：初始 poll_interval 秒，每次 ×1.5，最大 60 秒。
        
        Returns:
            {"video_url": str, "last_frame_image_url": str|None}
        """
        elapsed = 0
        current_interval = poll_interval
        unknown_count = 0  # UNKNOWN 状态计数器
        
        while elapsed < max_wait:
            await asyncio.sleep(current_interval)
            elapsed += current_interval
            
            try:
                result = await self.check_status(task_id)
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as net_err:
                # 瞬时网络抖动：不中断轮询，计为一次 UNKNOWN
                unknown_count += 1
                logger.warning(
                    f"任务 {task_id} check_status 网络异常（{type(net_err).__name__}），"
                    f"已累计 UNKNOWN {unknown_count} 次，已等待 {elapsed}s"
                )
                if unknown_count >= 5:
                    raise RuntimeError(
                        f"视频生成轮询连续网络异常超过阈值: {task_id}, 最后错误: {net_err!r}"
                    ) from net_err
                current_interval = min(int(current_interval * 1.5), 60)
                continue
            status = result["status"]
            
            logger.info(
                f"任务 {task_id} 状态: {status}, "
                f"进度: {result.get('progress', 'N/A')}, "
                f"已等待 {elapsed}s"
            )
            
            if status == "SUCCESS":
                if not result["video_url"]:
                    raise RuntimeError(f"任务完成但未返回视频 URL: {task_id}")
                return {
                    "video_url": result["video_url"],
                    "last_frame_image_url": result.get("last_frame_image_url"),
                }
            elif status == "FAILED":
                raise RuntimeError(f"视频生成失败: {result.get('error', '未知错误')}")
            elif status == "UNKNOWN":
                # 安全保护：UNKNOWN 状态连续出现多次则视为失败
                unknown_count += 1
                if unknown_count >= 3:
                    logger.warning(f"任务 {task_id} 连续 {unknown_count} 次返回 UNKNOWN 状态，视为失败")
                    raise RuntimeError(f"视频生成失败: 未知状态 (raw_state: {result.get('raw_state')})")
            else:
                # 重置 UNKNOWN 计数器
                unknown_count = 0
            
            # 指数退避
            current_interval = min(int(current_interval * 1.5), 60)
        
        raise TimeoutError(f"视频生成超时: {task_id}, 已等待 {elapsed}s")
    
    async def extract_last_frame(self, video_url: str) -> str:
        """
        从视频中提取最后一帧，保存为 PNG。
        
        Returns:
            本地文件路径
        """
        # 下载视频到临时文件
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            response = await client.get(video_url)
            response.raise_for_status()
            
            with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
                tmp.write(response.content)
                tmp_path = tmp.name
        
        try:
            # 使用 opencv 提取最后一帧
            cap = cv2.VideoCapture(tmp_path)
            if not cap.isOpened():
                raise RuntimeError(f"无法打开视频: {video_url}")
            
            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            if total_frames <= 0:
                raise RuntimeError(f"视频帧数为 0: {video_url}")
            
            # 定位到最后一帧
            cap.set(cv2.CAP_PROP_POS_FRAMES, total_frames - 1)
            ret, frame = cap.read()
            cap.release()
            
            if not ret or frame is None:
                raise RuntimeError(f"无法读取最后一帧: {video_url}")
            
            # 保存为 PNG
            frame_filename = f"{uuid.uuid4().hex}.png"
            frame_path = str(self.frames_dir / frame_filename)
            cv2.imwrite(frame_path, frame)
            
            logger.info(f"提取最后一帧: {frame_path} (共 {total_frames} 帧)")
            return frame_path
            
        finally:
            # 清理临时视频文件
            os.unlink(tmp_path)
    
    async def get_frame_url(self, frame_path: str, service_base_url: str = "http://localhost:8000") -> str:
        """
        将本地帧图片路径转为可访问的 URL。
        通过 FastAPI 的 StaticFiles 中间件提供。
        
        Args:
            frame_path: 本地文件路径
            service_base_url: 服务基础 URL
        
        Returns:
            可访问的完整 URL
        """
        filename = os.path.basename(frame_path)
        return f"{service_base_url}/static/frames/{filename}"
    
    async def upload_frame(self, frame_path: str) -> str:
        """
        将本地帧图片上传到可访问的 URL
        
        暂时实现为本地文件服务：
        - 将文件移动到静态文件目录
        - 返回可通过 FastAPI StaticFiles 访问的 URL
        
        Args:
            frame_path: 本地帧图片路径
        
        Returns:
            可访问的 URL
        
        Raises:
            RuntimeError: 上传失败
        """
        # 生成唯一文件名
        filename = f"{uuid.uuid4().hex}.png"
        dest_path = self.frames_dir / filename
        
        try:
            # 移动文件到静态目录
            shutil.move(frame_path, dest_path)
            
            # 构建 URL
            base_url = f"http://{settings.SERVICE_HOST}:{settings.SERVICE_PORT}"
            url = f"{base_url}/static/frames/{filename}"
            
            logger.info(f"帧图片已上传，URL: {url}")
            return url
        
        except Exception as e:
            logger.error(f"上传帧图片失败: {e}")
            raise RuntimeError(f"上传帧图片失败: {e}")
    
    async def download_video(self, video_url: str, save_path: str) -> str:
        """
        下载视频到本地
        
        Args:
            video_url: 视频 URL
            save_path: 保存路径
        
        Returns:
            本地文件路径
        """
        logger.info(f"下载视频: {video_url} -> {save_path}")
        
        # 确保目录存在
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            response = await client.get(video_url)
            response.raise_for_status()
            
            with open(save_path, "wb") as f:
                f.write(response.content)
        
        logger.info(f"视频下载完成: {save_path}")
        return save_path


def create_video_gen_service() -> VideoGenService:
    """从配置创建视频生成服务实例（默认 Seedance 直连，光配置则回退 KIE AI）"""
    from config import settings
    if settings.SEEDANCE_API_KEY:
        return VideoGenService(
            api_key=settings.SEEDANCE_API_KEY,
            base_url=settings.SEEDANCE_BASE_URL,
        )
    # 回退到 KIE AI
    logger.warning("SEEDANCE_API_KEY 未配置，回退使用 KIE AI")
    return VideoGenService(
        api_key=settings.KIE_API_KEY,
        base_url=settings.KIE_BASE_URL,
    )
