#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
视频分析器模块 - Rust优化版本
集成了Rust视频关键帧提取模块，提供高性能的视频分析功能
支持SIMD优化、多线程处理和智能关键帧检测
"""

import os
import tempfile
import asyncio
import base64
import hashlib
import time
import numpy as np
from PIL import Image
from pathlib import Path
from typing import List, Tuple, Optional, Dict
import io

from src.llm_models.utils_model import LLMRequest
from src.config.config import global_config, model_config
from src.common.logger import get_logger
from src.common.database.sqlalchemy_models import get_db_session, Videos

logger = get_logger("utils_video")

# Rust模块可用性检测
RUST_VIDEO_AVAILABLE = False
try:
    import rust_video

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
    """优化的视频分析器类"""

    def __init__(self):
        """初始化视频分析器"""
        # 检查是否有任何可用的视频处理实现
        opencv_available = False
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
            logger.info("✅ 使用video_analysis模型配置")
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
            config,
            "batch_analysis_prompt",
            """请以第一人称的视角来观看这一个视频，你看到的这些是从视频中按时间顺序提取的关键帧。

你的核心人设是：{personality_core}。
你的人格细节是：{personality_side}。

请提供详细的视频内容描述，涵盖以下方面：
1. 视频的整体内容和主题
2. 主要人物、对象和场景描述
3. 动作、情节和时间线发展
4. 视觉风格和艺术特点
5. 整体氛围和情感表达
6. 任何特殊的视觉效果或文字内容

