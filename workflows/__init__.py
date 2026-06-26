"""
工作流模块
定义语义复刻视频生成的五个阶段
"""

from .stage1_preparation import run_stage1, stage1_preparation
from .stage2_script import run_stage2, stage2_script
from .stage3_prompts import run_stage3, stage3_prompts
from .stage3_5_keyframes import run_stage3_5, stage3_5_keyframes
from .stage4_generation import run_stage4, stage4_generation
from .stage4_5_clip_editing import run_clip_editing, stage4_5_clip_editing
from .stage5_composition import run_stage5, stage5_composition
from .full_workflow import run_full_workflow

__all__ = [
    "run_stage1",
    "run_stage2",
    "run_stage3",
    "run_stage3_5",
    "run_stage4",
    "run_clip_editing",
    "run_stage5",
    "run_full_workflow",
    "stage1_preparation",
    "stage2_script",
    "stage3_prompts",
    "stage3_5_keyframes",
    "stage4_generation",
    "stage4_5_clip_editing",
    "stage5_composition",
]
