"""ClipEditorAgent 单元测试

覆盖：
- Phase 1: no_op / trim_head 策略
- Phase 3: speed_up / trim_and_speed 策略
- 时长预算分配（_allocate_target_durations）
- 最短时长保护（_enforce_min_duration）
- 节拍吸附（_apply_beat_snap / _snap_to_beat）
- 异常降级
- 边界条件
"""

from __future__ import annotations

import pytest
from unittest.mock import patch

from agents.clip_editor_agent import (
    ClipEditorAgent,
    STRATEGY_NO_OP,
    STRATEGY_TRIM_HEAD,
    STRATEGY_SPEED_UP,
    STRATEGY_TRIM_AND_SPEED,
    MIN_SHOT_DURATION,
    MAX_SPEED_MULTIPLIER,
)
from models.schemas import EditPlan


# ============================================================================
# 辅助工厂
# ============================================================================

def _rhythm(
    shots: list[dict] | None = None,
    total_duration: float = 15.0,
    beat_positions: list[float] | None = None,
) -> dict:
    """构造最小化 rhythm_analysis dict"""
    return {
        "overview": {"total_duration_sec": total_duration},
        "audio": {"beat_positions_sec": beat_positions or []},
        "shots": shots or [],
    }


def _shot(shot_number: int, duration_sec: float = 3.0, beat_aligned: bool = False, **kw) -> dict:
    """构造 rhythm_analysis.shots 中的单个镜头"""
    d = {"shot_number": shot_number, "duration_sec": duration_sec, "beat_aligned": beat_aligned}
    d.update(kw)
    return d


def _approved_shot(shot_number: int) -> dict:
    """构造 Airtable 格式的 approved_shot"""
    return {"fields": {"镜头序号": shot_number}}


# ============================================================================
# Phase 1: no_op / trim_head
# ============================================================================

class TestPhase1:
    """Phase 1 基础策略测试"""

    @pytest.mark.asyncio
    async def test_no_op_when_durations_match(self):
        """时长差在阈值内 -> no_op"""
        agent = ClipEditorAgent(
            rhythm_analysis=_rhythm(shots=[_shot(1, 3.0)]),
        )
        resp = await agent.plan(
            approved_shots=[_approved_shot(1)],
            source_durations=[3.1],  # delta=0.1 < 0.3
        )
        assert resp.edit_plans[0].strategy == STRATEGY_NO_OP

    @pytest.mark.asyncio
    async def test_no_op_when_source_shorter(self):
        """源比目标短 -> no_op（暂不拉伸）"""
        agent = ClipEditorAgent(
            rhythm_analysis=_rhythm(shots=[_shot(1, 5.0)]),
        )
        resp = await agent.plan(
            approved_shots=[_approved_shot(1)],
            source_durations=[3.0],  # 3.0 < 5.0
        )
        plan = resp.edit_plans[0]
        assert plan.strategy == STRATEGY_NO_OP
        assert plan.confidence == 0.3

    @pytest.mark.asyncio
    async def test_trim_head_when_source_longer(self):
        """源比目标长、Phase 2/3 未启用 -> trim_head"""
        agent = ClipEditorAgent(
            rhythm_analysis=_rhythm(shots=[_shot(1, 3.0)]),
            enable_llm_semantic_pick=False,
            enable_speed_adjust=False,
        )
        resp = await agent.plan(
            approved_shots=[_approved_shot(1)],
            source_durations=[5.0],  # 5.0 > 3.0
        )
        plan = resp.edit_plans[0]
        assert plan.strategy == STRATEGY_TRIM_HEAD
        assert plan.trim is not None
        assert plan.trim.start_sec == 0.0
        assert plan.trim.end_sec == 3.0
        assert plan.speed == 1.0

    @pytest.mark.asyncio
    async def test_zero_source_duration(self):
        """source_duration=0 -> no_op + 警告"""
        agent = ClipEditorAgent(
            rhythm_analysis=_rhythm(shots=[_shot(1, 3.0)]),
        )
        resp = await agent.plan(
            approved_shots=[_approved_shot(1)],
            source_durations=[0.0],
        )
        assert resp.edit_plans[0].strategy == STRATEGY_NO_OP
        assert any("source_duration=0" in w for w in resp.warnings)


