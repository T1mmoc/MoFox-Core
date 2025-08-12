#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
视频分析器模块 - 优化版本
支持多种分析模式：批处理、逐帧、自动选择
"""

import os
import cv2
import tempfile
import asyncio
import base64
from PIL import Image
from pathlib import Path
from typing import List, Tuple, Optional, Dict
import io

from src.llm_models.utils_model import LLMRequest
from src.config.config import global_config, model_config
from src.common.logger import get_logger

logger = get_logger("src.multimodal.video_analyzer")


class VideoAnalyzer:
    """优化的视频分析器类"""
    
    def __init__(self):
        """初始化视频分析器"""
        # 使用专用的视频分析配置
        try:
            self.video_llm = LLMRequest(
                model_set=model_config.model_task_config.video_analysis,
                request_type="video_analysis"
            )
            logger.info("✅ 使用video_analysis模型配置")
        except (AttributeError, KeyError) as e:
            # 如果video_analysis不存在，使用vlm配置
            self.video_llm = LLMRequest(
                model_set=model_config.model_task_config.vlm,
                request_type="vlm"
            )
            logger.warning(f"video_analysis配置不可用({e})，回退使用vlm配置")
        
        # 从配置文件读取参数，如果配置不存在则使用默认值
        try:
            config = global_config.video_analysis
            self.max_frames = config.max_frames
            self.frame_quality = config.frame_quality
            self.max_image_size = config.max_image_size
            self.enable_frame_timing = config.enable_frame_timing
            self.batch_analysis_prompt = config.batch_analysis_prompt
            
            # 将配置文件中的模式映射到内部使用的模式名称
            config_mode = config.analysis_mode
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
            logger.info(f"✅ 从配置文件读取视频分析参数")
            
        except AttributeError as e:
            # 如果配置不存在，使用代码中的默认值
            logger.warning(f"配置文件中缺少video_analysis配置({e})，使用默认值")
            self.max_frames = 6
            self.frame_quality = 85
            self.max_image_size = 600
            self.analysis_mode = "auto"
            self.frame_analysis_delay = 0.3
            self.frame_interval = 1.0  # 抽帧时间间隔（秒）
            self.batch_size = 3  # 批处理时每批处理的帧数
            self.timeout = 60.0  # 分析超时时间（秒）
            self.enable_frame_timing = True
            self.batch_analysis_prompt = """请分析这个视频的内容。这些图片是从视频中按时间顺序提取的关键帧。

请提供详细的分析，包括：
1. 视频的整体内容和主题
2. 主要人物、对象和场景描述
3. 动作、情节和时间线发展
4. 视觉风格和艺术特点
5. 整体氛围和情感表达
6. 任何特殊的视觉效果或文字内容

