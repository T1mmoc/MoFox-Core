#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
视频分析器模块 - 旧版本兼容模块
支持多种分析模式：批处理、逐帧、自动选择
包含Python原生的抽帧功能，作为Rust模块的降级方案
"""

import os
import cv2
import asyncio
import base64
import numpy as np
from PIL import Image
from pathlib import Path
from typing import List, Tuple, Optional, Any
import io
from concurrent.futures import ThreadPoolExecutor

from src.llm_models.utils_model import LLMRequest
from src.config.config import global_config, model_config
from src.common.logger import get_logger

logger = get_logger("utils_video_legacy")


def _extract_frames_worker(
    video_path: str,
    max_frames: int,
    frame_quality: int,
    max_image_size: int,
    frame_extraction_mode: str,
    frame_interval_seconds: Optional[float],
) -> list[Any] | list[tuple[str, str]]:
    """线程池中提取视频帧的工作函数"""
    frames = []
    try:
        cap = cv2.VideoCapture(video_path)
        fps = cap.get(cv2.CAP_PROP_FPS)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        duration = total_frames / fps if fps > 0 else 0

        if frame_extraction_mode == "time_interval":
            # 新模式：按时间间隔抽帧
            time_interval = frame_interval_seconds
            next_frame_time = 0.0
            extracted_count = 0  # 初始化提取帧计数器

            while cap.isOpened():
                ret, frame = cap.read()
                if not ret:
                    break

                current_time = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0

                if current_time >= next_frame_time:
                    # 转换为PIL图像并压缩
                    frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    pil_image = Image.fromarray(frame_rgb)

                    # 调整图像大小
                    if max(pil_image.size) > max_image_size:
                        ratio = max_image_size / max(pil_image.size)
                        new_size = tuple(int(dim * ratio) for dim in pil_image.size)
                        pil_image = pil_image.resize(new_size, Image.Resampling.LANCZOS)

                    # 转换为base64
                    buffer = io.BytesIO()
                    pil_image.save(buffer, format="JPEG", quality=frame_quality)
                    frame_base64 = base64.b64encode(buffer.getvalue()).decode("utf-8")

                    frames.append((frame_base64, current_time))
                    extracted_count += 1

                    # 注意：这里不能使用logger，因为在线程池中
                    # logger.debug(f"提取第{extracted_count}帧 (时间: {current_time:.2f}s)")

                    next_frame_time += time_interval
        else:
            # 使用numpy优化帧间隔计算
            if duration > 0:
                frame_interval = max(1, int(duration / max_frames * fps))
            else:
                frame_interval = 30  # 默认间隔

            # 使用numpy计算目标帧位置
            target_frames = np.arange(0, min(max_frames, total_frames // frame_interval + 1)) * frame_interval
            target_frames = target_frames[target_frames < total_frames].astype(int)

            for target_frame in target_frames:
                # 跳转到目标帧
                cap.set(cv2.CAP_PROP_POS_FRAMES, target_frame)
                ret, frame = cap.read()
                if not ret:
                    continue

                # 使用numpy优化图像处理
                frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

                # 转换为PIL图像并使用numpy进行尺寸计算
                height, width = frame_rgb.shape[:2]
                max_dim = max(height, width)

                if max_dim > max_image_size:
                    # 使用numpy计算缩放比例
                    ratio = max_image_size / max_dim
                    new_width = int(width * ratio)
                    new_height = int(height * ratio)

                    # 使用opencv进行高效缩放
                    frame_resized = cv2.resize(frame_rgb, (new_width, new_height), interpolation=cv2.INTER_LANCZOS4)
                    pil_image = Image.fromarray(frame_resized)
                else:
                    pil_image = Image.fromarray(frame_rgb)

                # 转换为base64
                buffer = io.BytesIO()
                pil_image.save(buffer, format="JPEG", quality=frame_quality)
                frame_base64 = base64.b64encode(buffer.getvalue()).decode("utf-8")

                # 计算时间戳
                timestamp = target_frame / fps if fps > 0 else 0
                frames.append((frame_base64, timestamp))

        cap.release()
        return frames

    except Exception as e:
        # 返回错误信息
        return [("ERROR", str(e))]


class LegacyVideoAnalyzer:
    """旧版本兼容的视频分析器类"""

    def __init__(self):
        """初始化视频分析器"""
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

        logger.info(
            f"✅ 旧版本视频分析器初始化完成，分析模式: {self.analysis_mode}, 线程池: {self.use_multiprocessing}"
        )

    async def extract_frames(self, video_path: str) -> List[Tuple[str, float]]:
        """提取视频帧 - 支持多进程和单线程模式"""
        # 先获取视频信息
        cap = cv2.VideoCapture(video_path)
        fps = cap.get(cv2.CAP_PROP_FPS)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        duration = total_frames / fps if fps > 0 else 0
        cap.release()

        logger.info(f"视频信息: {total_frames}帧, {fps:.2f}FPS, {duration:.2f}秒")

        # 估算提取帧数
        if duration > 0:
            frame_interval = max(1, int(duration / self.max_frames * fps))
            estimated_frames = min(self.max_frames, total_frames // frame_interval + 1)
        else:
            estimated_frames = self.max_frames

        logger.info(f"计算得出帧间隔: {frame_interval} (将提取约{estimated_frames}帧)")

        # 根据配置选择处理方式
        if self.use_multiprocessing:
            return await self._extract_frames_multiprocess(video_path)
        else:
            return await self._extract_frames_fallback(video_path)

    async def _extract_frames_multiprocess(self, video_path: str) -> List[Tuple[str, float]]:
        """线程池版本的帧提取"""
        loop = asyncio.get_event_loop()

        try:
            logger.info("🔄 启动线程池帧提取...")
            # 使用线程池，避免进程间的导入问题
            with ThreadPoolExecutor(max_workers=1) as executor:
                frames = await loop.run_in_executor(
                    executor,
                    _extract_frames_worker,
                    video_path,
                    self.max_frames,
                    self.frame_quality,
                    self.max_image_size,
                    self.frame_extraction_mode,
                    self.frame_interval_seconds,
                )

            # 检查是否有错误
            if frames and frames[0][0] == "ERROR":
                logger.error(f"线程池帧提取失败: {frames[0][1]}")
                # 降级到单线程模式
                logger.info("🔄 降级到单线程模式...")
                return await self._extract_frames_fallback(video_path)

            logger.info(f"✅ 成功提取{len(frames)}帧 (线程池模式)")
            return frames

        except Exception as e:
            logger.error(f"线程池帧提取失败: {e}")
            # 降级到原始方法
            logger.info("🔄 降级到单线程模式...")
            return await self._extract_frames_fallback(video_path)

    async def _extract_frames_fallback(self, video_path: str) -> List[Tuple[str, float]]:
        """帧提取的降级方法 - 原始异步版本"""
        frames = []
        extracted_count = 0
        cap = cv2.VideoCapture(video_path)
        fps = cap.get(cv2.CAP_PROP_FPS)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        duration = total_frames / fps if fps > 0 else 0

        logger.info(f"视频信息: {total_frames}帧, {fps:.2f}FPS, {duration:.2f}秒")

        if self.frame_extraction_mode == "time_interval":
            # 新模式：按时间间隔抽帧
            time_interval = self.frame_interval_seconds
            next_frame_time = 0.0

            while cap.isOpened():
                ret, frame = cap.read()
                if not ret:
                    break

                current_time = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0

                if current_time >= next_frame_time:
                    # 转换为PIL图像并压缩
                    frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    pil_image = Image.fromarray(frame_rgb)

                    # 调整图像大小
                    if max(pil_image.size) > self.max_image_size:
                        ratio = self.max_image_size / max(pil_image.size)
                        new_size = tuple(int(dim * ratio) for dim in pil_image.size)
                        pil_image = pil_image.resize(new_size, Image.Resampling.LANCZOS)

                    # 转换为base64
                    buffer = io.BytesIO()
                    pil_image.save(buffer, format="JPEG", quality=self.frame_quality)
                    frame_base64 = base64.b64encode(buffer.getvalue()).decode("utf-8")

                    frames.append((frame_base64, current_time))
                    extracted_count += 1

                    logger.debug(f"提取第{extracted_count}帧 (时间: {current_time:.2f}s)")

                    next_frame_time += time_interval
        else:
            # 使用numpy优化帧间隔计算
            if duration > 0:
                frame_interval = max(1, int(duration / self.max_frames * fps))
            else:
                frame_interval = 30  # 默认间隔

            logger.info(
                f"计算得出帧间隔: {frame_interval} (将提取约{min(self.max_frames, total_frames // frame_interval + 1)}帧)"
            )

            # 使用numpy计算目标帧位置
            target_frames = np.arange(0, min(self.max_frames, total_frames // frame_interval + 1)) * frame_interval
            target_frames = target_frames[target_frames < total_frames].astype(int)

            extracted_count = 0

            for target_frame in target_frames:
                # 跳转到目标帧
                cap.set(cv2.CAP_PROP_POS_FRAMES, target_frame)
                ret, frame = cap.read()
                if not ret:
                    continue

                # 使用numpy优化图像处理
                frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

                # 转换为PIL图像并使用numpy进行尺寸计算
                height, width = frame_rgb.shape[:2]
                max_dim = max(height, width)

                if max_dim > self.max_image_size:
                    # 使用numpy计算缩放比例
                    ratio = self.max_image_size / max_dim
                    new_width = int(width * ratio)
                    new_height = int(height * ratio)

                    # 使用opencv进行高效缩放
                    frame_resized = cv2.resize(frame_rgb, (new_width, new_height), interpolation=cv2.INTER_LANCZOS4)
                    pil_image = Image.fromarray(frame_resized)
                else:
                    pil_image = Image.fromarray(frame_rgb)

                # 转换为base64
                buffer = io.BytesIO()
                pil_image.save(buffer, format="JPEG", quality=self.frame_quality)
                frame_base64 = base64.b64encode(buffer.getvalue()).decode("utf-8")

                # 计算时间戳
                timestamp = target_frame / fps if fps > 0 else 0
                frames.append((frame_base64, timestamp))
                extracted_count += 1

                logger.debug(f"提取第{extracted_count}帧 (时间: {timestamp:.2f}s, 帧号: {target_frame})")

                # 每提取一帧让步一次
                await asyncio.sleep(0.001)

        cap.release()
        logger.info(f"✅ 成功提取{len(frames)}帧")
        return frames

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
            # 尝试使用多图片分析
            response = await self._analyze_multiple_frames(frames, prompt)
            logger.info("✅ 视频识别完成")
            return response

        except Exception as e:
            logger.error(f"❌ 视频识别失败: {e}")
            # 降级到单帧分析
            logger.warning("降级到单帧分析模式")
            try:
                frame_base64, timestamp = frames[0]
                fallback_prompt = (
                    prompt
                    + f"\n\n注意：由于技术限制，当前仅显示第1帧 (时间: {timestamp:.2f}s)，视频共有{len(frames)}帧。请基于这一帧进行分析。"
                )

                response, _ = await self.video_llm.generate_response_for_image(
                    prompt=fallback_prompt, image_base64=frame_base64, image_format="jpeg"
                )
                logger.info("✅ 降级的单帧分析完成")
                return response
            except Exception as fallback_e:
                logger.error(f"❌ 降级分析也失败: {fallback_e}")
                raise

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
            raise RuntimeError("无法为视频分析选择可用模型 (legacy)。")
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

    async def analyze_video(self, video_path: str, user_question: str = None) -> str:
        """分析视频的主要方法"""
        try:
            logger.info(f"开始分析视频: {os.path.basename(video_path)}")

            # 提取帧
            frames = await self.extract_frames(video_path)
            if not frames:
                return "❌ 无法从视频中提取有效帧"

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
            return result

        except Exception as e:
            error_msg = f"❌ 视频分析失败: {str(e)}"
            logger.error(error_msg)
            return error_msg

    @staticmethod
    def is_supported_video(file_path: str) -> bool:
        """检查是否为支持的视频格式"""
        supported_formats = {".mp4", ".avi", ".mov", ".mkv", ".flv", ".wmv", ".m4v", ".3gp", ".webm"}
        return Path(file_path).suffix.lower() in supported_formats


# 全局实例
_legacy_video_analyzer = None


def get_legacy_video_analyzer() -> LegacyVideoAnalyzer:
    """获取旧版本视频分析器实例（单例模式）"""
    global _legacy_video_analyzer
    if _legacy_video_analyzer is None:
        _legacy_video_analyzer = LegacyVideoAnalyzer()
    return _legacy_video_analyzer