# ============================================================================
# Phase 3: speed_up / trim_and_speed
# ============================================================================

class TestPhase3:
    """Phase 3 变速策略测试"""

    @pytest.mark.asyncio
    async def test_speed_up_within_limit(self):
        """source/target <= 1.5 -> speed_up（保留全部内容）"""
        agent = ClipEditorAgent(
            rhythm_analysis=_rhythm(shots=[_shot(1, 3.0)]),
            enable_speed_adjust=True,
        )
        resp = await agent.plan(
            approved_shots=[_approved_shot(1)],
            source_durations=[4.0],  # 4.0/3.0 = 1.33 <= 1.5
        )
        plan = resp.edit_plans[0]
        assert plan.strategy == STRATEGY_SPEED_UP
        assert plan.trim is None
        assert 1.3 < plan.speed < 1.4

    @pytest.mark.asyncio
    async def test_speed_up_at_boundary(self):
        """恰好 1.5x -> speed_up"""
        agent = ClipEditorAgent(
            rhythm_analysis=_rhythm(shots=[_shot(1, 2.0)]),
            enable_speed_adjust=True,
        )
        resp = await agent.plan(
            approved_shots=[_approved_shot(1)],
            source_durations=[3.0],  # 3.0/2.0 = 1.5
        )
        assert resp.edit_plans[0].strategy == STRATEGY_SPEED_UP

    @pytest.mark.asyncio
    async def test_trim_and_speed_when_too_long(self):
        """source/target > 1.5 -> trim_and_speed（先裁后变速）"""
        agent = ClipEditorAgent(
            rhythm_analysis=_rhythm(shots=[_shot(1, 3.0)]),
            enable_speed_adjust=True,
            enable_llm_semantic_pick=False,
        )
        resp = await agent.plan(
            approved_shots=[_approved_shot(1)],
            source_durations=[8.0],  # 8.0/3.0 = 2.67 > 1.5
        )
        plan = resp.edit_plans[0]
        assert plan.strategy == STRATEGY_TRIM_AND_SPEED
        assert plan.trim is not None
        assert plan.trim.start_sec == 0.0
        assert plan.trim.end_sec <= 3.0 * MAX_SPEED_MULTIPLIER + 0.01
        assert 1.0 < plan.speed <= MAX_SPEED_MULTIPLIER

    @pytest.mark.asyncio
    async def test_speed_adjust_disabled_falls_to_trim_head(self):
        """enable_speed_adjust=False -> 即使 source/target <=1.5 也走 trim_head"""
        agent = ClipEditorAgent(
            rhythm_analysis=_rhythm(shots=[_shot(1, 3.0)]),
            enable_speed_adjust=False,
        )
        resp = await agent.plan(
            approved_shots=[_approved_shot(1)],
            source_durations=[4.0],
        )
        assert resp.edit_plans[0].strategy == STRATEGY_TRIM_HEAD


# ============================================================================
# 时长预算分配
# ============================================================================

class TestTargetDurationAllocation:
    """时长预算分配测试"""

    @pytest.mark.asyncio
    async def test_exact_match_rhythm_shots(self):
        """rhythm_shots 数量 = approved_shots 数量时按序匹配"""
        agent = ClipEditorAgent(
            rhythm_analysis=_rhythm(shots=[_shot(1, 2.0), _shot(2, 4.0), _shot(3, 1.5)]),
        )
        targets = agent._allocate_target_durations(3, [])
        assert targets[0] == 2.0
        assert targets[1] == 4.0
        assert targets[2] == max(MIN_SHOT_DURATION, 1.5)

    @pytest.mark.asyncio
    async def test_fewer_rhythm_shots_average(self):
        """rhythm_shots 不足时按总时长均摊"""
        agent = ClipEditorAgent(
            rhythm_analysis=_rhythm(shots=[_shot(1, 5.0)], total_duration=12.0),
        )
        warnings: list[str] = []
        targets = agent._allocate_target_durations(3, warnings)
        assert len(targets) == 3
        assert abs(sum(targets) - 12.0) < 0.1
        assert any("均摊" in w for w in warnings)

    @pytest.mark.asyncio
    async def test_no_rhythm_data_default(self):
        """无节奏数据时每个镜头默认 3s"""
        agent = ClipEditorAgent(rhythm_analysis={})
        warnings: list[str] = []
        targets = agent._allocate_target_durations(4, warnings)
        assert all(t == 3.0 for t in targets)
        assert any("无节奏分析数据" in w for w in warnings)


