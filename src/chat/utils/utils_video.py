#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""纯 inkfox 视频关键帧分析工具

仅依赖 `inkfox.video` 提供的 Rust 扩展能力：
    - extract_keyframes_from_video
    - get_system_info

功能：
    - 关键帧提取 (base64, timestamp)
    - 批量 / 逐帧 LLM 描述
    - 自动模式 (<=3 帧批量，否则逐帧)
"""

from __future__ import annotations

import os
import io
import asyncio
import base64
import tempfile
from pathlib import Path
from typing import List, Tuple, Optional, Dict, Any
import hashlib
import time

from PIL import Image

from src.common.logger import get_logger
from src.common.database.sqlalchemy_models import get_db_session, Videos
from sqlalchemy import select

logger = get_logger("utils_video")

# Rust模块可用性检测
RUST_VIDEO_AVAILABLE = False
try:
    import rust_video # pyright: ignore[reportMissingImports]

    RUST_VIDEO_AVAILABLE = True
    logger.info("✅ Rust 视频处理模块加载成功")
except ImportError as e:
    logger.warning(f"⚠️ Rust 视频处理模块加载失败: {e}")
    logger.warning("⚠️ 视频识别功能将自动禁用")
except Exception as e:
    logger.error(f"❌ 加载Rust模块时发生错误: {e}")
    RUST_VIDEO_AVAILABLE = False

# 全局正在处理的视频哈希集合，用于防止重复处理
processing_videos = set()
processing_lock = asyncio.Lock()
# 为每个视频hash创建独立的锁和事件
video_locks = {}
video_events = {}
video_lock_manager = asyncio.Lock()


class VideoAnalyzer:
    """基于 inkfox 的视频关键帧 + LLM 描述分析器"""

    def __init__(self) -> None:
        cfg = getattr(global_config, "video_analysis", object())
        self.max_frames: int = getattr(cfg, "max_frames", 20)
        self.frame_quality: int = getattr(cfg, "frame_quality", 85)
        self.max_image_size: int = getattr(cfg, "max_image_size", 600)
        self.enable_frame_timing: bool = getattr(cfg, "enable_frame_timing", True)
        self.use_simd: bool = getattr(cfg, "rust_use_simd", True)
        self.threads: int = getattr(cfg, "rust_threads", 0)
        self.ffmpeg_path: str = getattr(cfg, "ffmpeg_path", "ffmpeg")
        self.analysis_mode: str = getattr(cfg, "analysis_mode", "auto")
        self.frame_analysis_delay: float = 0.3

        # 人格与提示模板
        try:
            import cv2

            opencv_available = True
        except ImportError:
            pass

        if not RUST_VIDEO_AVAILABLE and not opencv_available:
            logger.error("❌ 没有可用的视频处理实现，视频分析器将被禁用")
            self.disabled = True
            return
        elif not RUST_VIDEO_AVAILABLE:
            logger.warning("⚠️ Rust视频处理模块不可用，将使用Python降级实现")
        elif not opencv_available:
            logger.warning("⚠️ OpenCV不可用，仅支持Rust关键帧模式")

        self.disabled = False

        # 使用专用的视频分析配置
        try:
            self.video_llm = LLMRequest(
                model_set=model_config.model_task_config.video_analysis, request_type="video_analysis"
            )
            logger.debug("✅ 使用video_analysis模型配置")
        except (AttributeError, KeyError) as e:
            # 如果video_analysis不存在，使用vlm配置
            self.video_llm = LLMRequest(model_set=model_config.model_task_config.vlm, request_type="vlm")
            logger.warning(f"video_analysis配置不可用({e})，回退使用vlm配置")

        # 从配置文件读取参数，如果配置不存在则使用默认值
        config = global_config.video_analysis

        # 使用 getattr 统一获取配置参数，如果配置不存在则使用默认值
        self.max_frames = getattr(config, "max_frames", 6)
        self.frame_quality = getattr(config, "frame_quality", 85)
        self.max_image_size = getattr(config, "max_image_size", 600)
        self.enable_frame_timing = getattr(config, "enable_frame_timing", True)

        # Rust模块相关配置
        self.rust_keyframe_threshold = getattr(config, "rust_keyframe_threshold", 2.0)
        self.rust_use_simd = getattr(config, "rust_use_simd", True)
        self.rust_block_size = getattr(config, "rust_block_size", 8192)
        self.rust_threads = getattr(config, "rust_threads", 0)
        self.ffmpeg_path = getattr(config, "ffmpeg_path", "ffmpeg")

        # 从personality配置中获取人格信息
        try:
            personality_config = global_config.personality
            self.personality_core = getattr(personality_config, "personality_core", "是一个积极向上的女大学生")
            self.personality_side = getattr(
                personality_config, "personality_side", "用一句话或几句话描述人格的侧面特点"
            )
        except AttributeError:
            # 如果没有personality配置，使用默认值
            self.personality_core = "是一个积极向上的女大学生"
            self.personality_side = "用一句话或几句话描述人格的侧面特点"

        self.batch_analysis_prompt = getattr(
            cfg,
            "batch_analysis_prompt",
            """请以第一人称视角阅读这些按时间顺序提取的关键帧。\n核心：{personality_core}\n人格：{personality_side}\n请详细描述视频(主题/人物与场景/动作与时间线/视觉风格/情绪氛围/特殊元素)。""",
        )

        # 新增的线程池配置
        self.use_multiprocessing = getattr(config, "use_multiprocessing", True)
        self.max_workers = getattr(config, "max_workers", 2)
        self.frame_extraction_mode = getattr(config, "frame_extraction_mode", "fixed_number")
        self.frame_interval_seconds = getattr(config, "frame_interval_seconds", 2.0)

        # 将配置文件中的模式映射到内部使用的模式名称
        config_mode = getattr(config, "analysis_mode", "auto")
        if config_mode == "batch_frames":
            self.analysis_mode = "batch"
        elif config_mode == "frame_by_frame":
            self.analysis_mode = "sequential"
        elif config_mode == "auto":
            self.analysis_mode = "auto"
        else:
            logger.warning(f"无效的分析模式: {config_mode}，使用默认的auto模式")
            self.analysis_mode = "auto"

        self.frame_analysis_delay = 0.3  # API调用间隔（秒）
        self.frame_interval = 1.0  # 抽帧时间间隔（秒）
        self.batch_size = 3  # 批处理时每批处理的帧数
        self.timeout = 60.0  # 分析超时时间（秒）

        if config:
            logger.debug("✅ 从配置文件读取视频分析参数")
        else:
            logger.warning("配置文件中缺少video_analysis配置，使用默认值")

        # 系统提示词
        self.system_prompt = "你是一个专业的视频内容分析助手。请仔细观察用户提供的视频关键帧，详细描述视频内容。"

        logger.debug(f"✅ 视频分析器初始化完成，分析模式: {self.analysis_mode}, 线程池: {self.use_multiprocessing}")

        # 获取Rust模块系统信息
        self._log_system_info()

    def _log_system_info(self):
        """记录系统信息"""
        if not RUST_VIDEO_AVAILABLE:
            logger.info("⚠️ Rust模块不可用，跳过系统信息获取")
            return

        try:
            system_info = rust_video.get_system_info()
            logger.debug(f"🔧 系统信息: 线程数={system_info.get('threads', '未知')}")

            # 记录CPU特性
            features = []
            if system_info.get("avx2_supported"):
                features.append("AVX2")
            if system_info.get("sse2_supported"):
                features.append("SSE2")
            if system_info.get("simd_supported"):
                features.append("SIMD")

            if features:
                logger.debug(f"🚀 CPU特性: {', '.join(features)}")
            else:
                logger.debug("⚠️ 未检测到SIMD支持")

            logger.debug(f"📦 Rust模块版本: {system_info.get('version', '未知')}")

        except Exception as e:
            logger.warning(f"获取系统信息失败: {e}")

    def _calculate_video_hash(self, video_data: bytes) -> str:
        """计算视频文件的hash值"""
        hash_obj = hashlib.sha256()
        hash_obj.update(video_data)
        return hash_obj.hexdigest()

    async def _check_video_exists(self, video_hash: str) -> Optional[Videos]:
        """检查视频是否已经分析过"""
        try:
            async with get_db_session() as session:
                if not session:
                    logger.warning("无法获取数据库会话，跳过视频存在性检查。")
                    return None
                # 明确刷新会话以确保看到其他事务的最新提交
                await session.expire_all()
                stmt = select(Videos).where(Videos.video_hash == video_hash)
                result = await session.execute(stmt)
                return result.scalar_one_or_none()
        except Exception as e:
            logger.warning(f"检查视频是否存在时出错: {e}")
            return None

    async def _store_video_result(
        self, video_hash: str,  description: str, metadata: Optional[Dict] = None
    ) -> Optional[Videos]:
        """存储视频分析结果到数据库"""
        # 检查描述是否为错误信息，如果是则不保存
        if description.startswith("❌"):
            logger.warning(f"⚠️ 检测到错误信息，不保存到数据库: {description[:50]}...")
            return None

        try:
            async with get_db_session() as session:
                if not session:
                    logger.warning("无法获取数据库会话，跳过视频结果存储。")
                    return None
                # 只根据video_hash查找
                stmt = select(Videos).where(Videos.video_hash == video_hash)
                result = await session.execute(stmt)
                existing_video = result.scalar_one_or_none()

                if existing_video:
                    # 如果已存在，更新描述和计数
                    existing_video.description = description
                    existing_video.count += 1
                    existing_video.timestamp = time.time()
                    if metadata:
                        existing_video.duration = metadata.get("duration")
                        existing_video.frame_count = metadata.get("frame_count")
                        existing_video.fps = metadata.get("fps")
                        existing_video.resolution = metadata.get("resolution")
                        existing_video.file_size = metadata.get("file_size")
                    await session.commit()
                    await session.refresh(existing_video)
                    logger.info(f"✅ 更新已存在的视频记录，hash: {video_hash[:16]}..., count: {existing_video.count}")
                    return existing_video
                else:
                    video_record = Videos(
                        video_hash=video_hash, description=description, timestamp=time.time(), count=1
                    )
                    if metadata:
                        video_record.duration = metadata.get("duration")
                        video_record.frame_count = metadata.get("frame_count")
                        video_record.fps = metadata.get("fps")
                        video_record.resolution = metadata.get("resolution")
                        video_record.file_size = metadata.get("file_size")

                    session.add(video_record)
                    await session.commit()
                    await session.refresh(video_record)
                    logger.info(f"✅ 新视频分析结果已保存到数据库，hash: {video_hash[:16]}...")
                    return video_record
        except Exception as e:
            logger.error(f"❌ 存储视频分析结果时出错: {e}")
            return None

    def set_analysis_mode(self, mode: str):
        """设置分析模式"""
        if mode in ["batch", "sequential", "auto"]:
            self.analysis_mode = mode
            # logger.info(f"分析模式已设置为: {mode}")
        else:
            logger.warning(f"无效的分析模式: {mode}")

    async def extract_frames(self, video_path: str) -> List[Tuple[str, float]]:
        """提取视频帧 - 智能选择最佳实现"""
        # 检查是否应该使用Rust实现
        if RUST_VIDEO_AVAILABLE and self.frame_extraction_mode == "keyframe":
            # 优先尝试Rust关键帧提取
            try:
                return await self._extract_frames_rust_advanced(video_path)
            except Exception as e:
                logger.warning(f"Rust高级接口失败: {e}，尝试基础接口")
                try:
                    return await self._extract_frames_rust(video_path)
                except Exception as e2:
                    logger.warning(f"Rust基础接口也失败: {e2}，降级到Python实现")
                    return await self._extract_frames_python_fallback(video_path)
        else:
            # 使用Python实现（支持time_interval和fixed_number模式）
            if not RUST_VIDEO_AVAILABLE:
                logger.info("🔄 Rust模块不可用，使用Python抽帧实现")
            else:
                logger.info(f"🔄 抽帧模式为 {self.frame_extraction_mode}，使用Python抽帧实现")
            return await self._extract_frames_python_fallback(video_path)

    # ---- 系统信息 ----
    def _log_system(self) -> None:
        try:
            info = video.get_system_info()  # type: ignore[attr-defined]
            logger.info(
                f"inkfox: threads={info.get('threads')} version={info.get('version')} simd={info.get('simd_supported')}"
            )
        except Exception as e:  # pragma: no cover
            logger.debug(f"获取系统信息失败: {e}")

    # ---- 关键帧提取 ----
    async def extract_keyframes(self, video_path: str) -> List[Tuple[str, float]]:
        """提取关键帧并返回 (base64, timestamp_seconds) 列表"""
        with tempfile.TemporaryDirectory() as tmp:
            result = video.extract_keyframes_from_video(  # type: ignore[attr-defined]
                video_path=video_path,
                output_dir=tmp,
                max_keyframes=self.max_frames * 2,  # 先多抓一点再截断
                max_save=self.max_frames,
                ffmpeg_path=self.ffmpeg_path,
                use_simd=self.use_simd,
                threads=self.threads,
                verbose=False,
            )
            files = sorted(Path(tmp).glob("keyframe_*.jpg"))[: self.max_frames]
            total_ms = getattr(result, "total_time_ms", 0)
            frames: List[Tuple[str, float]] = []
            for i, f in enumerate(files):
                img = Image.open(f).convert("RGB")
                if max(img.size) > self.max_image_size:
                    scale = self.max_image_size / max(img.size)
                    img = img.resize((int(img.width * scale), int(img.height * scale)), Image.Resampling.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=self.frame_quality)
                b64 = base64.b64encode(buf.getvalue()).decode()
                ts = (i / max(1, len(files) - 1)) * (total_ms / 1000.0) if total_ms else float(i)
                frames.append((b64, ts))
            return frames

    # ---- 批量分析 ----
    async def _analyze_batch(self, frames: List[Tuple[str, float]], question: Optional[str]) -> str:
        from src.llm_models.payload_content.message import MessageBuilder, RoleType
        from src.llm_models.utils_model import RequestType
        prompt = self.batch_analysis_prompt.format(
            personality_core=self.personality_core, personality_side=self.personality_side
        )

        if user_question:
            prompt += f"\n\n用户问题: {user_question}"

        # 添加帧信息到提示词
        frame_info = []
        for i, (_frame_base64, timestamp) in enumerate(frames):
            if self.enable_frame_timing:
                frame_info.append(f"第{i + 1}帧 (时间: {timestamp:.2f}s)")
            else:
                frame_info.append(f"第{i + 1}帧")

        prompt += f"\n\n视频包含{len(frames)}帧图像：{', '.join(frame_info)}"
        prompt += "\n\n请基于所有提供的帧图像进行综合分析，关注并描述视频的完整内容和故事发展。"

        try:
            # 使用多图片分析
            response = await self._analyze_multiple_frames(frames, prompt)
            logger.info("✅ 视频识别完成")
            return response

        except Exception as e:
            logger.error(f"❌ 视频识别失败: {e}")
            raise e

    async def _analyze_multiple_frames(self, frames: List[Tuple[str, float]], prompt: str) -> str:
        """使用多图片分析方法"""
        logger.info(f"开始构建包含{len(frames)}帧的分析请求")

        # 导入MessageBuilder用于构建多图片消息
        from src.llm_models.payload_content.message import MessageBuilder, RoleType
        from src.llm_models.utils_model import RequestType

        # 构建包含多张图片的消息
        message_builder = MessageBuilder().set_role(RoleType.User).add_text_content(prompt)

        # 添加所有帧图像
        for _i, (frame_base64, _timestamp) in enumerate(frames):
            message_builder.add_image_content("jpeg", frame_base64)
            # logger.info(f"已添加第{i+1}帧到分析请求 (时间: {timestamp:.2f}s, 图片大小: {len(frame_base64)} chars)")

        message = message_builder.build()
        # logger.info(f"✅ 多帧消息构建完成，包含{len(frames)}张图片")

        # 获取模型信息和客户端
        selection_result = self.video_llm._model_selector.select_best_available_model(set(), "response")
        if not selection_result:
            raise RuntimeError("无法为视频分析选择可用模型。")
        model_info, api_provider, client = selection_result
        # logger.info(f"使用模型: {model_info.name} 进行多帧分析")

        # 直接执行多图片请求
        api_response = await self.video_llm._executor.execute_request(
            api_provider=api_provider,
            client=client,
            request_type=RequestType.RESPONSE,
            model_info=model_info,
            message_list=[message],
            temperature=None,
            max_tokens=None,
        )
        return resp.content or "❌ 未获得响应"

    # ---- 逐帧分析 ----
    async def _analyze_sequential(self, frames: List[Tuple[str, float]], question: Optional[str]) -> str:
        results: List[str] = []
        for i, (b64, ts) in enumerate(frames):
            prompt = f"分析第{i+1}帧" + (f" (时间: {ts:.2f}s)" if self.enable_frame_timing else "")
            if question:
                prompt += f"\n关注: {question}"
            try:
                text, _ = await self.video_llm.generate_response_for_image(
                    prompt=prompt, image_base64=b64, image_format="jpeg"
                )
                results.append(f"第{i+1}帧: {text}")
            except Exception as e:  # pragma: no cover
                results.append(f"第{i+1}帧: 失败 {e}")
            if i < len(frames) - 1:
                await asyncio.sleep(self.frame_analysis_delay)
        summary_prompt = "基于以下逐帧结果给出完整总结:\n\n" + "\n".join(results)
        try:
            final, _ = await self.video_llm.generate_response_for_image(
                prompt=summary_prompt, image_base64=frames[-1][0], image_format="jpeg"
            )
            return final
        except Exception:  # pragma: no cover
            return "\n".join(results)

    # ---- 主入口 ----
    async def analyze_video(self, video_path: str, question: Optional[str] = None) -> Tuple[bool, str]:
        if not os.path.exists(video_path):
            return False, "❌ 文件不存在"
        frames = await self.extract_keyframes(video_path)
        if not frames:
            return False, "❌ 未提取到关键帧"
        mode = self.analysis_mode
        if mode == "auto":
            mode = "batch" if len(frames) <= 20 else "sequential"
        text = await (self._analyze_batch(frames, question) if mode == "batch" else self._analyze_sequential(frames, question))
        return True, text

    async def analyze_video_from_bytes(
        self,
        video_bytes: bytes,
        filename: Optional[str] = None,
        prompt: Optional[str] = None,
        question: Optional[str] = None,
    ) -> Dict[str, str]:
        """从字节数据分析视频

        Args:
            video_bytes: 视频字节数据
            filename: 文件名（可选，仅用于日志）
            user_question: 用户问题（旧参数名，保持兼容性）
            prompt: 提示词（新参数名，与系统调用保持一致）

        Returns:
            Dict[str, str]: 包含分析结果的字典，格式为 {"summary": "分析结果"}
        """
        if self.disabled:
            return {"summary": "❌ 视频分析功能已禁用：没有可用的视频处理实现"}

        video_hash = None
        video_event = None

        try:
            logger.info("开始从字节数据分析视频")

            # 兼容性处理：如果传入了prompt参数，使用prompt；否则使用user_question
            question = prompt if prompt is not None else user_question

            # 检查视频数据是否有效
            if not video_bytes:
                return {"summary": "❌ 视频数据为空"}

            # 计算视频hash值
            video_hash = self._calculate_video_hash(video_bytes)
            logger.info(f"视频hash: {video_hash}")

            # 改进的并发控制：使用每个视频独立的锁和事件
            async with video_lock_manager:
                if video_hash not in video_locks:
                    video_locks[video_hash] = asyncio.Lock()
                    video_events[video_hash] = asyncio.Event()

                video_lock = video_locks[video_hash]
                video_event = video_events[video_hash]

            # 尝试获取该视频的专用锁
            if video_lock.locked():
                logger.info(f"⏳ 相同视频正在处理中，等待处理完成... (hash: {video_hash[:16]}...)")
                try:
                    # 等待处理完成的事件信号，最多等待60秒
                    await asyncio.wait_for(video_event.wait(), timeout=60.0)
                    logger.info("✅ 等待结束，检查是否有处理结果")

                    # 检查是否有结果了
                    existing_video = await self._check_video_exists(video_hash)
                    if existing_video:
                        logger.info(f"✅ 找到了处理结果，直接返回 (id: {existing_video.id})")
                        return {"summary": existing_video.description}
                    else:
                        logger.warning("⚠️ 等待完成但未找到结果，可能处理失败")
                except asyncio.TimeoutError:
                    logger.warning("⚠️ 等待超时(60秒)，放弃等待")

            # 获取锁开始处理
            async with video_lock:
                logger.info(f"🔒 获得视频处理锁，开始处理 (hash: {video_hash[:16]}...)")

                # 再次检查数据库（可能在等待期间已经有结果了）
                existing_video = await self._check_video_exists(video_hash)
                if existing_video:
                    logger.info(f"✅ 获得锁后发现已有结果，直接返回 (id: {existing_video.id})")
                    video_event.set()  # 通知其他等待者
                    return {"summary": existing_video.description}

                # 未找到已存在记录，开始新的分析
                logger.info("未找到已存在的视频记录，开始新的分析")

                # 创建临时文件进行分析
                with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as temp_file:
                    temp_file.write(video_bytes)
                    temp_path = temp_file.name

            try:
                with tempfile.NamedTemporaryFile(delete=False) as fp:
                    fp.write(video_bytes)
                    temp_path = fp.name
                try:
                    ok, summary = await self.analyze_video(temp_path, q)
                    # 写入缓存（仅成功）
                    if ok:
                        await self._save_cache(video_hash, summary, len(video_bytes))
                    return {"summary": summary}
                finally:
                    if os.path.exists(temp_path):
                        os.unlink(temp_path)

                # 保存分析结果到数据库（仅保存成功的结果）
                if success and not result.startswith("❌"):
                    metadata = {"filename": filename, "file_size": len(video_bytes), "analysis_timestamp": time.time()}
                    await self._store_video_result(video_hash=video_hash, description=result, metadata=metadata)
                    logger.info("✅ 分析结果已保存到数据库")
                else:
                    logger.warning("⚠️ 分析失败，不保存到数据库以便后续重试")

                # 处理完成，通知等待者并清理资源
                video_event.set()
                async with video_lock_manager:
                    # 清理资源
                    video_locks.pop(video_hash, None)
                    video_events.pop(video_hash, None)

                return {"summary": result}

        except Exception as e:
            error_msg = f"❌ 从字节数据分析视频失败: {str(e)}"
            logger.error(error_msg)

    async def _save_cache(self, video_hash: str, summary: str, file_size: int) -> None:
        try:
            async with get_db_session() as session:  # type: ignore
                stmt = insert(Videos).values(  # type: ignore
                    video_id="",
                    video_hash=video_hash,
                    description=summary,
                    count=1,
                    timestamp=time.time(),
                    vlm_processed=True,
                    duration=None,
                    frame_count=None,
                    fps=None,
                    resolution=None,
                    file_size=file_size,
                )
                try:
                    await session.execute(stmt)
                    await session.commit()
                    logger.debug(f"视频缓存写入 success hash={video_hash}")
                except sa_exc.IntegrityError:  # 可能并发已写入
                    await session.rollback()
                    logger.debug(f"视频缓存已存在 hash={video_hash}")
        except Exception:  # pragma: no cover
                logger.debug("视频缓存写入失败")


# ---- 外部接口 ----
_INSTANCE: Optional[VideoAnalyzer] = None


def get_video_analyzer() -> VideoAnalyzer:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = VideoAnalyzer()
    return _INSTANCE


def is_video_analysis_available() -> bool:
    return True


def get_video_analysis_status() -> Dict[str, Any]:
    try:
        info = video.get_system_info()  # type: ignore[attr-defined]
    except Exception as e:  # pragma: no cover
        return {"available": False, "error": str(e)}
    inst = get_video_analyzer()
    return {
        "available": True,
        "system": info,
        "modes": ["auto", "batch", "sequential"],
        "max_frames_default": inst.max_frames,
        "implementation": "inkfox",
    }
