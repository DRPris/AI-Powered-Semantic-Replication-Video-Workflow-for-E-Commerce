import asyncio
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 尝试导入 OpenCV，失败时设为 None 以便 graceful fallback
try:
    import cv2
    import numpy as np
    CV2_AVAILABLE = True
except ImportError:
    cv2 = None
    np = None
    CV2_AVAILABLE = False
    logger.warning("OpenCV 未安装，智能裁剪功能将不可用")


class FFmpegService:
    """使用 FFmpeg 进行本地视频合成"""
    
    def __init__(self, ffmpeg_bin: str = "ffmpeg", temp_dir: str = "./tmp"):
        self.ffmpeg_bin = ffmpeg_bin
        self.temp_dir = Path(temp_dir)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
    
    async def concatenate_videos(
        self,
        video_paths: list[str],
        output_path: Optional[str] = None,
        transition_duration: float = 0.5,
        output_format: str = "mp4",
    ) -> str:
        """
        按顺序拼接多个视频片段。
        
        Args:
            video_paths: 视频文件路径列表（按顺序）
            output_path: 输出文件路径（可选，默认自动生成）
            transition_duration: 转场时长（秒），0 表示无转场
            output_format: 输出格式
        
        Returns:
            输出文件的本地路径
        """
        import uuid
        
        if not video_paths:
            raise ValueError("视频路径列表不能为空")
        
        # 1. 创建 FFmpeg concat 文件
        concat_file = self.temp_dir / f"concat_{uuid.uuid4().hex}.txt"
        with open(concat_file, "w") as f:
            for file_path in video_paths:
                f.write(f"file '{file_path}'\n")
        
        # 2. 执行拼接
        if output_path is None:
            output_path = str(self.temp_dir / f"output_{uuid.uuid4().hex}.{output_format}")
        
        try:
            if transition_duration > 0 and len(video_paths) > 1:
                # 带交叉淡入淡出转场的拼接（使用 xfade filter）
                output_path = await self._concat_with_transitions(
                    video_paths, output_path, transition_duration
                )
            else:
                # 简单拼接（无转场）- 重新编码确保 QuickTime 兼容性
                cmd = [
                    self.ffmpeg_bin,
                    "-y",  # 覆盖输出
                    "-f", "concat",
                    "-safe", "0",
                    "-i", str(concat_file),
                    "-c:v", "libx264",
                    "-preset", "medium",
                    "-crf", "23",
                    "-pix_fmt", "yuv420p",  # QuickTime 兼容的像素格式
                    "-c:a", "aac",
                    "-b:a", "128k",
                    "-movflags", "+faststart",  # 优化网络播放
                    output_path,
                ]
                await self._run_ffmpeg(cmd)
        finally:
            # 清理 concat 文件
            try:
                os.unlink(concat_file)
            except OSError:
                pass
        
        logger.info(f"视频合成完成: {output_path}")
        return output_path
    
    async def _concat_with_transitions(
        self, input_files: list[str], output_path: str, transition_duration: float
    ) -> str:
        """使用 xfade 滤镜拼接视频并添加交叉淡入淡出转场"""
        if len(input_files) == 1:
            # 只有一个视频，直接复制
            import shutil
            shutil.copy2(input_files[0], output_path)
            return output_path
        
        # 获取每个视频的时长
        durations = []
        for f in input_files:
            duration = await self._get_video_duration(f)
            durations.append(duration)
        
        # 检测目标分辨率：使用第一个视频的分辨率作为基准
        target_w, target_h = await self._get_video_resolution(input_files[0])
        logger.info(f"xfade 合成目标分辨率: {target_w}x{target_h}")
        
        # 构建 xfade filter chain
        # xfade 需要逐步合并：先合并前两个，再与第三个合并...
        inputs = []
        for f in input_files:
            inputs.extend(["-i", f])
        
        # 先为每个输入添加 scale+pad 滤镜统一分辨率
        scale_parts = []
        for i in range(len(input_files)):
            scale_parts.append(
                f"[{i}:v]scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
                f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:color=black,"
                f"setsar=1[scaled{i}]"
            )
        
        filter_parts = list(scale_parts)
        current_offset = 0
        
        for i in range(len(input_files) - 1):
            if i == 0:
                src1 = "[scaled0]"
                src2 = "[scaled1]"
            else:
                src1 = f"[v{i}]"
                src2 = f"[scaled{i+1}]"
            
            current_offset = sum(durations[:i+1]) - transition_duration * (i + 1)
            if current_offset < 0:
                current_offset = 0
            
            out_label = f"[v{i+1}]" if i < len(input_files) - 2 else "[outv]"
            filter_parts.append(
                f"{src1}{src2}xfade=transition=fade:duration={transition_duration}:offset={current_offset:.2f}{out_label}"
            )

        # ---- 音频链：若所有镜头都有音轨，则做 acrossfade 同步淡化 ----
        has_audio_list = [await self._has_audio_track(f) for f in input_files]
        all_have_audio = all(has_audio_list)
        if all_have_audio:
            for i in range(len(input_files)):
                filter_parts.append(
                    f"[{i}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
                    f"asetpts=PTS-STARTPTS[a{i}]"
                )
            for i in range(len(input_files) - 1):
                src1_a = "[a0]" if i == 0 else f"[aout{i}]"
                src2_a = f"[a{i+1}]"
                out_a = "[outa]" if i == len(input_files) - 2 else f"[aout{i+1}]"
                filter_parts.append(
                    f"{src1_a}{src2_a}acrossfade=d={transition_duration}{out_a}"
                )

        filter_complex = ";".join(filter_parts)

        cmd = [
            self.ffmpeg_bin,
            "-y",
            *inputs,
            "-filter_complex", filter_complex,
            "-map", "[outv]",
        ]
        if all_have_audio:
            cmd += ["-map", "[outa]"]
        cmd += [
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", "23",
            "-pix_fmt", "yuv420p",  # QuickTime 兼容的像素格式
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",  # 优化网络播放
            output_path,
        ]
        
        await self._run_ffmpeg(cmd)
        if all_have_audio:
            logger.info(f"xfade 合成含音轨: {len(input_files)} 段 acrossfade 链")
        else:
            logger.warning(f"xfade 合成跳过音轨（部分镜头无音频）: has_audio={has_audio_list}")
        return output_path
    
    async def _get_video_resolution(self, file_path: str) -> tuple[int, int]:
        """获取视频分辨率 (width, height)"""
        import re
        cmd = [
            self.ffmpeg_bin,
            "-nostdin",
            "-i", file_path,
        ]
        result = await asyncio.to_thread(
            subprocess.run, cmd, capture_output=True, text=True,
            timeout=30
        )
        # 从 stderr 解析分辨率
        match = re.search(r'(\d{2,5})x(\d{2,5})', result.stderr)
        if match:
            w, h = int(match.group(1)), int(match.group(2))
            logger.info(f"视频分辨率: {file_path} -> {w}x{h}")
            return w, h
        logger.warning(f"获取视频分辨率失败: {file_path}, 使用默认 720x1280")
        return 720, 1280

    async def _has_audio_track(self, file_path: str) -> bool:
        """探测视频是否含有音频流（使用 ffmpeg -i 输出解析，无需 ffprobe）"""
        cmd = [
            self.ffmpeg_bin,
            "-nostdin",
            "-i", file_path,
        ]
        result = await asyncio.to_thread(
            subprocess.run, cmd, capture_output=True, text=True,
            timeout=30
        )
        # ffmpeg -i 不带输出时返回非 0，音频流会出现在 stderr 的 "Stream #x:x ... Audio: ..." 行
        return "Audio:" in (result.stderr or "")

    async def _get_video_duration(self, file_path: str) -> float:
        """获取视频时长（秒）- 仅读取文件头，不解码"""
        import re
        # 方法1: 使用 ffmpeg -i 仅读取头部信息（不解码）
        cmd = [
            self.ffmpeg_bin,
            "-nostdin",      # 不读取 stdin，避免 nohup 环境卡住
            "-i", file_path,
        ]
        result = await asyncio.to_thread(
            subprocess.run, cmd, capture_output=True, text=True,
            timeout=30  # 30秒超时保护
        )
        # 从 stderr 解析时长（ffmpeg -i 会输出 Duration 后以非零退出，这是正常的）
        duration_match = re.search(r'Duration: (\d{2}):(\d{2}):(\d{2}\.\d{2})', result.stderr)
        if duration_match:
            hours = int(duration_match.group(1))
            minutes = int(duration_match.group(2))
            seconds = float(duration_match.group(3))
            total = hours * 3600 + minutes * 60 + seconds
            logger.info(f"视频时长: {file_path} -> {total:.2f}s")
            return total
        
        logger.warning(f"获取视频时长失败: {file_path}, stderr: {result.stderr[:200]}, 使用默认 5 秒")
        return 5.0
    
    async def extract_sample_frames(
        self,
        video_path: str,
        num_frames: int = 4,
        output_dir: Optional[str] = None,
    ) -> list[str]:
        """按常用比例从视频抽取关键采样帧（用于模型审查）。

        默认按 4 帧：首帧 / 25% / 75% / 尾帧。如 num_frames 不等于 4，则按均匀分布采样
        （始终包含首帧和尾帧）。

        Args:
            video_path: 本地视频文件路径
            num_frames: 抽取帧数（>=2）
            output_dir: 输出目录（为空时写到 self.temp_dir/sample_frames/<video_stem>/）

        Returns:
            排序后的本地 PNG 路径列表（抽取失败的位置会被跳过，返回长度可能小于 num_frames）
        """
        if num_frames < 2:
            num_frames = 2
        duration = await self._get_video_duration(video_path)
        if duration <= 0.1:
            logger.warning(f"extract_sample_frames: 视频时长异常 ({duration:.2f}s)，返回空列表: {video_path}")
            return []

        if output_dir is None:
            stem = Path(video_path).stem or "video"
            output_dir = str(self.temp_dir / "sample_frames" / stem)
        os.makedirs(output_dir, exist_ok=True)

        # 按比例构造时间点
        if num_frames == 4:
            ratios = [0.0, 0.25, 0.75, 1.0]
        else:
            ratios = [i / (num_frames - 1) for i in range(num_frames)]

        # 避免恰好落在最后一帧导致 ffmpeg 解码不到
        max_seek = max(0.0, duration - 0.05)
        timestamps = [min(r * duration, max_seek) for r in ratios]

        results: list[str] = []
        for idx, ts in enumerate(timestamps):
            out_path = os.path.join(output_dir, f"frame_{idx:02d}_{int(ts * 1000):06d}ms.png")
            # -ss 放在 -i 前指定快速 seek；-frames:v 1 单帧输出
            cmd = [
                self.ffmpeg_bin,
                "-nostdin",
                "-y",
                "-ss", f"{ts:.3f}",
                "-i", video_path,
                "-frames:v", "1",
                "-q:v", "2",
                out_path,
            ]
            try:
                await self._run_ffmpeg(cmd)
                if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                    results.append(out_path)
                else:
                    logger.warning(f"extract_sample_frames: 抽帧输出为空 ({idx}@{ts:.2f}s): {out_path}")
            except Exception as e:
                logger.warning(f"extract_sample_frames: 抽帧失败 ({idx}@{ts:.2f}s): {e}")
                continue

        logger.info(f"extract_sample_frames: 共抽取 {len(results)}/{num_frames} 帧 ({video_path})")
        return results

    async def concatenate_videos_with_rhythm(
        self,
        video_paths: list[str],
        rhythm_plan: list[dict],
        output_path: Optional[str] = None,
        output_format: str = "mp4",
    ) -> str:
        """
        按照节奏计划拼接视频：支持逐镜头目标时长裁剪 + 可变转场时长/类型。

        Args:
            video_paths: 视频文件路径列表（按镜头顺序）
            rhythm_plan: 每个镜头的节奏控制参数列表，与 video_paths 一一对应
                [
                    {
                        "target_duration": float | None,   # 目标时长（秒），None 则不裁剪
                        "transition_duration": float,       # 进入该镜头的转场时长
                        "transition_type": str,             # xfade 转场名 (fade/wipeleft/...)
                    },
                    ...
                ]
            output_path: 输出路径（可选）
            output_format: 输出格式

        Returns:
            输出文件的本地路径
        """
        import uuid

        if not video_paths:
            raise ValueError("视频路径列表不能为空")
        if len(rhythm_plan) != len(video_paths):
            raise ValueError("rhythm_plan 长度必须与 video_paths 一致")

        if output_path is None:
            output_path = str(self.temp_dir / f"output_{uuid.uuid4().hex}.{output_format}")

        # ---- 1. 按目标时长裁剪每个镜头 ----
        trimmed_paths = []
        for i, (vp, plan) in enumerate(zip(video_paths, rhythm_plan)):
            target = plan.get("target_duration")
            if target and target > 0:
                actual_dur = await self._get_video_duration(vp)
                if actual_dur > target + 0.1:  # 只有实际比目标长 0.1s 以上才裁剪
                    trimmed = str(self.temp_dir / f"rhythm_trim_{i:03d}.{output_format}")
                    await self._trim_to_duration(vp, target, trimmed)
                    trimmed_paths.append(trimmed)
                    logger.info(f"镜头 {i+1}: 裁剪 {actual_dur:.2f}s -> {target:.2f}s")
                    continue
            trimmed_paths.append(vp)

        # ---- 2. 拼接（带可变转场） ----
        if len(trimmed_paths) == 1:
            import shutil
            shutil.copy2(trimmed_paths[0], output_path)
            return output_path

        # 获取每段实际时长
        durations = []
        for f in trimmed_paths:
            durations.append(await self._get_video_duration(f))

        # 构建 xfade filter chain（支持逐段不同的 transition_duration 和 type）
        inputs = []
        for f in trimmed_paths:
            inputs.extend(["-i", f])

        # 检测目标分辨率并添加 scale+pad 统一分辨率
        target_w, target_h = await self._get_video_resolution(trimmed_paths[0])
        logger.info(f"rhythm xfade 合成目标分辨率: {target_w}x{target_h}")
        
        scale_parts = []
        for i in range(len(trimmed_paths)):
            scale_parts.append(
                f"[{i}:v]scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
                f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:color=black,"
                f"setsar=1[scaled{i}]"
            )
        
        filter_parts = list(scale_parts)
        cumulative = 0.0  # 到当前合并流的总时长（扣除已使用的 overlap）

        for i in range(len(trimmed_paths) - 1):
            # 第 i+1 个镜头的 plan 决定「进入这一刀」的转场
            cut_plan = rhythm_plan[i + 1]
            t_dur = max(cut_plan.get("transition_duration", 0.5), 0.0)
            t_type = cut_plan.get("transition_type", "fade")

            src1 = "[scaled0]" if i == 0 else f"[v{i}]"
            src2 = f"[scaled{i+1}]"

            if i == 0:
                offset = durations[0] - t_dur
            else:
                offset = cumulative + durations[i] - t_dur

            offset = max(offset, 0.0)

            out_label = f"[v{i+1}]" if i < len(trimmed_paths) - 2 else "[outv]"
            filter_parts.append(
                f"{src1}{src2}xfade=transition={t_type}:duration={t_dur:.3f}:offset={offset:.3f}{out_label}"
            )

            # 更新 cumulative：当前合并段总时长 = offset + t_dur
            cumulative = offset

        # ---- 音频链：若所有镜头都有音轨，则做 acrossfade 同步淡化 ----
        has_audio_list = [await self._has_audio_track(f) for f in trimmed_paths]
        all_have_audio = all(has_audio_list)
        if all_have_audio:
            for i in range(len(trimmed_paths)):
                filter_parts.append(
                    f"[{i}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
                    f"asetpts=PTS-STARTPTS[a{i}]"
                )
            for i in range(len(trimmed_paths) - 1):
                cut_plan_a = rhythm_plan[i + 1]
                t_dur_a = max(cut_plan_a.get("transition_duration", 0.5), 0.05)
                src1_a = "[a0]" if i == 0 else f"[aout{i}]"
                src2_a = f"[a{i+1}]"
                out_a = "[outa]" if i == len(trimmed_paths) - 2 else f"[aout{i+1}]"
                filter_parts.append(
                    f"{src1_a}{src2_a}acrossfade=d={t_dur_a:.3f}{out_a}"
                )

        filter_complex = ";".join(filter_parts)

        cmd = [
            self.ffmpeg_bin,
            "-y",
            *inputs,
            "-filter_complex", filter_complex,
            "-map", "[outv]",
        ]
        if all_have_audio:
            cmd += ["-map", "[outa]"]
        cmd += [
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            output_path,
        ]

        await self._run_ffmpeg(cmd)
        if all_have_audio:
            logger.info(f"rhythm xfade 合成含音轨: {len(trimmed_paths)} 段 acrossfade 链")
        else:
            logger.warning(f"rhythm xfade 合成跳过音轨（部分镜头无音频）: has_audio={has_audio_list}")

        # 清理中间裁剪文件
        for i, tp in enumerate(trimmed_paths):
            if tp != video_paths[i] and tp != output_path:
                try:
                    os.unlink(tp)
                except OSError:
                    pass

        logger.info(f"节奏感知视频合成完成: {output_path}")
        return output_path

    async def _trim_to_duration(
        self, input_path: str, duration: float, output_path: str
    ) -> None:
        """将视频裁剪到指定时长（从头开始）"""
        cmd = [
            self.ffmpeg_bin,
            "-y",
            "-i", input_path,
            "-t", f"{duration:.3f}",
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            output_path,
        ]
        await self._run_ffmpeg(cmd)

    async def apply_edit_plan(
        self,
        input_path: str,
        edit_plan: dict,
        output_path: Optional[str] = None,
        output_format: str = "mp4",
    ) -> str:
        """按 edit_plan 对单个镜头 clip 执行裁剪 + 变速。

        由 clip_editor_agent 产出的 edit_plan 由 Stage 5 在下载镜头后调用，
        产物作为 downloaded_files[i] 的替换，后续再进入
        concatenate_videos_with_rhythm 拼接。

        Args:
            input_path: 原始生成 clip 路径
            edit_plan: EditPlan.model_dump() 生成的 dict
            output_path: 输出路径（可选）
            output_format: 输出格式

        Returns:
            剪辑后的视频路径。当 strategy=no_op 时直接返回 input_path。
        """
        import uuid

        strategy = (edit_plan or {}).get("strategy", "no_op")
        if strategy == "no_op":
            return input_path

        if output_path is None:
            output_path = str(
                self.temp_dir / f"clip_edit_{uuid.uuid4().hex[:8]}.{output_format}"
            )

        trim = (edit_plan or {}).get("trim") or {}
        start_sec = float(trim.get("start_sec", 0.0) or 0.0)
        end_sec = trim.get("end_sec")
        speed = float((edit_plan or {}).get("speed", 1.0) or 1.0)

        # 构造 ffmpeg 命令。裁剪先用 -ss / -to 在 input 之前，保证 seek 精确到关键帧。
        cmd = [self.ffmpeg_bin, "-y"]
        if start_sec > 0:
            cmd += ["-ss", f"{start_sec:.3f}"]
        if end_sec is not None and float(end_sec) > start_sec:
            cmd += ["-to", f"{float(end_sec):.3f}"]
        cmd += ["-i", input_path]

        # 变速：video 用 setpts，audio 用 atempo
        if abs(speed - 1.0) > 1e-3:
            # atempo 单次范围 [0.5, 2.0]，此处 speed 上限 2.0 已满足
            video_pts = 1.0 / speed  # setpts 与实际倍速成反比
            cmd += [
                "-filter_complex",
                f"[0:v]setpts={video_pts:.4f}*PTS[v];[0:a]atempo={speed:.4f}[a]",
                "-map", "[v]",
                "-map", "[a]",
            ]

        cmd += [
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            output_path,
        ]

        try:
            await self._run_ffmpeg(cmd)
        except Exception as e:
            logger.warning(
                f"apply_edit_plan 执行失败，降级为从头裁剪至 target_duration: {e}"
            )
            # 降级：用 target_duration 从头裁剪
            target = float((edit_plan or {}).get("target_duration") or 0.0)
            if target > 0:
                await self._trim_to_duration(input_path, target, output_path)
            else:
                return input_path

        logger.info(
            f"apply_edit_plan: strategy={strategy} trim=[{start_sec:.2f}-{end_sec}] "
            f"speed={speed:.2f} -> {output_path}"
        )
        return output_path

    async def _run_ffmpeg(self, cmd: list[str]) -> None:
        """执行 FFmpeg 命令"""
        # 确保 -nostdin 在参数中，避免 nohup 环境卡住
        if "-nostdin" not in cmd:
            cmd.insert(1, "-nostdin")
        logger.info(f"执行 FFmpeg: {' '.join(cmd[:6])}...")
        result = await asyncio.to_thread(
            subprocess.run, cmd, capture_output=True, text=True,
            timeout=600  # 10分钟超时保护
        )
        if result.returncode != 0:
            logger.error(f"FFmpeg 错误: {result.stderr[-500:]}")
            raise RuntimeError(f"FFmpeg 执行失败: {result.stderr[:500]}")
    
    # ---- xfade 转场类型映射 ----
    XFADE_TYPE_MAP = {
        "hard_cut":    "fade",       # FFmpeg 无 hard_cut，用极短 fade 模拟
        "dissolve":    "fade",
        "wipe":        "wipeleft",
        "fade":        "fade",
        "smash_cut":   "fade",       # smash_cut 语义上是硬切，同理
    }

    @staticmethod
    def map_cut_type_to_xfade(cut_type: str) -> str:
        """将节奏分析的 cut_type 映射为 FFmpeg xfade transition 名称"""
        return FFmpegService.XFADE_TYPE_MAP.get(cut_type, "fade")

    async def add_audio(
        self, video_path: str, audio_path: str, output_path: Optional[str] = None
    ) -> str:
        """为视频添加音频轨道"""
        if output_path is None:
            output_path = video_path.replace(".mp4", "_with_audio.mp4")
        
        cmd = [
            self.ffmpeg_bin,
            "-y",
            "-i", video_path,
            "-i", audio_path,
            "-c:v", "copy",
            "-c:a", "aac",
            "-shortest",
            output_path,
        ]
        await self._run_ffmpeg(cmd)
        return output_path

    async def mix_bgm(
        self,
        video_path: str,
        bgm_path: str,
        volume: float = 0.3,
        fade_out_sec: float = 2.0,
        output_path: Optional[str] = None,
    ) -> str:
        """
        将 BGM 混入视频，支持音量控制和尾部淡出。

        Args:
            video_path: 输入视频路径
            bgm_path: BGM 音频文件路径
            volume: BGM 音量 (0.0-1.0)，默认 0.3
            fade_out_sec: 尾部淡出时长（秒），默认 2.0
            output_path: 输出文件路径（可选）

        Returns:
            输出文件的本地路径
        """
        if output_path is None:
            output_path = video_path.replace(".mp4", "_with_bgm.mp4")

        # 获取视频时长用于计算淡出起始点
        video_duration = await self._get_video_duration(video_path)
        fade_start = max(0, video_duration - fade_out_sec)

        # 构建音频滤镜：音量控制 + 尾部淡出
        audio_filter = (
            f"[1:a]volume={volume:.2f},"
            f"afade=t=out:st={fade_start:.2f}:d={fade_out_sec:.2f}[bgm]"
        )

        cmd = [
            self.ffmpeg_bin,
            "-y",
            "-i", video_path,
            "-i", bgm_path,
            "-filter_complex", audio_filter,
            "-map", "0:v",
            "-map", "[bgm]",
            "-shortest",
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            output_path,
        ]

        await self._run_ffmpeg(cmd)
        logger.info(f"BGM 混入完成: {output_path} (volume={volume}, fade_out={fade_out_sec}s)")
        return output_path

    async def mux_video_with_ambient(
        self,
        video_path: str,
        audio_path: str,
        volume: float = 0.3,
        output_path: Optional[str] = None,
    ) -> str:
        """
        将镜头环境音 (mp3) 混入单个镜头视频（主要面向 Kling 双锚定无声视频）。

        策略：
        - 视频流 copy（不重编码）
        - 音频：volume 控制 + apad 静音填充以对齐视频时长 + -shortest 防超长
        - 成品另存新路径，不原地覆盖

        Args:
            video_path: 输入视频路径（无声）
            audio_path: 环境音 mp3 本地路径
            volume: 环境音音量 0.0-1.0，默认 0.3
            output_path: 输出路径（可选）

        Returns:
            混音后的视频本地路径
        """
        if output_path is None:
            base, _ = os.path.splitext(video_path)
            output_path = f"{base}_with_ambient.mp4"

        video_duration = await self._get_video_duration(video_path)
        # apad whole_dur 单位是秒，加 0.05s 容差
        whole_dur = max(0.5, video_duration + 0.05)

        audio_filter = (
            f"[1:a]volume={volume:.2f},"
            f"apad=whole_dur={whole_dur:.2f}[a1]"
        )

        cmd = [
            self.ffmpeg_bin,
            "-y",
            "-i", video_path,
            "-i", audio_path,
            "-filter_complex", audio_filter,
            "-map", "0:v",
            "-map", "[a1]",
            "-shortest",
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            output_path,
        ]

        await self._run_ffmpeg(cmd)
        logger.info(
            f"镜头环境音混入完成: {output_path} "
            f"(video_dur={video_duration:.2f}s, volume={volume})"
        )
        return output_path