# ============================================================================
# 最短时长保护
# ============================================================================

class TestMinDurationProtection:
    """最短时长保护测试"""

    def test_short_shot_raised_to_min(self):
        agent = ClipEditorAgent()
        targets = agent._enforce_min_duration([0.5, 5.0, 3.0], [])
        assert targets[0] >= MIN_SHOT_DURATION
        assert all(t >= MIN_SHOT_DURATION for t in targets)

    def test_no_change_if_all_long(self):
        agent = ClipEditorAgent()
        original = [3.0, 5.0, 2.0]
        targets = agent._enforce_min_duration(list(original), [])
        assert targets == original

    def test_empty_list(self):
        agent = ClipEditorAgent()
        assert agent._enforce_min_duration([], []) == []


# ============================================================================
# 节拍吸附
# ============================================================================

class TestBeatSnap:
    """节拍对齐测试"""

    def test_snap_to_nearest_beat(self):
        agent = ClipEditorAgent(
            rhythm_analysis=_rhythm(beat_positions=[1.0, 2.0, 3.0, 4.0, 5.0]),
        )
        assert agent._snap_to_beat(2.05) == 2.0
        assert agent._snap_to_beat(2.95) == 3.0
        assert agent._snap_to_beat(2.5) == 2.5  # 距离 > BEAT_SNAP_RADIUS，不吸附

    def test_no_beats_no_snap(self):
        agent = ClipEditorAgent(rhythm_analysis=_rhythm(beat_positions=[]))
        assert agent._snap_to_beat(2.5) == 2.5

    @pytest.mark.asyncio
    async def test_beat_snap_applied_to_trim_plan(self):
        """beat_aligned=True 的镜头裁剪点会吸附到节拍"""
        agent = ClipEditorAgent(
            rhythm_analysis=_rhythm(
                shots=[_shot(1, 3.0, beat_aligned=True)],
                beat_positions=[0.0, 1.0, 2.0, 2.9, 4.0],
            ),
            enable_speed_adjust=False,
        )
        resp = await agent.plan(
            approved_shots=[_approved_shot(1)],
            source_durations=[5.0],  # trim_head to 3.0, but 2.9 is a beat
        )
        plan = resp.edit_plans[0]
        assert plan.trim is not None
        # end_sec 应被吸附到 2.9（距离 3.0 仅 0.1 < BEAT_SNAP_RADIUS）
        assert plan.trim.end_sec == 2.9

    @pytest.mark.asyncio
    async def test_no_snap_when_not_beat_aligned(self):
        """beat_aligned=False 的镜头不做节拍吸附"""
        agent = ClipEditorAgent(
            rhythm_analysis=_rhythm(
                shots=[_shot(1, 3.0, beat_aligned=False)],
                beat_positions=[0.0, 1.0, 2.0, 2.9, 4.0],
            ),
            enable_speed_adjust=False,
        )
        resp = await agent.plan(
            approved_shots=[_approved_shot(1)],
            source_durations=[5.0],
        )
        plan = resp.edit_plans[0]
        assert plan.trim is not None
        assert plan.trim.end_sec == 3.0  # 未吸附


# ============================================================================
# 异常降级
# ============================================================================

