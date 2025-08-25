#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
视频帧提取工作函数 - 用于多进程
这个模块专门用于子进程，避免重复加载主框架
"""

import cv2
import base64
import io
import numpy as np
from PIL import Image
from typing import List, Tuple


def extract_frames_worker(video_path: str, max_frames: int, frame_quality: int, max_image_size: int) -> List[Tuple[str, float]]:
    """在子进程中提取视频帧的工作函数"""
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
        # 在子进程中不能使用logger，返回错误信息
        return [("ERROR", str(e))]