请用中文回答，结果要详细准确。""",
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
            logger.info("✅ 从配置文件读取视频分析参数")
        else:
            logger.warning("配置文件中缺少video_analysis配置，使用默认值")

        # 系统提示词
        self.system_prompt = "你是一个专业的视频内容分析助手。请仔细观察用户提供的视频关键帧，详细描述视频内容。"

        logger.info(f"✅ 视频分析器初始化完成，分析模式: {self.analysis_mode}, 线程池: {self.use_multiprocessing}")

        # 获取Rust模块系统信息
        self._log_system_info()

    @staticmethod
    def _log_system_info():
        """记录系统信息"""
        if not RUST_VIDEO_AVAILABLE:
            logger.info("⚠️ Rust模块不可用，跳过系统信息获取")
            return

        try:
            system_info = rust_video.get_system_info()
            logger.info(f"🔧 系统信息: 线程数={system_info.get('threads', '未知')}")

            # 记录CPU特性
            features = []
            if system_info.get("avx2_supported"):
                features.append("AVX2")
            if system_info.get("sse2_supported"):
                features.append("SSE2")
            if system_info.get("simd_supported"):
                features.append("SIMD")

            if features:
                logger.info(f"🚀 CPU特性: {', '.join(features)}")
            else:
                logger.info("⚠️ 未检测到SIMD支持")

            logger.info(f"📦 Rust模块版本: {system_info.get('version', '未知')}")

        except Exception as e:
            logger.warning(f"获取系统信息失败: {e}")

    @staticmethod
    def _calculate_video_hash(video_data: bytes) -> str:
        """计算视频文件的hash值"""
        hash_obj = hashlib.sha256()
        hash_obj.update(video_data)
        return hash_obj.hexdigest()

    @staticmethod
    def _check_video_exists(video_hash: str) -> Optional[Videos]:
        """检查视频是否已经分析过"""
        try:
            with get_db_session() as session:
                # 明确刷新会话以确保看到其他事务的最新提交
                session.expire_all()
                return session.query(Videos).filter(Videos.video_hash == video_hash).first()
        except Exception as e:
            logger.warning(f"检查视频是否存在时出错: {e}")
            return None

    @staticmethod
    def _store_video_result(
            video_hash: str, description: str, metadata: Optional[Dict] = None
    ) -> Optional[Videos]:
        """存储视频分析结果到数据库"""
        # 检查描述是否为错误信息，如果是则不保存
        if description.startswith("❌"):
            logger.warning(f"⚠️ 检测到错误信息，不保存到数据库: {description[:50]}...")
            return None

        try:
            with get_db_session() as session:
                # 只根据video_hash查找
                existing_video = session.query(Videos).filter(Videos.video_hash == video_hash).first()

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
                    session.refresh(existing_video)
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

                    await session.add(video_record)
                    await session.commit()
                    session.refresh(video_record)
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

    async def _extract_frames_rust_advanced(self, video_path: str) -> List[Tuple[str, float]]:
        """使用 Rust 高级接口的帧提取"""
        try:
            logger.info("🔄 使用 Rust 高级接口提取关键帧...")

            # 创建 Rust 视频处理器，使用配置参数
            extractor = rust_video.VideoKeyframeExtractor(
                ffmpeg_path=self.ffmpeg_path,
                threads=self.rust_threads,
                verbose=False,  # 使用固定值，不需要配置
            )

            # 1. 提取所有帧
            frames_data, width, height = extractor.extract_frames(
                video_path=video_path,
                max_frames=self.max_frames * 3,  # 提取更多帧用于关键帧检测
            )

            logger.info(f"提取到 {len(frames_data)} 帧，视频尺寸: {width}x{height}")

            # 2. 检测关键帧，使用配置参数
            keyframe_indices = extractor.extract_keyframes(
                frames=frames_data,
                threshold=self.rust_keyframe_threshold,
                use_simd=self.rust_use_simd,
                block_size=self.rust_block_size,
            )

            logger.info(f"检测到 {len(keyframe_indices)} 个关键帧")

            # 3. 转换选定的关键帧为 base64
            frames = []
            frame_count = 0

            for idx in keyframe_indices[: self.max_frames]:
                if idx < len(frames_data):
                    try:
                        frame = frames_data[idx]
                        frame_data = frame.get_data()

                        # 将灰度数据转换为PIL图像
                        frame_array = np.frombuffer(frame_data, dtype=np.uint8).reshape((frame.height, frame.width))
                        pil_image = Image.fromarray(
                            frame_array,
                            mode="L",  # 灰度模式
                        )

                        # 转换为RGB模式以便保存为JPEG
                        pil_image = pil_image.convert("RGB")

                        # 调整图像大小
                        if max(pil_image.size) > self.max_image_size:
                            ratio = self.max_image_size / max(pil_image.size)
                            new_size = tuple(int(dim * ratio) for dim in pil_image.size)
                            pil_image = pil_image.resize(new_size, Image.Resampling.LANCZOS)

                        # 转换为 base64
                        buffer = io.BytesIO()
                        pil_image.save(buffer, format="JPEG", quality=self.frame_quality)
                        frame_base64 = base64.b64encode(buffer.getvalue()).decode("utf-8")

                        # 估算时间戳
                        estimated_timestamp = frame.frame_number * (1.0 / 30.0)  # 假设30fps

                        frames.append((frame_base64, estimated_timestamp))
                        frame_count += 1

                        logger.debug(
                            f"处理关键帧 {frame_count}: 帧号 {frame.frame_number}, 时间 {estimated_timestamp:.2f}s"
                        )

                    except Exception as e:
                        logger.error(f"处理关键帧 {idx} 失败: {e}")
                        continue

            logger.info(f"✅ Rust 高级提取完成: {len(frames)} 关键帧")
            return frames

        except Exception as e:
            logger.error(f"❌ Rust 高级帧提取失败: {e}")
            # 回退到基础方法
            logger.info("回退到基础 Rust 方法")
            return await self._extract_frames_rust(video_path)

    async def _extract_frames_rust(self, video_path: str) -> List[Tuple[str, float]]:
        """使用 Rust 实现的帧提取"""
        try:
            logger.info("🔄 使用 Rust 模块提取关键帧...")

            # 创建临时输出目录
            with tempfile.TemporaryDirectory() as temp_dir:
                # 使用便捷函数进行关键帧提取，使用配置参数
                result = rust_video.extract_keyframes_from_video(
                    video_path=video_path,
                    output_dir=temp_dir,
                    threshold=self.rust_keyframe_threshold,
                    max_frames=self.max_frames * 2,  # 提取更多帧以便筛选
                    max_save=self.max_frames,
                    ffmpeg_path=self.ffmpeg_path,
                    use_simd=self.rust_use_simd,
                    threads=self.rust_threads,
                    verbose=False,  # 使用固定值，不需要配置
                )

                logger.info(
                    f"Rust 处理完成: 总帧数 {result.total_frames}, 关键帧 {result.keyframes_extracted}, 处理速度 {result.processing_fps:.1f} FPS"
                )

                # 转换保存的关键帧为 base64 格式
                frames = []
                temp_dir_path = Path(temp_dir)

                # 获取所有保存的关键帧文件
                keyframe_files = sorted(temp_dir_path.glob("keyframe_*.jpg"))

                for i, keyframe_file in enumerate(keyframe_files):
                    if len(frames) >= self.max_frames:
                        break

                    try:
                        # 读取关键帧文件
                        with open(keyframe_file, "rb") as f:
                            image_data = f.read()

                        # 转换为 PIL 图像并压缩
                        pil_image = Image.open(io.BytesIO(image_data))

                        # 调整图像大小
                        if max(pil_image.size) > self.max_image_size:
                            ratio = self.max_image_size / max(pil_image.size)
                            new_size = tuple(int(dim * ratio) for dim in pil_image.size)
                            pil_image = pil_image.resize(new_size, Image.Resampling.LANCZOS)

                        # 转换为 base64
                        buffer = io.BytesIO()
                        pil_image.save(buffer, format="JPEG", quality=self.frame_quality)
                        frame_base64 = base64.b64encode(buffer.getvalue()).decode("utf-8")

                        # 估算时间戳（基于帧索引和总时长）
                        if result.total_frames > 0:
                            # 假设关键帧在时间上均匀分布
                            estimated_timestamp = (i * result.total_time_ms / 1000.0) / result.keyframes_extracted
                        else:
                            estimated_timestamp = i * 1.0  # 默认每秒一帧

                        frames.append((frame_base64, estimated_timestamp))

                        logger.debug(f"处理关键帧 {i + 1}: 估算时间 {estimated_timestamp:.2f}s")

                    except Exception as e:
                        logger.error(f"处理关键帧 {keyframe_file.name} 失败: {e}")
                        continue

                logger.info(f"✅ Rust 提取完成: {len(frames)} 关键帧")
                return frames

        except Exception as e:
            logger.error(f"❌ Rust 帧提取失败: {e}")
            raise e

    async def _extract_frames_python_fallback(self, video_path: str) -> List[Tuple[str, float]]:
        """Python降级抽帧实现 - 支持多种抽帧模式"""
        try:
            # 导入旧版本分析器
            from .utils_video_legacy import get_legacy_video_analyzer

            logger.info("🔄 使用Python降级抽帧实现...")
            legacy_analyzer = get_legacy_video_analyzer()

            # 同步配置参数
            legacy_analyzer.max_frames = self.max_frames
            legacy_analyzer.frame_quality = self.frame_quality
            legacy_analyzer.max_image_size = self.max_image_size
            legacy_analyzer.frame_extraction_mode = self.frame_extraction_mode
            legacy_analyzer.frame_interval_seconds = self.frame_interval_seconds
            legacy_analyzer.use_multiprocessing = self.use_multiprocessing

            # 使用旧版本的抽帧功能
            frames = await legacy_analyzer.extract_frames(video_path)

            logger.info(f"✅ Python降级抽帧完成: {len(frames)} 帧")
            return frames

        except Exception as e:
            logger.error(f"❌ Python降级抽帧失败: {e}")
            return []

    async def analyze_frames_batch(self, frames: List[Tuple[str, float]], user_question: str = None) -> str:
        """批量分析所有帧"""
        logger.info(f"开始批量分析{len(frames)}帧")

        if not frames:
            return "❌ 没有可分析的帧"

        # 构建提示词并格式化人格信息，要不然占位符的那个会爆炸
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
        model_info, api_provider, client = self.video_llm._select_model()
        # logger.info(f"使用模型: {model_info.name} 进行多帧分析")

        # 直接执行多图片请求
        api_response = await self.video_llm._execute_request(
            api_provider=api_provider,
            client=client,
            request_type=RequestType.RESPONSE,
            model_info=model_info,
            message_list=[message],
            temperature=None,
            max_tokens=None,
        )

        logger.info(f"视频识别完成，响应长度: {len(api_response.content or '')} ")
        return api_response.content or "❌ 未获得响应内容"

    async def analyze_frames_sequential(self, frames: List[Tuple[str, float]], user_question: str = None) -> str:
        """逐帧分析并汇总"""
        logger.info(f"开始逐帧分析{len(frames)}帧")

        frame_analyses = []

        for i, (frame_base64, timestamp) in enumerate(frames):
            try:
                prompt = f"请分析这个视频的第{i + 1}帧"
                if self.enable_frame_timing:
                    prompt += f" (时间: {timestamp:.2f}s)"
                prompt += "。描述你看到的内容，包括人物、动作、场景、文字等。"

                if user_question:
                    prompt += f"\n特别关注: {user_question}"

                response, _ = await self.video_llm.generate_response_for_image(
                    prompt=prompt, image_base64=frame_base64, image_format="jpeg"
                )

                frame_analyses.append(f"第{i + 1}帧 ({timestamp:.2f}s): {response}")
                logger.debug(f"✅ 第{i + 1}帧分析完成")

                # API调用间隔
                if i < len(frames) - 1:
                    await asyncio.sleep(self.frame_analysis_delay)

            except Exception as e:
                logger.error(f"❌ 第{i + 1}帧分析失败: {e}")
                frame_analyses.append(f"第{i + 1}帧: 分析失败 - {e}")

        # 生成汇总
        logger.info("开始生成汇总分析")
        summary_prompt = f"""基于以下各帧的分析结果，请提供一个完整的视频内容总结：