class TestFallback:
    """异常降级测试"""

    @pytest.mark.asyncio
    async def test_source_durations_length_mismatch(self):
        """source_durations 长度不匹配 -> 抛出 ValueError"""
        agent = ClipEditorAgent(
            rhythm_analysis=_rhythm(shots=[_shot(1, 3.0)]),
        )
        with pytest.raises(ValueError, match="长度"):
            await agent.plan(
                approved_shots=[_approved_shot(1)],
                source_durations=[3.0, 5.0],  # 2 != 1
            )

    @pytest.mark.asyncio
    async def test_plan_exception_falls_to_trim_head(self):
        """_plan_single_shot 异常时降级为 trim_head"""
        agent = ClipEditorAgent(
            rhythm_analysis=_rhythm(shots=[_shot(1, 3.0)]),
        )
        with patch.object(
            agent, "_plan_single_shot", side_effect=RuntimeError("test error")
        ):
            resp = await agent.plan(
                approved_shots=[_approved_shot(1)],
                source_durations=[5.0],
            )
        plan = resp.edit_plans[0]
        assert plan.strategy == STRATEGY_TRIM_HEAD
        assert "异常降级" in plan.reasoning


# ============================================================================
# 多镜头完整流程
# ============================================================================

class TestMultiShot:
    """多镜头端到端测试"""

    @pytest.mark.asyncio
    async def test_three_shots_mixed_strategies(self):
        """3 镜头混合场景：no_op + trim_head + speed_up"""
        agent = ClipEditorAgent(
            rhythm_analysis=_rhythm(
                shots=[_shot(1, 3.0), _shot(2, 4.0), _shot(3, 2.0)],
            ),
            enable_speed_adjust=True,
        )
        resp = await agent.plan(
            approved_shots=[_approved_shot(1), _approved_shot(2), _approved_shot(3)],
            source_durations=[3.1, 8.0, 2.5],
        )
        assert resp.total_shots == 3
        assert resp.edit_plans[0].strategy == STRATEGY_NO_OP     # 3.1 vs 3.0 -> no_op
        assert resp.edit_plans[1].strategy in (STRATEGY_TRIM_AND_SPEED, STRATEGY_TRIM_HEAD)  # 8.0 vs 4.0 -> need cut
        assert resp.edit_plans[2].strategy == STRATEGY_SPEED_UP   # 2.5/2.0=1.25 -> speed_up

    @pytest.mark.asyncio
    async def test_response_durations_consistent(self):
        """验证 response 中的时长汇总数值一致性"""
        agent = ClipEditorAgent(
            rhythm_analysis=_rhythm(
                shots=[_shot(1, 3.0), _shot(2, 3.0)],
                total_duration=6.0,
            ),
            enable_speed_adjust=False,
        )
        resp = await agent.plan(
            approved_shots=[_approved_shot(1), _approved_shot(2)],
            source_durations=[5.0, 5.0],
            project_id="test-proj",
        )
        assert resp.project_id == "test-proj"
        assert resp.source_total_duration == 10.0
        assert resp.target_total_duration == 6.0
        assert resp.expected_output_duration <= resp.source_total_duration


# ============================================================================
# _plan_expected_duration
# ============================================================================

class TestExpectedDuration:
    """预估输出时长计算测试"""

    def test_no_op_returns_source(self):
        plan = EditPlan(
            shot_number=1, source_duration=5.0, target_duration=3.0,
            strategy=STRATEGY_NO_OP, trim=None, speed=1.0, confidence=1.0,
            reasoning="test",
        )
        result = ClipEditorAgent._plan_expected_duration(plan, [5.0], 0)
        assert result == 5.0

    def test_trim_head_returns_trim_range(self):
        from models.schemas import EditTrim
        plan = EditPlan(
            shot_number=1, source_duration=5.0, target_duration=3.0,
            strategy=STRATEGY_TRIM_HEAD,
            trim=EditTrim(start_sec=0.0, end_sec=3.0),
            speed=1.0, confidence=0.6, reasoning="test",
        )
        result = ClipEditorAgent._plan_expected_duration(plan, [5.0], 0)
        assert result == 3.0

    def test_speed_up_divides_by_speed(self):
        plan = EditPlan(
            shot_number=1, source_duration=4.0, target_duration=3.0,
            strategy=STRATEGY_SPEED_UP, trim=None,
            speed=1.333, confidence=0.8, reasoning="test",
        )
        result = ClipEditorAgent._plan_expected_duration(plan, [4.0], 0)
        assert abs(result - 4.0 / 1.333) < 0.01
