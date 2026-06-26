"""
Airtable 服务封装
用于项目、素材、分镜、审核记录等数据的读写
使用 pyairtable 库实现
"""

import asyncio
import logging
import os
from typing import Any, Optional

from requests.exceptions import HTTPError

try:
    from pyairtable import Api
except ImportError:  # pragma: no cover - exercised only without optional extra
    Api = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# 清除代理环境变量，避免 SOCKS 代理导致连接问题
for key in ['HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy']:
    if key in os.environ:
        del os.environ[key]


class AirtableService:
    """Airtable 数据服务层"""

    # 表名常量
    PROJECTS_TABLE = "Projects"
    ASSETS_TABLE = "Assets"
    SHOTS_TABLE = "Shots"
    REVIEWS_TABLE = "Reviews"

    # Shots 表中 OST 本地化相关字段（用户需在 Airtable 手动建字段）
    SHOT_OST_ORIGINAL_FIELD = "OST原文"       # Long text
    SHOT_OST_LOCALIZED_FIELD = "OST本地化"     # Long text
    SHOT_OST_CATEGORY_FIELD = "OST分类"         # Single select: generic_hook/product_specific/promo/emotional/brand_badge

    # Shots 表中复刻剪辑 Agent 相关字段（用户需在 Airtable 手动建字段，缺失时竞态跳过）
    SHOT_EDIT_PLAN_FIELD = "剪辑指令"             # Long text（JSON）
    SHOT_SRC_DURATION_FIELD = "原镜头时长"         # Number
    SHOT_TARGET_DURATION_FIELD = "目标时长"       # Number
    SHOT_EDIT_REVIEW_STATUS_FIELD = "剪辑审核状态"  # Single Select: 待审核/已通过/已驳回

    # 统一模型审查层字段（用户需在 Airtable 手动建字段，缺失时降级为警告，不阻塞）
    # Projects 表：
    PROJECT_AUDIT_VIDEO_STATUS_FIELD = "视频分析审查状态"   # Single Select: 待审核/已通过/已驳回
    PROJECT_AUDIT_VIDEO_COMMENT_FIELD = "视频分析审查意见"  # Long text
    PROJECT_AUDIT_PRODUCT_STATUS_FIELD = "商品分析审查状态"  # Single Select
    PROJECT_AUDIT_PRODUCT_COMMENT_FIELD = "商品分析审查意见" # Long text
    # Shots 表：
    SHOT_KEYFRAME_AUDIT_STATUS_FIELD = "关键帧审查状态"        # Single Select
    SHOT_KEYFRAME_AUDIT_COMMENT_FIELD = "关键帧审查意见"       # Long text
    SHOT_KEYFRAME_AUDIT_ATTEMPT_FIELD = "关键帧审查尝试次数"   # Number（级联重试使用，缺失则跳过）

    def __new__(cls, *args: Any, **kwargs: Any) -> Any:
        if cls is AirtableService:
            from config import settings

            if settings.DATA_BACKEND == "postgres":
                from services.database_state_service import DatabaseStateService

                return DatabaseStateService()
        return super().__new__(cls)

    def __init__(self, api_key: str, base_id: str) -> None:
        """
        初始化 Airtable 服务

        Args:
            api_key: Airtable API 密钥
            base_id: Airtable Base ID
        """
        if Api is None:
            raise RuntimeError(
                "DATA_BACKEND=airtable requires optional dependency pyairtable. "
                "Install it with: pip install -r requirements-airtable.txt"
            )
        self.api_key = api_key
        self.base_id = base_id
        self.api = Api(api_key)
        self._projects = self.api.table(base_id, self.PROJECTS_TABLE)
        self._assets = self.api.table(base_id, self.ASSETS_TABLE)
        self._shots = self.api.table(base_id, self.SHOTS_TABLE)
        self._reviews = self.api.table(base_id, self.REVIEWS_TABLE)

    # ==========================================================================
    # 项目 (Projects) 操作
    # ==========================================================================

    async def create_project(
        self,
        name: str,
        video_url: str = "",
        product_image_url: str = "",
        mode: str = "full",
    ) -> dict[str, Any]:
        """
        创建新项目记录

        Args:
            name: 项目名称
            video_url: 原视频 URL
            product_image_url: 产品图链接
            mode: 模式 (simple/full)

        Returns:
            创建的项目记录
        """
        fields = {
            "项目名称": name,
            "状态": "素材准备中",
            "产品图链接": product_image_url,
            "模式": mode,
        }
        # 原视频作为附件
        if video_url:
            fields["原视频"] = [{"url": video_url}]

        try:
            record = await asyncio.to_thread(self._projects.create, fields)
            logger.info(f"创建项目: {record['id']} - {name}")
            return record
        except (HTTPError, Exception) as e:
            logger.error(f"创建项目失败: {e}")
            raise

    async def update_project_status(self, project_id: str, status: str) -> dict[str, Any]:
        """
        更新项目状态

        Args:
            project_id: 项目 ID
            status: 新状态

        Returns:
            更新后的项目记录
        """
        try:
            record = await asyncio.to_thread(
                self._projects.update, project_id, {"状态": status}
            )
            logger.info(f"更新项目状态: {project_id} → {status}")
            return record
        except (HTTPError, Exception) as e:
            logger.error(f"更新项目状态失败: {e}")
            raise

    async def get_project(self, project_id: str) -> Optional[dict[str, Any]]:
        """
        获取项目详情

        Args:
            project_id: 项目 ID

        Returns:
            项目记录，不存在则返回 None
        """
        try:
            return await asyncio.to_thread(self._projects.get, project_id)
        except HTTPError as e:
            if e.status_code == 404:
                return None
            logger.error(f"获取项目失败: {e}")
            raise
        except (HTTPError, Exception) as e:
            logger.error(f"获取项目失败: {e}")
            raise

    async def update_project(
        self, project_id: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        """
        更新项目信息

        Args:
            project_id: 项目 ID
            data: 更新的数据

        Returns:
            更新后的项目记录
        """
        try:
            record = await asyncio.to_thread(self._projects.update, project_id, data)
            logger.info(f"更新项目: {project_id}")
            return record
        except (HTTPError, Exception) as e:
            logger.error(f"更新项目失败: {e}")
            raise

    async def list_projects(
        self, status: Optional[str] = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """
        获取项目列表

        Args:
            status: 按状态筛选
            limit: 返回数量限制

        Returns:
            项目记录列表
        """
        try:
            formula = None
            if status:
                formula = f"{{状态}} = '{status}'"
            records = await asyncio.to_thread(
                self._projects.all, formula=formula, max_records=limit
            )
            return records
        except (HTTPError, Exception) as e:
            logger.error(f"获取项目列表失败: {e}")
            raise

    # ==========================================================================
    # Product Brief Agent 相关字段操作
    # ==========================================================================

    # 用于保存 Agent Brief 草稿与待用户确认问题的字段名
    # （若 Airtable 实际未建该字段，写入会失败但不影响工作流；读取时也宽容）
    BRIEF_DRAFT_FIELD = "Brief草稿"
    BRIEF_CLARIFICATION_FIELD = "待确认问题"
    BRIEF_CONFIDENCE_FIELD = "Brief置信度"
    BRIEF_USER_ANSWERS_FIELD = "用户答复"

    async def save_product_brief_draft(
        self, project_id: str, brief: dict[str, Any]
    ) -> bool:
        """将 Brief 草稿写入项目记录（字段不存在时降级）

        Args:
            project_id: 项目 ID
            brief: ProductBrief.model_dump() 结果

        Returns:
            True 写入成功；False 失败（不阻塞主流程）
        """
        import json as _json
        try:
            clarifications = brief.get("clarification_items") or []
            data: dict[str, Any] = {
                # 阈值说明：Airtable Long Text 实际容量远大于 90KB，
                # 对齐 _truncate_content 默认阈值上调到 200000 字符，
                # 避免复杂产品 Brief 草稿被过度截断。
                self.BRIEF_DRAFT_FIELD: _json.dumps(brief, ensure_ascii=False)[:200000],
                self.BRIEF_CLARIFICATION_FIELD: _json.dumps(clarifications, ensure_ascii=False)[:200000],
                self.BRIEF_CONFIDENCE_FIELD: float(brief.get("confidence_score", 0.0)),
            }
            await asyncio.to_thread(self._projects.update, project_id, data)
            logger.info(f"[{project_id}] product brief draft saved (fields={list(data.keys())})")
            return True
        except HTTPError as e:
            # 字段不存在等情况：仅记录警告不报错
            logger.warning(f"[{project_id}] save_product_brief_draft failed (non-blocking): {e}")
            return False
        except Exception as e:
            logger.warning(f"[{project_id}] save_product_brief_draft unexpected error: {e}")
            return False

    async def get_product_brief_state(
        self, project_id: str
    ) -> dict[str, Any]:
        """读取项目中的 Brief 草稿与用户答复

        Returns:
            {"draft": dict|None, "user_answers": dict|None, "clarifications": list}
            字段不存在或解析失败时返回空值。
        """
        import json as _json
        result: dict[str, Any] = {"draft": None, "user_answers": None, "clarifications": []}
        try:
            record = await asyncio.to_thread(self._projects.get, project_id)
            fields = (record or {}).get("fields", {})
            draft_raw = fields.get(self.BRIEF_DRAFT_FIELD)
            if draft_raw:
                try:
                    result["draft"] = _json.loads(draft_raw)
                except Exception:
                    logger.warning(f"[{project_id}] brief draft field is not valid JSON")
            clarif_raw = fields.get(self.BRIEF_CLARIFICATION_FIELD)
            if clarif_raw:
                try:
                    result["clarifications"] = _json.loads(clarif_raw)
                except Exception:
                    pass
            answers_raw = fields.get(self.BRIEF_USER_ANSWERS_FIELD)
            if answers_raw:
                try:
                    result["user_answers"] = _json.loads(answers_raw)
                except Exception:
                    # 允许用户填纯文本，按 key=value 解析降级
                    parsed: dict[str, str] = {}
                    for line in str(answers_raw).splitlines():
                        if "=" in line:
                            k, _, v = line.partition("=")
                            parsed[k.strip()] = v.strip()
                    result["user_answers"] = parsed or None
        except HTTPError as e:
            logger.warning(f"[{project_id}] get_product_brief_state failed: {e}")
        except Exception as e:
            logger.warning(f"[{project_id}] get_product_brief_state unexpected error: {e}")
        return result

    async def save_product_brief_finalized(
        self, project_id: str, brief: dict[str, Any]
    ) -> bool:
        """保存最终态 Brief（覆盖草稿字段，清空 clarification）"""
        import json as _json
        try:
            data = {
                # 同 save_product_brief_draft，上调阈值到 200000 字符。
                self.BRIEF_DRAFT_FIELD: _json.dumps(brief, ensure_ascii=False)[:200000],
                self.BRIEF_CLARIFICATION_FIELD: "",
                self.BRIEF_CONFIDENCE_FIELD: float(brief.get("confidence_score", 0.0)),
            }
            await asyncio.to_thread(self._projects.update, project_id, data)
            logger.info(f"[{project_id}] product brief finalized and saved")
            return True
        except Exception as e:
            logger.warning(f"[{project_id}] save_product_brief_finalized failed: {e}")
            return False

    # ==========================================================================
    # 素材 (Assets) 操作
    # ==========================================================================

    async def create_asset(
        self,
        project_id: str,
        asset_type: str,
        content: str = "",
        attachment_url: str = "",
    ) -> dict[str, Any]:
        """
        创建素材记录

        Args:
            project_id: 项目 ID
            asset_type: 素材类型 (三视图/商品属性/原视频脚本)
            content: 内容
            attachment_url: 附件 URL

        Returns:
            创建的素材记录
        """
        fields = {
            "项目": [project_id],
            "素材类型": asset_type,
            "内容": content,
        }
        if attachment_url:
            fields["附件"] = [{"url": attachment_url}]

        try:
            record = await asyncio.to_thread(self._assets.create, fields)
            logger.info(f"创建素材: {record['id']} - 类型: {asset_type}")
            return record
        except (HTTPError, Exception) as e:
            logger.error(f"创建素材失败: {e}")
            raise

    async def create_asset_from_dict(self, data: dict[str, Any]) -> dict[str, Any]:
        """
        从字典创建素材记录

        Args:
            data: 包含 project_id, asset_type, content, attachment_url 的字典

        Returns:
            创建的素材记录
        """
        return await self.create_asset(
            project_id=data.get("project_id", ""),
            asset_type=data.get("asset_type", ""),
            content=data.get("content", ""),
            attachment_url=data.get("attachment_url", ""),
        )

    async def update_asset_content(
        self,
        project_id: str,
        asset_type: str,
        content: str,
    ) -> Optional[dict[str, Any]]:
        """
        更新指定项目+素材类型的素材内容字段

        找到第一条匹配的记录并更新其 "内容" 字段。
        """
        try:
            records = await asyncio.to_thread(self._assets.all)
            target = None
            for r in records:
                fields = r.get("fields", {})
                if (
                    project_id in fields.get("项目", [])
                    and fields.get("素材类型", "") == asset_type
                ):
                    target = r
                    break
            if not target:
                logger.warning(f"update_asset_content: 未找到 {project_id}/{asset_type}，fallback to create")
                return await self.create_asset(
                    project_id=project_id,
                    asset_type=asset_type,
                    content=content,
                )
            record = await asyncio.to_thread(
                self._assets.update, target["id"], {"内容": content}
            )
            logger.info(f"更新素材内容: {target['id']} - 类型: {asset_type}")
            return record
        except (HTTPError, Exception) as e:
            logger.error(f"更新素材内容失败: {e}")
            raise

    async def get_project_assets(self, project_id: str) -> list[dict[str, Any]]:
        """
        获取项目的所有素材

        Args:
            project_id: 项目 ID

        Returns:
            素材记录列表
        """
        try:
            # Link 字段在 formula 中返回显示值而非 record ID，改用客户端过滤
            records = await asyncio.to_thread(self._assets.all)
            # 在 API 返回的 JSON 中，Link 字段 "项目" 是 record ID 数组
            filtered = [
                r for r in records
                if project_id in r.get("fields", {}).get("项目", [])
            ]
            logger.info(f"get_project_assets: found {len(filtered)}/{len(records)} assets for {project_id}")
            return filtered
        except (HTTPError, Exception) as e:
            logger.error(f"获取素材失败: {e}")
            raise

    async def get_assets(self, project_id: str) -> list[dict[str, Any]]:
        """
        获取项目的所有素材（别名方法）

        Args:
            project_id: 项目 ID

        Returns:
            素材记录列表
        """
        return await self.get_project_assets(project_id)

    # ==========================================================================
    # 分镜 (Shots) 操作
    # ==========================================================================

    async def create_shot(
        self,
        project_id: str,
        shot_number: int,
        original_description: str = "",
        new_description: str = "",
        generation_prompt: str = "",
    ) -> dict[str, Any]:
        """
        创建分镜记录

        Args:
            project_id: 项目 ID
            shot_number: 镜头序号
            original_description: 原镜头描述
            new_description: 新镜头描述
            generation_prompt: 生成提示词

        Returns:
            创建的分镜记录
        """
        fields = {
            "项目": [project_id],
            "镜头序号": shot_number,
            "原镜头描述": original_description,
            "新镜头描述": new_description,
            "生成提示词": generation_prompt,
            "提示词审核状态": "待审核",
            "生成状态": "待生成",
        }

        try:
            record = await asyncio.to_thread(self._shots.create, fields)
            logger.info(f"创建分镜: 镜头{shot_number}")
            return record
        except (HTTPError, Exception) as e:
            logger.error(f"创建分镜失败: {e}")
            raise

    async def create_shot_from_dict(self, data: dict[str, Any]) -> dict[str, Any]:
        """
        从字典创建分镜记录

        Args:
            data: 分镜数据字典

        Returns:
            创建的分镜记录
        """
        return await self.create_shot(
            project_id=data.get("project_id", ""),
            shot_number=data.get("sequence_number", data.get("shot_number", 0)),
            original_description=data.get("original_shot_description", ""),
            new_description=data.get("new_shot_description", ""),
            generation_prompt=data.get("generation_prompt", ""),
        )

    async def batch_create_shots(
        self, shots: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """
        批量创建分镜记录

        Args:
            shots: 分镜数据列表

        Returns:
            创建的分镜记录列表
        """
        results = []
        for shot_data in shots:
            try:
                record = await self.create_shot_from_dict(shot_data)
                results.append(record)
            except (HTTPError, Exception) as e:
                logger.error(f"批量创建分镜失败: {e}")
                raise
        return results

    async def get_project_shots(self, project_id: str) -> list[dict[str, Any]]:
        """
        获取项目的所有分镜，按镜头序号排序

        Args:
            project_id: 项目 ID

        Returns:
            分镜记录列表
        """
        try:
            records = await asyncio.to_thread(self._shots.all)
            filtered = [
                r for r in records
                if project_id in r.get("fields", {}).get("项目", [])
            ]
            # 客户端按镜头序号排序
            filtered.sort(key=lambda r: r.get("fields", {}).get("镜头序号", 0))
            logger.info(f"get_project_shots: found {len(filtered)}/{len(records)} shots for {project_id}")
            return filtered
        except (HTTPError, Exception) as e:
            logger.error(f"获取分镜失败: {e}")
            raise

    async def get_shots(self, project_id: str) -> list[dict[str, Any]]:
        """
        获取项目的所有分镜（别名方法）

        Args:
            project_id: 项目 ID

        Returns:
            分镜记录列表
        """
        return await self.get_project_shots(project_id)

    async def update_shot(
        self, shot_id: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        """
        更新分镜信息

        Args:
            shot_id: 分镜 ID
            data: 更新的数据

        Returns:
            更新后的分镜记录
        """
        try:
            record = await asyncio.to_thread(self._shots.update, shot_id, data)
            logger.info(f"更新分镜: {shot_id}")
            return record
        except (HTTPError, Exception) as e:
            logger.error(f"更新分镜失败: {e}")
            raise

    async def save_shot_ost_localization(
        self,
        shot_id: str,
        original: str,
        localized: str,
        category: Optional[str] = None,
    ) -> bool:
        """回写 OST 本地化结果到 Shots 表。

        字段不存在时只警告不抛错，防止阻塞主流程。

        Args:
            shot_id: Shots 表 record_id
            original: OST 原文
            localized: Gemini 生成的本地化文案（delete 时传空字符串）
            category: OST 分类标签

        Returns:
            True=回写成功，False=字段不存在或写入失败
        """
        fields: dict[str, Any] = {
            self.SHOT_OST_ORIGINAL_FIELD: original or "",
            self.SHOT_OST_LOCALIZED_FIELD: localized or "",
        }
        if category:
            fields[self.SHOT_OST_CATEGORY_FIELD] = category
        try:
            await asyncio.to_thread(self._shots.update, shot_id, fields)
            return True
        except HTTPError as e:
            msg = str(e)
            # 字段不存在 / Single Select 选项未预置 → 警告不报错
            if "UNKNOWN_FIELD_NAME" in msg or "INVALID_MULTIPLE_CHOICE_OPTIONS" in msg:
                logger.warning(
                    f"save_shot_ost_localization: Airtable Shots 表缺少 OST 相关字段或选项，跳过回写 ({shot_id}): {msg}"
                )
                return False
            logger.error(f"save_shot_ost_localization 写入失败 ({shot_id}): {msg}")
            return False
        except Exception as e:
            logger.error(f"save_shot_ost_localization 异常 ({shot_id}): {e}")
            return False

    async def update_shot_prompt(self, shot_id: str, prompt: str) -> dict[str, Any]:
        """
        更新分镜的生成提示词

        Args:
            shot_id: 分镜 ID
            prompt: 生成提示词

        Returns:
            更新后的分镜记录
        """
        try:
            record = await asyncio.to_thread(
                self._shots.update,
                shot_id,
                {"生成提示词": prompt, "提示词审核状态": "待审核"},
            )
            logger.info(f"更新分镜提示词: {shot_id}")
            return record
        except (HTTPError, Exception) as e:
            logger.error(f"更新分镜提示词失败: {e}")
            raise

    async def update_shot_prompt_status(
        self, shot_id: str, status: str, review_comment: str = ""
    ) -> dict[str, Any]:
        """
        更新提示词审核状态

        Args:
            shot_id: 分镜 ID
            status: 审核状态
            review_comment: 审核意见

        Returns:
            更新后的分镜记录
        """
        fields = {"提示词审核状态": status}
        if review_comment:
            fields["提示词审核意见"] = review_comment

        try:
            record = await asyncio.to_thread(self._shots.update, shot_id, fields)
            logger.info(f"更新提示词审核状态: {shot_id} → {status}")
            return record
        except (HTTPError, Exception) as e:
            logger.error(f"更新提示词审核状态失败: {e}")
            raise

    async def update_shot_keyframe(
        self, shot_id: str, keyframe_image_url: str
    ) -> dict[str, Any]:
        """
        更新分镜的关键帧图片 URL

        Args:
            shot_id: 分镜 ID
            keyframe_image_url: 关键帧图片 URL

        Returns:
            更新后的分镜记录
        """
        try:
            record = await asyncio.to_thread(
                self._shots.update,
                shot_id,
                {"关键帧图片": keyframe_image_url},
            )
            logger.info(f"更新分镜关键帧: {shot_id}")
            return record
        except (HTTPError, Exception) as e:
            logger.error(f"更新分镜关键帧失败: {e}")
            raise

    async def update_shot_video(self, shot_id: str, video_url: str) -> dict[str, Any]:
        """
        更新分镜的生成视频

        Args:
            shot_id: 分镜 ID
            video_url: 生成视频 URL

        Returns:
            更新后的分镜记录
        """
        try:
            record = await asyncio.to_thread(
                self._shots.update,
                shot_id,
                {"生成视频": video_url, "视频审核状态": "待审核"},
            )
            logger.info(f"更新分镜视频: {shot_id}")
            return record
        except (HTTPError, Exception) as e:
            logger.error(f"更新分镜视频失败: {e}")
            raise

    async def update_shot_edit_plan(
        self,
        shot_id: str,
        edit_plan: dict,
        source_duration: Optional[float] = None,
        target_duration: Optional[float] = None,
        edit_review_status: Optional[str] = None,
    ) -> bool:
        """写入复刻剪辑 Agent 产出的 edit_plan 到 Shots 表。

        字段缺失时（用户未在 Airtable 手动建字段）降级为警告，不阻塞流水线。
        这里采用“逐个字段重试”策略：若某个字段不存在造成整个请求失败，则逐一重试剩余字段。
        """
        import json as _json

        candidate_fields: dict[str, Any] = {}
        try:
            candidate_fields[self.SHOT_EDIT_PLAN_FIELD] = _json.dumps(
                edit_plan, ensure_ascii=False
            )
        except Exception:
            candidate_fields[self.SHOT_EDIT_PLAN_FIELD] = str(edit_plan)
        if source_duration is not None:
            candidate_fields[self.SHOT_SRC_DURATION_FIELD] = float(source_duration)
        if target_duration is not None:
            candidate_fields[self.SHOT_TARGET_DURATION_FIELD] = float(target_duration)
        if edit_review_status:
            candidate_fields[self.SHOT_EDIT_REVIEW_STATUS_FIELD] = edit_review_status

        async def _try(fields: dict[str, Any]) -> bool:
            try:
                await asyncio.to_thread(self._shots.update, shot_id, fields)
                return True
            except HTTPError as e:
                msg = str(e)
                if "UNKNOWN_FIELD_NAME" in msg:
                    return False
                logger.error(f"update_shot_edit_plan 写入失败 ({shot_id}): {msg}")
                return False
            except Exception as e:
                logger.error(f"update_shot_edit_plan 异常 ({shot_id}): {e}")
                return False

        if await _try(candidate_fields):
            return True

        # 降级：逐字段尝试。缺失的字段静默跳过。
        any_success = False
        for k, v in candidate_fields.items():
            if await _try({k: v}):
                any_success = True
            else:
                logger.warning(
                    f"update_shot_edit_plan: Airtable Shots 表缺失字段 '{k}'，跳过写入 ({shot_id})"
                )
        return any_success

    async def save_stage1_audit_result(
        self,
        project_id: str,
        scope: str,
        status: str,
        review_comment: str = "",
    ) -> bool:
        """写回 Stage 1 审查结果到 Projects 表。

        Args:
            project_id: 项目 ID
            scope: "video" （审查点 1.1）或 "product" （审查点 1.2）
            status: "待审核" / "已通过" / "已驳回"
            review_comment: 审查意见

        字段缺失时（用户未在 Airtable 手动建字段）降级为警告，不阻塞。
        采用与 update_shot_edit_plan 一致的 “整体失败→逐字段重试” 策略。

        Returns:
            True 如果至少成功写入一个字段，否则 False。
        """
        if scope == "video":
            status_field = self.PROJECT_AUDIT_VIDEO_STATUS_FIELD
            comment_field = self.PROJECT_AUDIT_VIDEO_COMMENT_FIELD
        elif scope == "product":
            status_field = self.PROJECT_AUDIT_PRODUCT_STATUS_FIELD
            comment_field = self.PROJECT_AUDIT_PRODUCT_COMMENT_FIELD
        else:
            raise ValueError(f"save_stage1_audit_result: 未知 scope={scope!r}")

        candidate_fields: dict[str, Any] = {status_field: status}
        if review_comment:
            candidate_fields[comment_field] = review_comment

        async def _try(fields: dict[str, Any]) -> bool:
            try:
                await asyncio.to_thread(self._projects.update, project_id, fields)
                return True
            except HTTPError as e:
                msg = str(e)
                if "UNKNOWN_FIELD_NAME" in msg:
                    return False
                logger.error(f"save_stage1_audit_result 写入失败 ({project_id}/{scope}): {msg}")
                return False
            except Exception as e:
                logger.error(f"save_stage1_audit_result 异常 ({project_id}/{scope}): {e}")
                return False

        if await _try(candidate_fields):
            logger.info(f"写入 Stage1 审查结果: {project_id}/{scope} → {status}")
            return True

        # 降级：逐字段尝试
        any_success = False
        for k, v in candidate_fields.items():
            if await _try({k: v}):
                any_success = True
            else:
                logger.warning(
                    f"save_stage1_audit_result: Projects 表缺失字段 '{k}'，跳过写入 ({project_id})"
                )
        return any_success

    async def save_keyframe_audit_result(
        self,
        shot_id: str,
        status: str,
        review_comment: str = "",
        attempt: Optional[int] = None,
    ) -> bool:
        """写回 Stage 3.5 关键帧审查结果到 Shots 表。

        Args:
            shot_id: 分镜 ID
            status: "待审核" / "已通过" / "已驳回" / "待人审"
            review_comment: 审查意见
            attempt: 当前尝试次数（级联重试使用，可选；字段缺失时自动跳过该项）

        字段缺失时降级为警告，不阻塞。
        """
        candidate_fields: dict[str, Any] = {
            self.SHOT_KEYFRAME_AUDIT_STATUS_FIELD: status,
        }
        if review_comment:
            candidate_fields[self.SHOT_KEYFRAME_AUDIT_COMMENT_FIELD] = review_comment
        if attempt is not None:
            candidate_fields[self.SHOT_KEYFRAME_AUDIT_ATTEMPT_FIELD] = int(attempt)

        async def _try(fields: dict[str, Any]) -> bool:
            try:
                await asyncio.to_thread(self._shots.update, shot_id, fields)
                return True
            except HTTPError as e:
                msg = str(e)
                if "UNKNOWN_FIELD_NAME" in msg:
                    return False
                logger.error(f"save_keyframe_audit_result 写入失败 ({shot_id}): {msg}")
                return False
            except Exception as e:
                logger.error(f"save_keyframe_audit_result 异常 ({shot_id}): {e}")
                return False

        if await _try(candidate_fields):
            logger.info(f"写入关键帧审查结果: {shot_id} → {status}")
            return True

        any_success = False
        for k, v in candidate_fields.items():
            if await _try({k: v}):
                any_success = True
            else:
                logger.warning(
                    f"save_keyframe_audit_result: Shots 表缺失字段 '{k}'，跳过写入 ({shot_id})"
                )
        return any_success

    async def update_shot_video_status(
        self, shot_id: str, status: str, review_comment: str = ""
    ) -> dict[str, Any]:
        """
        更新视频审核状态

        Args:
            shot_id: 分镜 ID
            status: 审核状态
            review_comment: 审核意见

        Returns:
            更新后的分镜记录
        """
        fields = {"视频审核状态": status}
        if review_comment:
            fields["视频审核意见"] = review_comment

        try:
            record = await asyncio.to_thread(self._shots.update, shot_id, fields)
            logger.info(f"更新视频审核状态: {shot_id} → {status}")
            return record
        except (HTTPError, Exception) as e:
            logger.error(f"更新视频审核状态失败: {e}")
            raise

    async def update_shot_status(
        self,
        shot_id: str,
        status: str,
        video_url: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        更新分镜的生成运行状态（不影响审核状态）

        Args:
            shot_id: 分镜 ID
            status: 新状态 (generating/completed/failed)
            video_url: 生成视频 URL（可选）

        Returns:
            更新后的分镜记录
        """
        fields = {"生成状态": status}
        if video_url:
            fields["生成视频"] = video_url

        try:
            record = await asyncio.to_thread(self._shots.update, shot_id, fields)
            logger.info(f"更新分镜生成状态: {shot_id} → {status}")
            return record
        except (HTTPError, Exception) as e:
            logger.error(f"更新分镜生成状态失败: {e}")
            raise

    async def check_all_shots_approved(
        self, project_id: str, review_type: str
    ) -> dict[str, Any]:
        """
        检查项目的所有镜头是否都已审核通过

        Args:
            project_id: 项目 ID
            review_type: "prompt" 或 "video"

        Returns:
            {"all_approved": bool, "total": int, "approved": int, "pending": int}
        """
        try:
            shots = await self.get_shots(project_id)
            status_field = (
                "提示词审核状态" if review_type == "prompt" else "视频审核状态"
            )

            total = len(shots)
            approved = sum(
                1 for s in shots if s["fields"].get(status_field) == "已通过"
            )
            pending = total - approved

            return {
                "all_approved": approved == total and total > 0,
                "total": total,
                "approved": approved,
                "pending": pending,
            }
        except (HTTPError, Exception) as e:
            logger.error(f"检查镜头审核状态失败: {e}")
            raise

    # ==========================================================================
    # 审核记录 (Reviews) 操作
    # ==========================================================================

    async def create_review(
        self,
        shot_id: str,
        review_type: str,
        result: str,
        description: str = "",
        suggestion: str = "",
    ) -> dict[str, Any]:
        """
        创建审核记录

        Args:
            shot_id: 关联镜头 ID
            review_type: 审核类型 (提示词审核/视频审核)
            result: 审核结果 (通过/需修改/重新生成)
            description: 问题描述
            suggestion: 修改建议

        Returns:
            创建的审核记录
        """
        fields = {
            "关联镜头": [shot_id],
            "审核类型": review_type,
            "审核结果": result,
            "问题描述": description,
            "修改建议": suggestion,
        }

        try:
            record = await asyncio.to_thread(self._reviews.create, fields)
            logger.info(f"创建审核记录: {review_type} - {result}")
            return record
        except (HTTPError, Exception) as e:
            logger.error(f"创建审核记录失败: {e}")
            raise

    async def create_review_from_dict(self, data: dict[str, Any]) -> dict[str, Any]:
        """
        从字典创建审核记录

        Args:
            data: 审核数据字典

        Returns:
            创建的审核记录
        """
        return await self.create_review(
            shot_id=data.get("shot_id", ""),
            review_type=data.get("review_type", ""),
            result=data.get("result", ""),
            description=data.get("description", ""),
            suggestion=data.get("suggestion", ""),
        )

    async def get_project_reviews(self, project_id: str) -> list[dict[str, Any]]:
        """
        获取项目的所有审核记录
        通过分镜关联查询

        Args:
            project_id: 项目 ID

        Returns:
            审核记录列表
        """
        try:
            shots = await self.get_shots(project_id)
            shot_ids = [s["id"] for s in shots]
            if not shot_ids:
                return []

            shot_id_set = set(shot_ids)
            records = await asyncio.to_thread(self._reviews.all)
            filtered = [
                r for r in records
                if any(sid in shot_id_set for sid in r.get("fields", {}).get("关联镜头", []))
            ]
            logger.info(f"get_project_reviews: found {len(filtered)}/{len(records)} reviews for {project_id}")
            return filtered
        except (HTTPError, Exception) as e:
            logger.error(f"获取项目审核记录失败: {e}")
            raise

    async def get_shot_reviews(self, shot_id: str) -> list[dict[str, Any]]:
        """
        获取分镜的审核记录

        Args:
            shot_id: 分镜 ID

        Returns:
            审核记录列表
        """
        try:
            records = await asyncio.to_thread(self._reviews.all)
            filtered = [
                r for r in records
                if shot_id in r.get("fields", {}).get("关联镜头", [])
            ]
            logger.info(f"get_shot_reviews: found {len(filtered)}/{len(records)} reviews for {shot_id}")
            return filtered
        except (HTTPError, Exception) as e:
            logger.error(f"获取分镜审核记录失败: {e}")
            raise

    # ==========================================================================
    # Token 消耗统计同步
    # ==========================================================================

    TOKEN_USAGE_TABLE = "TokenUsage"

    async def sync_token_usage(self, project_id: str, summary: dict[str, Any]) -> None:
        """
        将项目的 Token 消耗汇总同步到 Airtable TokenUsage 表

        Args:
            project_id: 项目 ID
            summary: TokenTracker.get_project_summary() 的返回值
        """
        from datetime import datetime

        try:
            table = self.api.table(self.base_id, self.TOKEN_USAGE_TABLE)

            # 按阶段写入/更新记录
            by_stage = summary.get("by_stage", {})
            for stage, data in by_stage.items():
                # 确定该阶段主要使用的 API
                by_model = summary.get("by_model", {})
                api_used = "Gemini"  # 默认
                if stage == "stage3" and "qwen-plus" in str(by_model):
                    api_used = "Gemini+Qwen"

                # 映射 stage 到可读的 Usage Purpose
                stage_purpose_map = {
                    "stage1": "视频/商品/节奏分析",
                    "stage2": "分镜脚本生成",
                    "stage3": "提示词转换+审核",
                    "stage4": "视频生成",
                    "stage5": "视频合成",
                }
                usage_purpose = stage_purpose_map.get(stage, stage)

                # 计算效率指标：平均每次调用 Token 数
                call_count = data.get("call_count", 1) or 1
                avg_tokens = data.get("total_tokens", 0) // call_count
                efficiency = f"{avg_tokens} tokens/call"

                fields = {
                    "ProjectID": project_id,
                    "Stage": stage,
                    "TotalInputTokens": data.get("input_tokens", 0),
                    "TotalOutputTokens": data.get("output_tokens", 0),
                    "TotalTokens": data.get("total_tokens", 0),
                    "CallCount": data.get("call_count", 0),
                    "EstimatedCostUSD": round(data.get("cost_usd", 0.0), 6),
                    # Airtable AI 生成的字段
                    "项目": project_id,
                    "Date Time": datetime.now().isoformat(),
                    "API Used": api_used,
                    "Usage Purpose": usage_purpose,
                    "User/Operator": "system",
                    "Efficiency Commentary": efficiency,
                    "Success?": "Yes",
                }

                # 尝试查找已有记录（按 ProjectID + Stage 唯一）
                try:
                    formula = f"AND({{ProjectID}}='{project_id}', {{Stage}}='{stage}')"
                    existing = await asyncio.to_thread(
                        table.all, formula=formula
                    )
                    if existing:
                        record_id = existing[0]["id"]
                        await asyncio.to_thread(
                            table.update, record_id, fields
                        )
                        logger.debug(f"TokenUsage updated: {project_id}/{stage}")
                    else:
                        await asyncio.to_thread(table.create, fields)
                        logger.debug(f"TokenUsage created: {project_id}/{stage}")
                except Exception as inner_e:
                    logger.warning(f"TokenUsage sync failed for {project_id}/{stage}: {inner_e}")

            logger.info(
                f"Token usage synced to Airtable for project {project_id}: "
                f"{len(by_stage)} stages, total_cost=${summary.get('total_cost_usd', 0):.4f}"
            )
        except Exception as e:
            # 同步失败不阻塞主流程
            logger.warning(f"Token usage Airtable sync failed (non-blocking): {e}")
