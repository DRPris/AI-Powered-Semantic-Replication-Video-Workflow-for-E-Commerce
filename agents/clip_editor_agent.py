"""
ClipEditorAgent: 复刻剪辑 Agent 主类
======================================

位置：Stage 4（视频生成）之后、Stage 5（合成）之前。

职责：
- 基于原视频 rhythm_analysis + video_analysis 的逐镜头时间轴，为每个"超长生成 clip"
  产出精确的 edit_plan（裁剪区间 + 变速倍率），使成片节奏与原视频 1:1 对齐。
- Phase 1：仅规则层（零 LLM 成本），策略为 `trim_head`（从头裁剪到目标时长）。
- Phase 2：接入 Gemini 视频理解做语义选段（trim_semantic）。
- Phase 3：开启变速兜底（speed_up / trim_and_speed）。

设计原则：
- 任何异常都降级为"不阻塞主流程"，给出兜底 edit_plan。
- 字段缺失时对 Airtable 写入静默跳过，保持向下兼容。
"""

from __future__ import annotations

import logging
from typing import Optional

from models.schemas import EditPlan, EditTrim, ClipEditorResponse

logger = logging.getLogger(__name__)


# 策略常量
STRATEGY_NO_OP = "no_op"
STRATEGY_TRIM_HEAD = "trim_head"
STRATEGY_TRIM_SEMANTIC = "trim_semantic"
STRATEGY_SPEED_UP = "speed_up"
STRATEGY_TRIM_AND_SPEED = "trim_and_speed"

# 触发 no_op 的时长差阈值
NO_OP_DURATION_DELTA = 0.3

# 单镜头最短时长保护（成片不应出现 <1s 的瞬闪）
MIN_SHOT_DURATION = 1.0

# 变速倍率硬上限（Phase 3）
MAX_SPEED_MULTIPLIER = 1.5

# 节拍吸附半径（秒）：裁剪点距离节拍 <= 此值时才吸附
BEAT_SNAP_RADIUS = 0.15


