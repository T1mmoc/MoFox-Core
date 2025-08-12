#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
视频集成测试
测试Bot端对从Adapter发送的视频消息处理
"""

import base64
import asyncio
from pathlib import Path

from maim_message import Seg
from src.chat.message_receive.message import MessageRecv


async def test_video_integration():
    """测试视频消息集成"""
    print("🎬 视频消息集成测试")
    print("=" * 60)
    
    # 视频文件路径
    video_path = Path("../参考文件/小猫烧.mp4")
    
    if not video_path.exists():
        print(f"视频文件不存在: {video_path}")
        return
    
    try:
        # 读取视频文件并编码为base64（模拟Adapter处理）
        with open(video_path, "rb") as f:
            video_bytes = f.read()
        
        video_base64 = base64.b64encode(video_bytes).decode('utf-8')
        file_size_mb = len(video_bytes) / (1024 * 1024)
        
        print(f"视频文件: {video_path}")
        print(f"文件大小: {file_size_mb:.2f} MB")
        print(f"Base64长度: {len(video_base64)} 字符")
        
        # 创建视频消息段（模拟Adapter发送的格式）
        video_segment = Seg(
            type="video",
            data={
                "base64": video_base64,
                "filename": video_path.name,
                "size_mb": file_size_mb,
                "url": "http://example.com/video.mp4"  # 模拟URL
            }
        )
        
        print("\n📤 模拟Adapter发送视频消息...")
        
        # 创建消息接收对象（模拟Bot接收）
        message_dict = {
            "message_info": {},
            "message_segment": {
                "type": "seglist",
                "data": [video_segment.to_dict()]
            },
            "raw_message": "测试视频消息"
        }
        
        # 创建MessageRecv实例
        message_recv = MessageRecv(message_dict)
        
        print("🤖 Bot端开始处理视频消息...")
        
        # 处理消息（这会调用video analyzer）
        await message_recv.process()
        
        print(f"\n✅ 处理完成!")
        print(f"处理结果: {message_recv.processed_plain_text}")
        
        # 输出一些状态信息
        print(f"\n📊 消息状态:")
        print(f"  - 是否为图片: {message_recv.is_picid}")
        print(f"  - 是否为表情: {message_recv.is_emoji}")
        print(f"  - 是否为语音: {message_recv.is_voice}")
        
    except Exception as e:
        print(f"❌ 测试失败: {str(e)}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(test_video_integration())