def detect_transition_frame(video_path: str, max_check_seconds: float = 3.0, threshold: float = None) -> float:
    """
    使用帧差法检测视频中三视图静态画面到正常运动画面的过渡点。
    
    原理：三视图作为首帧时，视频开头几帧是静态的（帧差很小），
    当画面开始运动时，帧差会突然增大，这个突变点就是裁剪起始位置。
    
    Args:
        video_path: 视频文件路径
        max_check_seconds: 最多检查前多少秒（默认 3 秒）
        threshold: 帧差阈值，None 则自动计算（使用均值+2倍标准差）
    
    Returns:
        裁剪起始时间（秒），如果检测不到过渡点返回 0.0
    """
    # OpenCV 不可用时返回 fallback 值 1.0
    if not CV2_AVAILABLE:
        logger.error(f"OpenCV 不可用，使用 fallback 时间 1.0s: {video_path}")
        return 1.0
    
    cap = None
    try:
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            logger.error(f"无法打开视频文件: {video_path}")
            return 0.0
        
        # 获取视频基本信息
        fps = cap.get(cv2.CAP_PROP_FPS)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        total_duration = total_frames / fps if fps > 0 else 0
        
        if fps <= 0 or total_frames <= 0:
            logger.warning(f"无法获取视频帧率或帧数: {video_path}")
            return 0.0
        
        # 计算需要检查的帧数
        max_frames_to_check = int(max_check_seconds * fps)
        frames_to_check = min(max_frames_to_check, total_frames - 1)
        
        if frames_to_check < 2:
            logger.warning(f"视频帧数太少，无法检测过渡点: {video_path}")
            return 0.0
        
        logger.info(f"开始检测过渡点 - 视频: {video_path}, FPS: {fps:.2f}, "
                   f"总帧数: {total_frames}, 检查帧数: {frames_to_check}")
        
        # 读取第一帧
        ret, prev_frame = cap.read()
        if not ret or prev_frame is None:
            logger.warning(f"无法读取视频第一帧: {video_path}")
            return 0.0
        
        # 转换为灰度图
        prev_gray = cv2.cvtColor(prev_frame, cv2.COLOR_BGR2GRAY)
        
        # 收集帧差值
        frame_diffs = []
        frame_numbers = []
        
        for frame_num in range(1, frames_to_check + 1):
            ret, curr_frame = cap.read()
            if not ret or curr_frame is None:
                break
            
            # 转换为灰度图
            curr_gray = cv2.cvtColor(curr_frame, cv2.COLOR_BGR2GRAY)
            
            # 计算帧差（绝对差值的均值）
            diff = cv2.absdiff(prev_gray, curr_gray)
            diff_mean = np.mean(diff)
            
            frame_diffs.append(diff_mean)
            frame_numbers.append(frame_num)
            
            # 更新前一帧
            prev_gray = curr_gray
        
        if len(frame_diffs) < 3:
            logger.warning(f"收集到的帧差数据太少，无法检测: {video_path}")
            return 0.0
        
        # 计算自动阈值
        if threshold is None:
            mean_diff = np.mean(frame_diffs)
            std_diff = np.std(frame_diffs)
            threshold = mean_diff + 2 * std_diff
            # 设置最小阈值为 5.0，避免误判
            threshold = max(threshold, 5.0)
            logger.info(f"自动计算阈值 - 均值: {mean_diff:.4f}, 标准差: {std_diff:.4f}, "
                       f"阈值: {threshold:.4f}")
        else:
            # 确保手动阈值也满足最小值要求
            threshold = max(threshold, 5.0)
            logger.info(f"使用手动阈值: {threshold:.4f}")
        
        # 查找过渡点（第一个超过阈值的帧）
        transition_frame = None
        for i, diff in enumerate(frame_diffs):
            logger.debug(f"帧 {frame_numbers[i]}: 帧差 = {diff:.4f}")
            if diff > threshold:
                transition_frame = frame_numbers[i]
                logger.info(f"检测到过渡点 - 帧号: {transition_frame}, "
                           f"帧差: {diff:.4f}, 阈值: {threshold:.4f}")
                break
        
        if transition_frame is None:
            logger.info(f"未检测到过渡点（所有帧差都低于阈值）: {video_path}")
            return 0.0
        
        # 计算过渡时间（毫秒级精度）
        transition_time = transition_frame / fps
        
        # 安全保护：如果过渡点超过视频总时长的 80%，说明检测失败
        if total_duration > 0 and transition_time > total_duration * 0.8:
            logger.warning(f"过渡点 {transition_time:.3f}s 超过视频总时长 80% ({total_duration * 0.8:.3f}s)，"
                          f"判定为检测失败: {video_path}")
            return 0.0
        
        logger.info(f"过渡点检测成功 - 裁剪起始时间: {transition_time:.3f}s ({transition_time*1000:.1f}ms)")
        return transition_time
        
    except Exception as e:
        logger.error(f"检测过渡点时发生错误: {video_path}, 错误: {e}")
        return 0.0
    finally:
        if cap is not None:
            cap.release()


