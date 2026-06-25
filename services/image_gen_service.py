"""
图片生成服务封装
通过 KIE AI GPT Image 2 API 实现文生图 / 图生图
"""

import httpx
import asyncio
import json
import logging
from typing import Optional, List

from config import settings

logger = logging.getLogger(__name__)


class ImageGenService:
    """KIE AI GPT Image 2 图片生成服务"""

    BASE_URL = "https://api.kie.ai"
    MODEL_TEXT_TO_IMAGE = "gpt-image-2-text-to-image"
    MODEL_IMAGE_TO_IMAGE = "gpt-image-2-image-to-image"

    # 终态状态集合
    _TERMINAL_STATES = {"success", "fail"}
    # 进行中状态集合
    _PENDING_STATES = {"waiting", "queuing", "generating"}

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or settings.KIE_API_KEY
        if not self.api_key:
            logger.warning("[ImageGen] KIE_API_KEY 未配置，图片生成服务将不可用")
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # 创建任务
    # ------------------------------------------------------------------
    async def generate_image(
        self,
        prompt: str,
        input_urls: Optional[List[str]] = None,
        model: Optional[str] = None,
        max_retries: int = 3,
    ) -> str:
        """创建图片生成任务，返回 task_id。

        - input_urls 为空/None → 使用 gpt-image-2-text-to-image（文生图）
        - input_urls 非空 → 使用 gpt-image-2-image-to-image（图生图）

        Args:
            prompt: 生成提示词
            input_urls: 参考图片 URL 列表（图生图时必填）
            model: 可选覆盖模型名
            max_retries: 网络请求最大重试次数

        Returns:
            task_id: 异步任务 ID
        """
        # 自动选择模型
        if model is None:
            model = (
                self.MODEL_IMAGE_TO_IMAGE
                if input_urls
                else self.MODEL_TEXT_TO_IMAGE
            )

        input_payload: dict = {
            "prompt": prompt,
            "nsfw_checker": False,
        }
        if input_urls:
            input_payload["input_urls"] = input_urls

        payload = {
            "model": model,
            "input": input_payload,
        }

        create_url = f"{self.BASE_URL}/api/v1/jobs/createTask"
        logger.info(f"[ImageGen] createTask 请求: url={create_url}, model={model}, prompt={prompt[:80]}...")

        last_error: Optional[Exception] = None
        for attempt in range(1, max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    response = await client.post(
                        create_url,
                        headers=self.headers,
                        json=payload,
                    )
                    response.raise_for_status()
                    result = response.json()

                logger.info(f"[ImageGen] createTask 响应: {result}")

                if result.get("code") != 200:
                    raise RuntimeError(
                        f"创建图片任务失败: code={result.get('code')}, "
                        f"message={result.get('msg') or result.get('message')}, "
                        f"完整响应={result}"
                    )

                task_id = result.get("data", {}).get("taskId")
                if not task_id:
                    raise RuntimeError(f"KIE AI 响应缺少 taskId: {result}")

                logger.info(f"[ImageGen] 创建图片生成任务成功: task_id={task_id}, model={model}")
                return task_id

            except httpx.HTTPStatusError as e:
                last_error = e
                logger.warning(
                    f"[ImageGen] createTask HTTP 错误 (attempt {attempt}/{max_retries}): "
                    f"status={e.response.status_code}, body={e.response.text}"
                )
                if attempt < max_retries:
                    await asyncio.sleep(2 ** attempt)
            except httpx.RequestError as e:
                last_error = e
                logger.warning(
                    f"[ImageGen] createTask 网络错误 (attempt {attempt}/{max_retries}): {e}"
                )
                if attempt < max_retries:
                    await asyncio.sleep(2 ** attempt)

        raise RuntimeError(f"创建图片任务失败，已重试 {max_retries} 次: {last_error}")

    # ------------------------------------------------------------------
    # 查询状态
    # ------------------------------------------------------------------
    async def check_status(self, task_id: str) -> dict:
        """查询任务状态。

        Returns:
            dict 包含:
            - status: str (waiting/queuing/generating/success/fail)
            - result_urls: list[str] | None（成功时）
            - error: str | None（失败时）
        """
        query_url = f"{self.BASE_URL}/api/v1/jobs/recordInfo"

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                query_url,
                headers=self.headers,
                params={"taskId": task_id},
            )
            response.raise_for_status()
            result = response.json()

        logger.info(f"[ImageGen] 状态查询响应: task_id={task_id}, result={result}")

        if result.get("code") != 200:
            raise RuntimeError(
                f"查询图片任务状态失败: code={result.get('code')}, "
                f"msg={result.get('msg') or result.get('message')}, "
                f"完整响应={result}"
            )

        data = result.get("data", {})
        raw_status = data.get("state", "unknown")
        result_urls: Optional[List[str]] = None
        error: Optional[str] = None

        if raw_status == "success":
            result_json_str = data.get("resultJson", "")
            if result_json_str:
                try:
                    result_data = json.loads(result_json_str)
                    urls = result_data.get("resultUrls", [])
                    if isinstance(urls, list) and urls:
                        result_urls = urls
                    else:
                        logger.warning(
                            f"[ImageGen] resultJson 中缺少 resultUrls: {result_data}"
                        )
                except (json.JSONDecodeError, TypeError) as e:
                    logger.warning(
                        f"[ImageGen] 解析 resultJson 失败: {e}, 原始数据: {result_json_str}"
                    )
        elif raw_status == "fail":
            error = data.get("failMsg") or data.get("failCode") or "未知错误"

        return {
            "status": raw_status,
            "result_urls": result_urls,
            "error": error,
        }

    # ------------------------------------------------------------------
    # 轮询等待完成
    # ------------------------------------------------------------------
    async def wait_for_completion(
        self,
        task_id: str,
        poll_interval: float = 5.0,
        timeout: float = 300.0,
    ) -> List[str]:
        """轮询等待任务完成，返回结果图片 URL 列表。

        使用指数退避：初始 poll_interval 秒，每次 ×1.5，最大 30 秒。

        Args:
            task_id: 任务 ID
            poll_interval: 初始轮询间隔（秒）
            timeout: 最大等待时间（秒）

        Returns:
            结果图片 URL 列表

        Raises:
            TimeoutError: 超时
            RuntimeError: 任务失败
        """
        elapsed = 0.0
        current_interval = poll_interval

        while elapsed < timeout:
            await asyncio.sleep(current_interval)
            elapsed += current_interval

            try:
                status_result = await self.check_status(task_id)
            except Exception as e:
                logger.warning(
                    f"[ImageGen] 轮询状态查询异常 (task_id={task_id}): {e}, 继续重试..."
                )
                current_interval = min(current_interval * 1.5, 30.0)
                continue

            status = status_result["status"]
            logger.info(
                f"[ImageGen] 任务 {task_id} 状态: {status}, 已等待 {elapsed:.1f}s"
            )

            if status == "success":
                urls = status_result.get("result_urls")
                if not urls:
                    raise RuntimeError(
                        f"图片生成任务完成但未返回结果 URL: task_id={task_id}"
                    )
                logger.info(
                    f"[ImageGen] 任务 {task_id} 完成，结果: {urls}"
                )
                return urls

            if status == "fail":
                raise RuntimeError(
                    f"图片生成失败: task_id={task_id}, "
                    f"error={status_result.get('error', '未知错误')}"
                )

            # 指数退避
            current_interval = min(current_interval * 1.5, 30.0)

        raise TimeoutError(
            f"图片生成超时: task_id={task_id}, 已等待 {elapsed:.1f}s (timeout={timeout}s)"
        )

    # ------------------------------------------------------------------
    # 便捷方法：提交并等待
    # ------------------------------------------------------------------
    async def generate_and_wait(
        self,
        prompt: str,
        input_urls: Optional[List[str]] = None,
        model: Optional[str] = None,
        poll_interval: float = 5.0,
        timeout: float = 300.0,
    ) -> List[str]:
        """提交图片生成任务并等待完成，返回结果图片 URL 列表。

        Args:
            prompt: 生成提示词
            input_urls: 参考图片 URL 列表（图生图时必填）
            model: 可选覆盖模型名
            poll_interval: 初始轮询间隔（秒）
            timeout: 最大等待时间（秒）

        Returns:
            结果图片 URL 列表
        """
        task_id = await self.generate_image(
            prompt=prompt,
            input_urls=input_urls,
            model=model,
        )
        return await self.wait_for_completion(
            task_id=task_id,
            poll_interval=poll_interval,
            timeout=timeout,
        )


def create_image_gen_service() -> ImageGenService:
    """从配置创建图片生成服务实例"""
    return ImageGenService(api_key=settings.KIE_API_KEY)
