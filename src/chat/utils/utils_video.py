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
import hashlib
import time
import numpy as np
from PIL import Image
from pathlib import Path
from typing import List, Tuple, Optional, Dict
import io
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import numpy as np

from src.llm_models.utils_model import LLMRequest
from src.config.config import global_config, model_config
from src.common.logger import get_logger
from src.common.database.sqlalchemy_models import get_db_session, Videos

logger = get_logger("utils_video")

# 全局正在处理的视频哈希集合，用于防止重复处理
processing_videos = set()
processing_lock = asyncio.Lock()
# 为每个视频hash创建独立的锁和事件
video_locks = {}
video_events = {}
video_lock_manager = asyncio.Lock()


def _extract_frames_worker(video_path: str, max_frames: int, frame_quality: int, max_image_size: int) -> List[Tuple[str, float]]:
    """线程池中提取视频帧的工作函数"""
    frames = []
    try:
        cap = cv2.VideoCapture(video_path)
        fps = cap.get(cv2.CAP_PROP_FPS)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        duration = total_frames / fps if fps > 0 else 0
        
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
            pil_image.save(buffer, format='JPEG', quality=frame_quality)
            frame_base64 = base64.b64encode(buffer.getvalue()).decode('utf-8')
            
            # 计算时间戳
            timestamp = target_frame / fps if fps > 0 else 0
            frames.append((frame_base64, timestamp))
        
        cap.release()
        return frames
        
    except Exception as e:
        # 返回错误信息
        return [("ERROR", str(e))]


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
            # 新增的线程池配置
            self.use_multiprocessing = getattr(config, 'use_multiprocessing', True)
            self.max_workers = getattr(config, 'max_workers', 2)
            
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
            logger.info("✅ 从配置文件读取视频分析参数")
            
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
            self.use_multiprocessing = True  # 默认启用线程池
            self.max_workers = 2  # 默认最大2个线程
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
        
        logger.info(f"✅ 视频分析器初始化完成，分析模式: {self.analysis_mode}, 线程池: {self.use_multiprocessing}")

    def _calculate_video_hash(self, video_data: bytes) -> str:
        """计算视频文件的hash值"""
        hash_obj = hashlib.sha256()
        hash_obj.update(video_data)
        return hash_obj.hexdigest()
    
    def _check_video_exists(self, video_hash: str) -> Optional[Videos]:
        """检查视频是否已经分析过"""
        try:
            with get_db_session() as session:
                # 明确刷新会话以确保看到其他事务的最新提交
                session.expire_all()
                return session.query(Videos).filter(Videos.video_hash == video_hash).first()
        except Exception as e:
            logger.warning(f"检查视频是否存在时出错: {e}")
            return None
    
    def _store_video_result(self, video_hash: str, description: str, metadata: Optional[Dict] = None) -> Optional[Videos]:
        """存储视频分析结果到数据库"""
        try:
            with get_db_session() as session:
                # 只根据video_hash查找
                existing_video = session.query(Videos).filter(
                    Videos.video_hash == video_hash
                ).first()
                
                if existing_video:
                    # 如果已存在，更新描述和计数
                    existing_video.description = description
                    existing_video.count += 1
                    existing_video.timestamp = time.time()
                    if metadata:
                        existing_video.duration = metadata.get('duration')
                        existing_video.frame_count = metadata.get('frame_count')
                        existing_video.fps = metadata.get('fps')
                        existing_video.resolution = metadata.get('resolution')
                        existing_video.file_size = metadata.get('file_size')
                    session.commit()
                    session.refresh(existing_video)
                    logger.info(f"✅ 更新已存在的视频记录，hash: {video_hash[:16]}..., count: {existing_video.count}")
                    return existing_video
                else:
                    video_record = Videos(
                        video_hash=video_hash,
                        description=description,
                        timestamp=time.time(),
                        count=1
                    )
                    if metadata:
                        video_record.duration = metadata.get('duration')
                        video_record.frame_count = metadata.get('frame_count')
                        video_record.fps = metadata.get('fps')
                        video_record.resolution = metadata.get('resolution')
                        video_record.file_size = metadata.get('file_size')
                    
                    session.add(video_record)
                    session.commit()
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
                    self.max_image_size
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
        cap = cv2.VideoCapture(video_path)
        fps = cap.get(cv2.CAP_PROP_FPS)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        duration = total_frames / fps if fps > 0 else 0
        
        logger.info(f"视频信息: {total_frames}帧, {fps:.2f}FPS, {duration:.2f}秒")
        
        # 使用numpy优化帧间隔计算
        if duration > 0:
            frame_interval = max(1, int(duration / self.max_frames * fps))
        else:
            frame_interval = 30  # 默认间隔
            
        logger.info(f"计算得出帧间隔: {frame_interval} (将提取约{min(self.max_frames, total_frames // frame_interval + 1)}帧)")

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
            pil_image.save(buffer, format='JPEG', quality=self.frame_quality)
            frame_base64 = base64.b64encode(buffer.getvalue()).decode('utf-8')
            
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
        
        # 构建提示词
        prompt = self.batch_analysis_prompt
        
        if user_question:
            prompt += f"\n\n用户问题: {user_question}"
        
        # 添加帧信息到提示词
        frame_info = []
        for i, (_frame_base64, timestamp) in enumerate(frames):
            if self.enable_frame_timing:
                frame_info.append(f"第{i+1}帧 (时间: {timestamp:.2f}s)")
            else:
                frame_info.append(f"第{i+1}帧")
        
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
                fallback_prompt = prompt + f"\n\n注意：由于技术限制，当前仅显示第1帧 (时间: {timestamp:.2f}s)，视频共有{len(frames)}帧。请基于这一帧进行分析。"
                
                response, _ = await self.video_llm.generate_response_for_image(
                    prompt=fallback_prompt,
                    image_base64=frame_base64,
                    image_format="jpeg"
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
            max_tokens=None
        )
        
        logger.info(f"视频识别完成，响应长度: {len(api_response.content or '')} ")
        return api_response.content or "❌ 未获得响应内容"

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
            filename: 文件名（可选，仅用于日志）
            user_question: 用户问题（旧参数名，保持兼容性）
            prompt: 提示词（新参数名，与系统调用保持一致）
            
        Returns:
            Dict[str, str]: 包含分析结果的字典，格式为 {"summary": "分析结果"}
        """
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
            with tempfile.NamedTemporaryFile(delete=False, suffix='.mp4') as temp_file:
                temp_file.write(video_bytes)
                temp_path = temp_file.name
            
            try:
                # 检查临时文件是否创建成功
                if not os.path.exists(temp_path):
                    video_event.set()  # 通知等待者
                    return {"summary": "❌ 临时文件创建失败"}
                
                # 使用临时文件进行分析
                result = await self.analyze_video(temp_path, question)
                
            finally:
                # 清理临时文件
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
            
            # 保存分析结果到数据库
            metadata = {
                "filename": filename,
                "file_size": len(video_bytes),
                "analysis_timestamp": time.time()
            }
            self._store_video_result(
                video_hash=video_hash,
                description=result,
                metadata=metadata
            )
            
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
            
            # 即使失败也保存错误信息到数据库，避免重复处理
            try:
                metadata = {
                    "filename": filename,
                    "file_size": len(video_bytes),
                    "analysis_timestamp": time.time(),
                    "error": str(e)
                }
                self._store_video_result(
                    video_hash=video_hash,
                    description=error_msg,
                    metadata=metadata
                )
                logger.info("✅ 错误信息已保存到数据库")
            except Exception as store_e:
                logger.error(f"❌ 保存错误信息失败: {store_e}")
            
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