class ClipEditorAgent:
    """复刻剪辑 Agent

    典型用法：
        agent = ClipEditorAgent(
            rhythm_analysis=rhythm_dict,
            video_analysis=va_dict,
            enable_llm_semantic_pick=False,
            enable_speed_adjust=False,
        )
        response = await agent.plan(
            approved_shots=approved_shots,
            source_durations=[5.0, 5.0, 5.0],
        )
        for edit_plan in response.edit_plans:
            ...
    """

    def __init__(
        self,
        rhythm_analysis: Optional[dict] = None,
        video_analysis: Optional[dict] = None,
        gemini_service=None,
        enable_llm_semantic_pick: bool = False,
        enable_speed_adjust: bool = False,
    ) -> None:
        self.rhythm_analysis = rhythm_analysis or {}
        self.video_analysis = video_analysis or {}
        self.gemini = gemini_service
        self.enable_llm_semantic_pick = enable_llm_semantic_pick
        self.enable_speed_adjust = enable_speed_adjust

        # 从节奏分析中提取节拍时间轴（已排序），用于裁剪点吸附
        audio = self.rhythm_analysis.get("audio") or {}
        raw_beats = audio.get("beat_positions_sec") or []
        self._beat_positions: list[float] = sorted(
            float(b) for b in raw_beats if isinstance(b, (int, float)) and b >= 0
        )

    # ------------------------------------------------------------------
    # Public entry
    # ------------------------------------------------------------------

    async def plan(
        self,
        approved_shots: list[dict],
        source_durations: list[float],
        project_id: str = "",
        clip_video_bytes_list: Optional[list[Optional[bytes]]] = None,
    ) -> ClipEditorResponse:
        """为每个镜头规划 edit_plan。

        Args:
            approved_shots: 审核通过的镜头（从 Airtable 获取），按镜头序号排序
            source_durations: 每个生成 clip 的实际时长（秒），与 approved_shots 一一对应
            project_id: 项目 ID，仅用于日志
            clip_video_bytes_list: 每个生成 clip 的字节数据（Phase 2 语义选段用），
                                  与 approved_shots 一一对应，None 则不启用语义选段
        """
        total = len(approved_shots)
        if len(source_durations) != total:
            raise ValueError(
                f"source_durations 长度 ({len(source_durations)}) 必须与 approved_shots ({total}) 一致"
            )

        warnings: list[str] = []

        # Step 1: 时长预算规划
        target_durations = self._allocate_target_durations(total, warnings)

        # Step 2: 逐镜头产出 edit_plan
        edit_plans: list[EditPlan] = []
        for idx in range(total):
            shot_fields = (approved_shots[idx] or {}).get("fields", {}) or {}
            shot_number = int(shot_fields.get("镜头序号", idx + 1) or (idx + 1))
            src_dur = float(source_durations[idx] or 0.0)
            tgt_dur = float(target_durations[idx])

            if src_dur <= 0:
                warnings.append(f"镜头 {shot_number}: source_duration=0，跳过")
                edit_plans.append(self._no_op_plan(shot_number, src_dur, tgt_dur))
                continue

            try:
                clip_bytes = (
                    clip_video_bytes_list[idx]
                    if clip_video_bytes_list and idx < len(clip_video_bytes_list)
                    else None
                )
                plan = await self._plan_single_shot(
                    shot_number=shot_number,
                    source_duration=src_dur,
                    target_duration=tgt_dur,
                    clip_video_bytes=clip_bytes,
                )
            except Exception as e:
                logger.warning(
                    f"[ClipEditorAgent] 镜头 {shot_number} 规划失败，降级为 trim_head: {e}"
                )
                plan = self._trim_head_plan(
                    shot_number=shot_number,
                    source_duration=src_dur,
                    target_duration=tgt_dur,
                    reasoning=f"规划异常降级: {e}",
                )

            # 节拍吸附：裁剪点对齐到最近的 beat_positions_sec
            plan = self._apply_beat_snap(plan, idx, warnings)

            edit_plans.append(plan)

        src_total = sum(source_durations)
        tgt_total = sum(target_durations)
        expected_total = sum(
            self._plan_expected_duration(p, src_durations=source_durations, index=i)
            for i, p in enumerate(edit_plans)
        )

        response = ClipEditorResponse(
            project_id=project_id,
            total_shots=total,
            success_count=sum(1 for p in edit_plans if p.strategy != STRATEGY_NO_OP or abs(p.source_duration - p.target_duration) <= NO_OP_DURATION_DELTA),
            source_total_duration=round(src_total, 3),
            target_total_duration=round(tgt_total, 3),
            expected_output_duration=round(expected_total, 3),
            edit_plans=edit_plans,
            warnings=warnings,
        )

        logger.info(
            f"[ClipEditorAgent] project={project_id} shots={total} "
            f"src_total={src_total:.2f}s -> target={tgt_total:.2f}s (expected {expected_total:.2f}s)"
        )
        return response

    # ------------------------------------------------------------------
    # Step 1: 时长预算规划
    # ------------------------------------------------------------------

    def _allocate_target_durations(
        self, total_shots: int, warnings: list[str]
    ) -> list[float]:
        """按原视频节奏分析为每个审核通过镜头分配目标时长。

        策略：
        - 若 rhythm_analysis.shots 数量 >= total_shots：按序一一匹配。
        - 若 rhythm_analysis.shots 数量 < total_shots：按总时长均摊。
        - 若完全没有节奏数据：默认每个镜头 3s（宽松兜底，不裁剪过度）。
        - 单镜头最短时长保护：< MIN_SHOT_DURATION 的镜头升至 1.0s，并从最长镜头按比例扣减。
        """
        rhythm_shots = (self.rhythm_analysis or {}).get("shots") or []
        overview = (self.rhythm_analysis or {}).get("overview") or {}
        total_video_duration = float(overview.get("total_duration_sec") or 0.0)

        # 情况 1: 镜头数 >= total_shots，按序一一匹配
        if len(rhythm_shots) >= total_shots and total_shots > 0:
            targets = [float(rhythm_shots[i].get("duration_sec") or 0.0) for i in range(total_shots)]
            # 处理 0 或负数
            for i, t in enumerate(targets):
                if t <= 0:
                    targets[i] = 3.0
                    warnings.append(f"镜头 {i+1}: rhythm.duration_sec 异常，使用默认 3.0s")
        # 情况 2: 镜头数不足 —— 按总时长均摊
        elif total_video_duration > 0 and total_shots > 0:
            avg = total_video_duration / total_shots
            targets = [avg] * total_shots
            warnings.append(
                f"rhythm_shots 不足 ({len(rhythm_shots)} < {total_shots})，按总时长 {total_video_duration:.2f}s 均摊"
            )
        # 情况 3: 无节奏数据 —— 默认 3s
        else:
            targets = [3.0] * total_shots
            warnings.append("无节奏分析数据，每个镜头默认 3.0s")

        # 最短时长保护
        targets = self._enforce_min_duration(targets, warnings)

        return targets

    def _enforce_min_duration(
        self, targets: list[float], warnings: list[str]
    ) -> list[float]:
        """确保所有镜头时长 >= MIN_SHOT_DURATION，从最长镜头按比例扣减补偿。"""
        if not targets:
            return targets
        total = sum(targets)
        short_indices = [i for i, t in enumerate(targets) if t < MIN_SHOT_DURATION]
        if not short_indices:
            return targets

        need_add = sum(MIN_SHOT_DURATION - targets[i] for i in short_indices)
        long_indices = [i for i in range(len(targets)) if i not in short_indices]
        long_total = sum(targets[i] for i in long_indices)

        if long_total <= need_add:
            # 补偿不足，直接把所有 short 的升到 MIN，总时长会轻微膨胀
            warnings.append("短镜头补偿池不足，成片总时长会轻微膨胀")
            for i in short_indices:
                targets[i] = MIN_SHOT_DURATION
            return targets

        # 按比例从长镜头扣减
        for i in short_indices:
            targets[i] = MIN_SHOT_DURATION
        for i in long_indices:
            ratio = targets[i] / long_total
            targets[i] = max(MIN_SHOT_DURATION, targets[i] - need_add * ratio)

        # 四舍五入到 3 位小数
        targets = [round(t, 3) for t in targets]
        logger.info(
            f"[ClipEditorAgent] 最短时长保护：原总时长 {total:.2f}s -> 调整后 {sum(targets):.2f}s"
        )
        return targets

    # ------------------------------------------------------------------
    # Step 2: 单镜头策略决策
    # ------------------------------------------------------------------

    async def _plan_single_shot(
        self,
        shot_number: int,
        source_duration: float,
        target_duration: float,
        clip_video_bytes: Optional[bytes] = None,
    ) -> EditPlan:
        """为单个镜头决定剪辑策略。

        决策优先级:
        1. no_op     — 时长差极小，不做处理
        2. no_op     — 源比目标短（暂不拉伸）
        3. speed_up  — Phase 3: 纯变速即可满足(<=1.5x)，保留完整内容
        4. semantic  — Phase 2: LLM 语义选段
        5. trim_and_speed — Phase 3: 先裁剪再微调变速
        6. trim_head — Phase 1: 从头裁剪（最终兜底）
        """
        delta = source_duration - target_duration

        # Case 1: 时长差 <= 阈值，no_op
        if abs(delta) <= NO_OP_DURATION_DELTA:
            return self._no_op_plan(shot_number, source_duration, target_duration)

        # Case 2: source < target，生成 clip 反而比目标还短
        if delta < 0:
            return EditPlan(
                shot_number=shot_number,
                source_duration=source_duration,
                target_duration=target_duration,
                strategy=STRATEGY_NO_OP,
                trim=None,
                speed=1.0,
                confidence=0.3,
                reasoning=f"源时长 {source_duration:.2f}s 短于目标 {target_duration:.2f}s，暂不拉伸",
            )

        # Case 3: source > target —— 需要缩短

        # Phase 3 优先判断：纯变速即可解决 —— 保留全部视频内容
        if self.enable_speed_adjust:
            speed_needed = source_duration / target_duration
            if speed_needed <= MAX_SPEED_MULTIPLIER:
                return self._speed_up_plan(
                    shot_number=shot_number,
                    source_duration=source_duration,
                    target_duration=target_duration,
                    speed=speed_needed,
                )

        # Phase 2: 若启用语义选段，调用 LLM
        if self.enable_llm_semantic_pick and self.gemini and clip_video_bytes:
            try:
                semantic_plan = await self._semantic_pick_via_llm(
                    shot_number=shot_number,
                    source_duration=source_duration,
                    target_duration=target_duration,
                    clip_video_bytes=clip_video_bytes,
                )
                if semantic_plan and semantic_plan.confidence >= 0.7:
                    return semantic_plan
            except Exception as e:
                logger.warning(
                    f"[ClipEditorAgent] 镜头 {shot_number} 语义选段失败，降级: {e}"
                )

        # Phase 3 trim_and_speed：裁剪后再微调变速使时长精确匹配
        if self.enable_speed_adjust and delta > NO_OP_DURATION_DELTA:
            plan = self._trim_and_speed_plan(
                shot_number=shot_number,
                source_duration=source_duration,
                target_duration=target_duration,
            )
            if plan:
                return plan

        # Phase 1 兜底：trim_head
        return self._trim_head_plan(
            shot_number=shot_number,
            source_duration=source_duration,
            target_duration=target_duration,
            reasoning=f"Phase 1 规则层：从头裁剪 {source_duration:.2f}s -> {target_duration:.2f}s",
        )

    # ------------------------------------------------------------------
    # 节拍吸附（Beat Snap）
    # ------------------------------------------------------------------

    def _apply_beat_snap(
        self, plan: EditPlan, shot_index: int, warnings: list[str]
    ) -> EditPlan:
        """将 plan 中 trim 的起止点吸附到最近的节拍位置。

        只在以下条件同时满足时吸附：
        1. plan 有 trim 区间
        2. 存在有效的 beat_positions 数据
        3. 原镜头标记为 beat_aligned（来自 rhythm_analysis）
        4. 吸附距离 <= BEAT_SNAP_RADIUS

        吸附后会修正 trim 区间并更新 reasoning。
        """
        if not plan.trim or not self._beat_positions:
            return plan

        # 检查该镜头是否被标记为需要节拍对齐
        ra_shots = self.rhythm_analysis.get("shots") or []
        shot_ra = ra_shots[shot_index] if shot_index < len(ra_shots) else {}
        if not shot_ra.get("beat_aligned", False):
            return plan

        start = float(plan.trim.start_sec)
        end = float(plan.trim.end_sec)
        snapped_start = self._snap_to_beat(start)
        snapped_end = self._snap_to_beat(end)
        changed = False

        if snapped_start != start:
            start = snapped_start
            changed = True
        if snapped_end != end:
            end = snapped_end
            changed = True

        if not changed:
            return plan

        # 安全校验：吸附后的 start 不能 >= end，end 不能超过 source
        if start >= end or end > plan.source_duration:
            return plan

        snapped_dur = end - start
        if snapped_dur < MIN_SHOT_DURATION:
            return plan

        plan.trim = EditTrim(start_sec=round(start, 3), end_sec=round(end, 3))
        snap_note = f" [节拍吸附: {start:.3f}s-{end:.3f}s]"
        plan.reasoning = (plan.reasoning or "") + snap_note
        logger.debug(
            f"[ClipEditorAgent] 镜头 {plan.shot_number} 裁剪点节拍吸附: {snap_note}"
        )
        return plan

    def _snap_to_beat(self, time_sec: float) -> float:
        """将时间点吸附到最近的节拍位置（在 BEAT_SNAP_RADIUS 内）。"""
        if not self._beat_positions:
            return time_sec

        import bisect
        idx = bisect.bisect_left(self._beat_positions, time_sec)
        candidates = []
        if idx > 0:
            candidates.append(self._beat_positions[idx - 1])
        if idx < len(self._beat_positions):
            candidates.append(self._beat_positions[idx])

        if not candidates:
            return time_sec

        nearest = min(candidates, key=lambda b: abs(b - time_sec))
        if abs(nearest - time_sec) <= BEAT_SNAP_RADIUS:
            return nearest
        return time_sec

    # ------------------------------------------------------------------
    # Plan 构造器
    # ------------------------------------------------------------------

    @staticmethod
    def _no_op_plan(
        shot_number: int, source_duration: float, target_duration: float
    ) -> EditPlan:
        return EditPlan(
            shot_number=shot_number,
            source_duration=source_duration,
            target_duration=target_duration,
            strategy=STRATEGY_NO_OP,
            trim=None,
            speed=1.0,
            confidence=1.0,
            reasoning=f"时长差 {abs(source_duration - target_duration):.2f}s <= {NO_OP_DURATION_DELTA}s，无需剪辑",
        )

    @staticmethod
    def _trim_head_plan(
        shot_number: int,
        source_duration: float,
        target_duration: float,
        reasoning: str = "",
    ) -> EditPlan:
        end_sec = min(target_duration, source_duration)
        return EditPlan(
            shot_number=shot_number,
            source_duration=source_duration,
            target_duration=target_duration,
            strategy=STRATEGY_TRIM_HEAD,
            trim=EditTrim(start_sec=0.0, end_sec=end_sec),
            speed=1.0,
            confidence=0.6,
            reasoning=reasoning or "规则层从头裁剪",
            fallback={
                "strategy": STRATEGY_TRIM_HEAD,
                "trim": {"start_sec": 0.0, "end_sec": end_sec},
            },
        )

    @staticmethod
    def _speed_up_plan(
        shot_number: int,
        source_duration: float,
        target_duration: float,
        speed: float,
    ) -> EditPlan:
        """Phase 3: 纯变速 —— 加速播放使 source 刚好压缩到 target。

        适用于 source 略长于 target（所需倍率 <= MAX_SPEED_MULTIPLIER）的场景，
        比裁剪更优因为保留了视频的全部画面内容。
        """
        speed = round(speed, 4)
        return EditPlan(
            shot_number=shot_number,
            source_duration=source_duration,
            target_duration=target_duration,
            strategy=STRATEGY_SPEED_UP,
            trim=None,
            speed=speed,
            confidence=0.8,
            reasoning=(
                f"Phase 3 变速：{speed:.2f}x 加速 "
                f"({source_duration:.2f}s -> {target_duration:.2f}s)，保留完整内容"
            ),
            fallback={
                "strategy": STRATEGY_TRIM_HEAD,
                "trim": {"start_sec": 0.0, "end_sec": min(target_duration, source_duration)},
            },
        )

    @staticmethod
    def _trim_and_speed_plan(
        shot_number: int,
        source_duration: float,
        target_duration: float,
    ) -> Optional[EditPlan]:
        """Phase 3: 先裁剪再微调变速 —— 先 trim 掉大块多余，再小幅变速精修。

        策略：先从头裁剪到 target_duration * MAX_SPEED_MULTIPLIER（留出变速裕量），
        然后用变速微调到精确目标时长。若最终所需变速 > MAX_SPEED_MULTIPLIER 则放弃。
        """
        # 裁剪后保留的时长上限：让变速后刚好等于 target
        # speed = trim_duration / target_duration，限制 speed <= MAX_SPEED_MULTIPLIER
        # => trim_duration <= target_duration * MAX_SPEED_MULTIPLIER
        max_keep = target_duration * MAX_SPEED_MULTIPLIER
        # 实际保留的时长不超过源时长
        trim_end = min(max_keep, source_duration)
        speed = trim_end / target_duration

        if speed > MAX_SPEED_MULTIPLIER or speed < 1.0:
            return None

        speed = round(speed, 4)
        return EditPlan(
            shot_number=shot_number,
            source_duration=source_duration,
            target_duration=target_duration,
            strategy=STRATEGY_TRIM_AND_SPEED,
            trim=EditTrim(start_sec=0.0, end_sec=round(trim_end, 3)),
            speed=speed,
            confidence=0.7,
            reasoning=(
                f"Phase 3 裁剪+变速：先取前 {trim_end:.2f}s，"
                f"再 {speed:.2f}x 加速 -> {target_duration:.2f}s"
            ),
            fallback={
                "strategy": STRATEGY_TRIM_HEAD,
                "trim": {"start_sec": 0.0, "end_sec": min(target_duration, source_duration)},
            },
        )

    async def _semantic_pick_via_llm(
        self,
        shot_number: int,
        source_duration: float,
        target_duration: float,
        clip_video_bytes: bytes,
    ) -> Optional[EditPlan]:
        """Phase 2: 调用 Gemini 做语义选段，产出 trim_semantic 策略。

        从 video_analysis 和 rhythm_analysis 提取原镜头语义信息，
        将生成 clip 字节 + 语义摘要发送给 Gemini，输出最佳连续窗口。
        """
        # 1. 从 video_analysis 提取原镜头的动作描述与视觉锚点
        va_shots = (self.video_analysis or {}).get("shots") or []
        action_description = ""
        visual_anchors = ""
        if shot_number - 1 < len(va_shots):
            va_shot = va_shots[shot_number - 1] or {}
            action = va_shot.get("action") or {}
            # 拼接 person_hand + product 作为动作描述
            parts = []
            if action.get("person_hand"):
                parts.append(action["person_hand"])
            if action.get("product"):
                parts.append(action["product"])
            if not parts and action.get("narrative_role"):
                parts.append(action["narrative_role"])
            action_description = "; ".join(parts) or "no description"
            # 视觉锚点：product_specific_elements + scene_elements
            anchors = []
            for elem in (va_shot.get("product_specific_elements") or []):
                if isinstance(elem, str):
                    anchors.append(elem)
            for elem in (va_shot.get("scene_elements") or []):
                if isinstance(elem, str):
                    anchors.append(elem)
            visual_anchors = ", ".join(anchors[:5]) if anchors else ""

        # 2. 从 rhythm_analysis 提取节奏信息
        ra_shots = (self.rhythm_analysis or {}).get("shots") or []
        pace = "medium"
        original_duration = target_duration  # 兜底
        if shot_number - 1 < len(ra_shots):
            ra_shot = ra_shots[shot_number - 1] or {}
            pace = ra_shot.get("pace", "medium")
            original_duration = float(ra_shot.get("duration_sec") or target_duration)

        # 3. 调用 Gemini 语义选段
        result = await self.gemini.semantic_clip_pick(
            video_bytes=clip_video_bytes,
            shot_number=shot_number,
            source_duration=source_duration,
            target_duration=target_duration,
            original_duration=original_duration,
            action_description=action_description,
            pace=pace,
            visual_anchors=visual_anchors,
        )

        # 4. 校验结果
        confidence = float(result.get("confidence", 0.0))
        if confidence < 0.7:
            logger.info(
                f"[ClipEditorAgent] 镜头 {shot_number} 语义选段置信度不足 "
                f"({confidence:.2f} < 0.7)，降级 trim_head"
            )
            return None

        window = result.get("best_window") or {}
        start_sec = float(window.get("start_sec", 0.0))
        end_sec = float(window.get("end_sec", 0.0))
        window_dur = end_sec - start_sec

        # 窗口时长与目标时长差距校验
        if abs(window_dur - target_duration) > 0.2:
            logger.warning(
                f"[ClipEditorAgent] 镜头 {shot_number} 窗口时长 {window_dur:.2f}s "
                f"与目标 {target_duration:.2f}s 差距 > 0.2s，降级 trim_head"
            )
            return None

        # 边界校验
        if start_sec < 0:
            start_sec = 0.0
        if end_sec > source_duration:
            end_sec = source_duration

        # 5. 构造 EditPlan
        keep_anchors = []
        for anchor in (result.get("semantic_anchors") or []):
            if isinstance(anchor, dict) and anchor.get("description"):
                keep_anchors.append(anchor["description"])
            elif isinstance(anchor, str):
                keep_anchors.append(anchor)

        return EditPlan(
            shot_number=shot_number,
            source_duration=source_duration,
            target_duration=target_duration,
            strategy=STRATEGY_TRIM_SEMANTIC,
            trim=EditTrim(start_sec=start_sec, end_sec=end_sec),
            speed=1.0,
            keep_anchors=keep_anchors[:5],
            confidence=confidence,
            reasoning=result.get("reasoning", "Gemini 语义选段"),
            fallback={
                "strategy": STRATEGY_TRIM_HEAD,
                "trim": {"start_sec": 0.0, "end_sec": min(target_duration, source_duration)},
            },
        )

    # ------------------------------------------------------------------
    # 预估输出时长（用于 Response.expected_output_duration）
    # ------------------------------------------------------------------

    @staticmethod
    def _plan_expected_duration(
        plan: EditPlan,
        src_durations: list[float],
        index: int,
    ) -> float:
        """根据 plan 估算剪辑后实际时长。"""
        if plan.strategy == STRATEGY_NO_OP:
            return float(src_durations[index])

        trim = plan.trim
        if trim is None:
            base = float(src_durations[index])
        else:
            base = max(0.0, float(trim.end_sec) - float(trim.start_sec))

        speed = max(plan.speed or 1.0, 0.01)
        return base / speed