请用中文回答，分析要详细准确。"""
        
        # 系统提示词
        self.system_prompt = "你是一个专业的视频内容分析助手。请仔细观察用户提供的视频关键帧，详细描述视频内容。"
        
        logger.info(f"✅ 视频分析器初始化完成，分析模式: {self.analysis_mode}")

    def set_analysis_mode(self, mode: str):
        """设置分析模式"""
        if mode in ["batch", "sequential", "auto"]:
            self.analysis_mode = mode
            # logger.info(f"分析模式已设置为: {mode}")
        else:
            logger.warning(f"无效的分析模式: {mode}")

    async def extract_frames(self, video_path: str) -> List[Tuple[str, float]]:
        """提取视频帧"""
        frames = []
        cap = cv2.VideoCapture(video_path)
        fps = cap.get(cv2.CAP_PROP_FPS)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        duration = total_frames / fps if fps > 0 else 0
        
        logger.info(f"视频信息: {total_frames}帧, {fps:.2f}FPS, {duration:.2f}秒")
        
        # 动态计算帧间隔
        if duration > 0:
            frame_interval = max(1, int(duration / self.max_frames * fps))
        else:
            frame_interval = 30  # 默认间隔
        
        frame_count = 0
        extracted_count = 0
        
        while cap.isOpened() and extracted_count < self.max_frames:
            ret, frame = cap.read()
            if not ret:
                break
                
            if frame_count % frame_interval == 0:
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
                pil_image.save(buffer, format='JPEG', quality=self.frame_quality)
                frame_base64 = base64.b64encode(buffer.getvalue()).decode('utf-8')
                
                # 计算时间戳
                timestamp = frame_count / fps if fps > 0 else 0
                frames.append((frame_base64, timestamp))
                extracted_count += 1
                
                logger.debug(f"📸 提取第{extracted_count}帧 (时间: {timestamp:.2f}s)")
            
            frame_count += 1
        
        cap.release()
        logger.info(f"✅ 成功提取{len(frames)}帧")
        return frames

    async def analyze_frames_batch(self, frames: List[Tuple[str, float]], user_question: str = None) -> str:
        """批量分析所有帧"""
        logger.info(f"开始批量分析{len(frames)}帧")
        
        # 构建提示词
        prompt = self.batch_analysis_prompt
        
        if user_question:
            prompt += f"\n\n用户问题: {user_question}"
        
        # 添加帧信息到提示词
        for i, (frame_base64, timestamp) in enumerate(frames):
            if self.enable_frame_timing:
                prompt += f"\n\n第{i+1}帧 (时间: {timestamp:.2f}s):"
        
        try:
            # 使用第一帧进行分析（批量模式暂时使用单帧，后续可以优化为真正的多图片分析）
            if frames:
                frame_base64, _ = frames[0]
                prompt += f"\n\n注意：当前显示的是第1帧，请基于这一帧和提示词进行分析。视频共有{len(frames)}帧。"
                
                response, _ = await self.video_llm.generate_response_for_image(
                    prompt=prompt,
                    image_base64=frame_base64,
                    image_format="jpeg"
                )
                logger.info("✅ 批量分析完成")
                return response
            else:
                return "❌ 没有可分析的帧"
        except Exception as e:
            logger.error(f"❌ 批量分析失败: {e}")
            raise

    async def analyze_frames_sequential(self, frames: List[Tuple[str, float]], user_question: str = None) -> str:
        """逐帧分析并汇总"""
        logger.info(f"开始逐帧分析{len(frames)}帧")
        
        frame_analyses = []
        
        for i, (frame_base64, timestamp) in enumerate(frames):
            try:
                prompt = f"请分析这个视频的第{i+1}帧"
                if self.enable_frame_timing:
                    prompt += f" (时间: {timestamp:.2f}s)"
                prompt += "。描述你看到的内容，包括人物、动作、场景、文字等。"
                
                if user_question:
                    prompt += f"\n特别关注: {user_question}"
                
                response, _ = await self.video_llm.generate_response_for_image(
                    prompt=prompt,
                    image_base64=frame_base64,
                    image_format="jpeg"
                )
                
                frame_analyses.append(f"第{i+1}帧 ({timestamp:.2f}s): {response}")
                logger.debug(f"✅ 第{i+1}帧分析完成")
                
                # API调用间隔
                if i < len(frames) - 1:
                    await asyncio.sleep(self.frame_analysis_delay)
                    
            except Exception as e:
                logger.error(f"❌ 第{i+1}帧分析失败: {e}")
                frame_analyses.append(f"第{i+1}帧: 分析失败 - {e}")
        
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
                    prompt=summary_prompt,
                    image_base64=last_frame_base64,
                    image_format="jpeg"
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

    async def analyze_video_from_bytes(self, video_bytes: bytes, filename: str = None, user_question: str = None, prompt: str = None) -> Dict[str, str]:
        """从字节数据分析视频
        
        Args:
            video_bytes: 视频字节数据
            filename: 文件名（可选）
            user_question: 用户问题（旧参数名，保持兼容性）
            prompt: 提示词（新参数名，与系统调用保持一致）
            
        Returns:
            Dict[str, str]: 包含分析结果的字典，格式为 {"summary": "分析结果"}
        """
        try:
            logger.info("开始从字节数据分析视频")
            
            # 兼容性处理：如果传入了prompt参数，使用prompt；否则使用user_question
            question = prompt if prompt is not None else user_question
            
            # 检查视频数据是否有效
            if not video_bytes:
                return {"summary": "❌ 视频数据为空"}
            
            # 创建临时文件保存视频数据
            with tempfile.NamedTemporaryFile(delete=False, suffix='.mp4') as temp_file:
                temp_file.write(video_bytes)
                temp_path = temp_file.name
            
            try:
                # 检查临时文件是否创建成功
                if not os.path.exists(temp_path):
                    return {"summary": "❌ 临时文件创建失败"}
                
                # 使用临时文件进行分析
                result = await self.analyze_video(temp_path, question)
                return {"summary": result}
            finally:
                # 清理临时文件
                try:
                    if os.path.exists(temp_path):
                        os.unlink(temp_path)
                        logger.debug("临时文件已清理")
                except Exception as e:
                    logger.warning(f"清理临时文件失败: {e}")
                    
        except Exception as e:
            error_msg = f"❌ 从字节数据分析视频失败: {str(e)}"
            logger.error(error_msg)
            return {"summary": error_msg}

    def is_supported_video(self, file_path: str) -> bool:
        """检查是否为支持的视频格式"""
        supported_formats = {'.mp4', '.avi', '.mov', '.mkv', '.flv', '.wmv', '.m4v', '.3gp', '.webm'}
        return Path(file_path).suffix.lower() in supported_formats


# 全局实例
_video_analyzer = None

def get_video_analyzer() -> VideoAnalyzer:
    """获取视频分析器实例（单例模式）"""
    global _video_analyzer
    if _video_analyzer is None:
        _video_analyzer = VideoAnalyzer()
    return _video_analyzer