{chr(10).join(frame_analyses)}

请综合所有帧的信息，描述视频的整体内容、故事线、主要元素和特点。"""

        if user_question:
            summary_prompt += f"\n特别回答用户的问题: {user_question}"

        try:
            # 使用最后一帧进行汇总分析
            if frames:
                last_frame_base64, _ = frames[-1]
                summary, _ = await self.video_llm.generate_response_for_image(
                    prompt=summary_prompt, image_base64=last_frame_base64, image_format="jpeg"
                )
                logger.info("✅ 逐帧分析和汇总完成")
                return summary
            else:
                return "❌ 没有可用于汇总的帧"
        except Exception as e:
            logger.error(f"❌ 汇总分析失败: {e}")
            # 如果汇总失败，返回各帧分析结果
            return f"视频逐帧分析结果：\n\n{chr(10).join(frame_analyses)}"

    async def analyze_video(self, video_path: str, user_question: str = None) -> Tuple[bool, str]:
        """分析视频的主要方法

        Returns:
            Tuple[bool, str]: (是否成功, 分析结果或错误信息)
        """
        if self.disabled:
            error_msg = "❌ 视频分析功能已禁用：没有可用的视频处理实现"
            logger.warning(error_msg)
            return False, error_msg

        try:
            logger.info(f"开始分析视频: {os.path.basename(video_path)}")

            # 提取帧
            frames = await self.extract_frames(video_path)
            if not frames:
                error_msg = "❌ 无法从视频中提取有效帧"
                return False, error_msg

            # 根据模式选择分析方法
            if self.analysis_mode == "auto":
                # 智能选择：少于等于3帧用批量，否则用逐帧
                mode = "batch" if len(frames) <= 3 else "sequential"
                logger.info(f"自动选择分析模式: {mode} (基于{len(frames)}帧)")
            else:
                mode = self.analysis_mode

            # 执行分析
            if mode == "batch":
                result = await self.analyze_frames_batch(frames, user_question)
            else:  # sequential
                result = await self.analyze_frames_sequential(frames, user_question)

            logger.info("✅ 视频分析完成")
            return True, result

        except Exception as e:
            error_msg = f"❌ 视频分析失败: {str(e)}"
            logger.error(error_msg)
            return False, error_msg

    async def analyze_video_from_bytes(
        self, video_bytes: bytes, filename: str = None, user_question: str = None, prompt: str = None
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
                    existing_video = self._check_video_exists(video_hash)
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
                existing_video = self._check_video_exists(video_hash)
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
                    # 检查临时文件是否创建成功
                    if not os.path.exists(temp_path):
                        video_event.set()  # 通知等待者
                        return {"summary": "❌ 临时文件创建失败"}

                    # 使用临时文件进行分析
                    success, result = await self.analyze_video(temp_path, question)

                finally:
                    # 清理临时文件
                    if os.path.exists(temp_path):
                        os.unlink(temp_path)

                # 保存分析结果到数据库（仅保存成功的结果）
                if success:
                    metadata = {"filename": filename, "file_size": len(video_bytes), "analysis_timestamp": time.time()}
                    self._store_video_result(video_hash=video_hash, description=result, metadata=metadata)
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

            # 不保存错误信息到数据库，允许后续重试
            logger.info("💡 错误信息不保存到数据库，允许后续重试")

            # 处理失败，通知等待者并清理资源
            try:
                if video_hash and video_event:
                    async with video_lock_manager:
                        if video_hash in video_events:
                            video_events[video_hash].set()
                        video_locks.pop(video_hash, None)
                        video_events.pop(video_hash, None)
            except Exception as cleanup_e:
                logger.error(f"❌ 清理锁资源失败: {cleanup_e}")

            return {"summary": error_msg}

    @staticmethod
    def is_supported_video(file_path: str) -> bool:
        """检查是否为支持的视频格式"""
        supported_formats = {".mp4", ".avi", ".mov", ".mkv", ".flv", ".wmv", ".m4v", ".3gp", ".webm"}
        return Path(file_path).suffix.lower() in supported_formats

    def get_processing_capabilities(self) -> Dict[str, any]:
        """获取处理能力信息"""
        if not RUST_VIDEO_AVAILABLE:
            return {"error": "Rust视频处理模块不可用", "available": False, "reason": "rust_video模块未安装或加载失败"}

        try:
            system_info = rust_video.get_system_info()

            # 创建一个临时的extractor来获取CPU特性
            extractor = rust_video.VideoKeyframeExtractor(threads=0, verbose=False)
            cpu_features = extractor.get_cpu_features()

            capabilities = {
                "system": {
                    "threads": system_info.get("threads", 0),
                    "rust_version": system_info.get("version", "unknown"),
                },
                "cpu_features": cpu_features,
                "recommended_settings": self._get_recommended_settings(cpu_features),
                "analysis_modes": ["auto", "batch", "sequential"],
                "supported_formats": [".mp4", ".avi", ".mov", ".mkv", ".flv", ".wmv", ".m4v", ".3gp", ".webm"],
                "available": True,
            }

            return capabilities

        except Exception as e:
            logger.error(f"获取处理能力信息失败: {e}")
            return {"error": str(e), "available": False}

    @staticmethod
    def _get_recommended_settings(cpu_features: Dict[str, bool]) -> Dict[str, any]:
        """根据CPU特性推荐最佳设置"""
        settings = {
            "use_simd": any(cpu_features.values()),
            "block_size": 8192,
            "threads": 0,  # 自动检测
        }

        # 根据CPU特性调整设置
        if cpu_features.get("avx2", False):
            settings["block_size"] = 16384  # AVX2支持更大的块
            settings["optimization_level"] = "avx2"
        elif cpu_features.get("sse2", False):
            settings["block_size"] = 8192
            settings["optimization_level"] = "sse2"
        else:
            settings["use_simd"] = False
            settings["block_size"] = 4096
            settings["optimization_level"] = "scalar"

        return settings


# 全局实例
_video_analyzer = None


def get_video_analyzer() -> VideoAnalyzer:
    """获取视频分析器实例（单例模式）"""
    global _video_analyzer
    if _video_analyzer is None:
        _video_analyzer = VideoAnalyzer()
    return _video_analyzer


def is_video_analysis_available() -> bool:
    """检查视频分析功能是否可用

    Returns:
        bool: 如果有任何可用的视频处理实现则返回True
    """
    # 现在即使Rust模块不可用，也可以使用Python降级实现
    try:
        import cv2

        return True
    except ImportError:
        return False


def get_video_analysis_status() -> Dict[str, any]:
    """获取视频分析功能的详细状态信息

    Returns:
        Dict[str, any]: 包含功能状态信息的字典
    """
    # 检查OpenCV是否可用
    opencv_available = False
    try:
        import cv2

        opencv_available = True
    except ImportError:
        pass

    status = {
        "available": opencv_available or RUST_VIDEO_AVAILABLE,
        "implementations": {
            "rust_keyframe": {
                "available": RUST_VIDEO_AVAILABLE,
                "description": "Rust智能关键帧提取",
                "supported_modes": ["keyframe"],
            },
            "python_legacy": {
                "available": opencv_available,
                "description": "Python传统抽帧方法",
                "supported_modes": ["fixed_number", "time_interval"],
            },
        },
        "supported_modes": [],
    }

    # 汇总支持的模式
    if RUST_VIDEO_AVAILABLE:
        status["supported_modes"].extend(["keyframe"])
    if opencv_available:
        status["supported_modes"].extend(["fixed_number", "time_interval"])

    if not status["available"]:
        status.update({"error": "没有可用的视频处理实现", "solution": "请安装opencv-python或rust_video模块"})

    return status