async def crop_video_from_time(
    ffmpeg_bin: str,
    input_path: str,
    start_time: float,
    output_path: Optional[str] = None
) -> str:
    """
    使用 FFmpeg 从指定时间开始裁剪视频
    
    Args:
        ffmpeg_bin: FFmpeg 可执行文件路径
        input_path: 输入视频路径
        start_time: 裁剪起始时间（秒）
        output_path: 输出路径（可选，默认自动生成）
    
    Returns:
        裁剪后的视频路径
    """
    if output_path is None:
        output_path = input_path.replace(".mp4", f"_cropped_{start_time:.3f}.mp4")
    
    # 使用 -ss 参数从指定时间开始裁剪，重新编码确保 QuickTime 兼容性
    cmd = [
        ffmpeg_bin,
        "-y",
        "-nostdin",  # 不读 stdin，避免 nohup/后台运行时挂起
        "-ss", f"{start_time:.3f}",
        "-i", input_path,
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "23",
        "-pix_fmt", "yuv420p",  # QuickTime 兼容的像素格式
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",  # 优化网络播放
        output_path,
    ]
    
    logger.info(f"裁剪视频 - 从 {start_time:.3f}s 开始: {input_path} -> {output_path}")
    
    result = await asyncio.to_thread(
        subprocess.run, cmd,
        capture_output=True, text=True,
        stdin=subprocess.DEVNULL, timeout=300,
    )
    
    if result.returncode != 0:
        logger.error(f"裁剪视频失败: {result.stderr}")
        raise RuntimeError(f"裁剪视频失败: {result.stderr[:500]}")
    
    logger.info(f"视频裁剪完成: {output_path}")
    return output_path
