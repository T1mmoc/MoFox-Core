import asyncio
import datetime
import time
import traceback
import os
import json
from typing import Dict, List, Any

from src.common.logger import get_logger
from src.plugin_system.apis import llm_api, config_api

# 导入工具模块
import sys
import os
sys.path.append(os.path.dirname(__file__))

from qzone_utils import QZoneManager, get_send_history

# 获取日志记录器
logger = get_logger('MaiZone-Scheduler')


class ScheduleManager:
    """定时任务管理器 - 负责定时发送说说"""
    
    def __init__(self, plugin):
        """初始化定时任务管理器"""
        self.plugin = plugin
        self.is_running = False
        self.task = None
        self.last_send_times: Dict[str, float] = {}  # 记录每个时间点的最后发送时间
        
        logger.info("定时任务管理器初始化完成")

    async def start(self):
        """启动定时任务"""
        if self.is_running:
            logger.warning("定时任务已在运行中")
            return
            
        self.is_running = True
        self.task = asyncio.create_task(self._schedule_loop())
        logger.info("定时发送说说任务已启动")

    async def stop(self):
        """停止定时任务"""
        if not self.is_running:
            return
            
        self.is_running = False
        
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                logger.info("定时任务已被取消")
                
        logger.info("定时发送说说任务已停止")

    async def _schedule_loop(self):
        """定时任务主循环"""
        while self.is_running:
            try:
                # 检查定时任务是否启用
                if not self.plugin.get_config("schedule.enable_schedule", False):
                    logger.info("定时任务已禁用，等待下次检查")
                    await asyncio.sleep(60)
                    continue
                
                # 获取当前时间
                current_time = datetime.datetime.now().strftime("%H:%M")
                
                # 从插件配置中获取定时任务
                schedules = self.plugin.get_config("schedule.schedules", {})

                if not schedules:
                    logger.info("未找到有效的定时任务配置")
                    await asyncio.sleep(60)
                    continue
                
                # 检查每个定时任务
                for time_str, topic in schedules.items():
                    schedule = {"time": time_str, "topic": topic}
                    await self._check_and_execute_schedule(schedule, current_time)
                
                # 每分钟检查一次
                await asyncio.sleep(60)
                
            except asyncio.CancelledError:
                logger.info("定时任务循环被取消")
                break
            except Exception as e:
                logger.error(f"定时任务循环出错: {str(e)}")
                logger.error(traceback.format_exc())
                # 出错后等待5分钟再重试
                await asyncio.sleep(300)

    async def _check_and_execute_schedule(self, schedule: Dict[str, Any], current_time: str):
        """检查并执行定时任务"""
        try:
            schedule_time = schedule.get("time", "")
            topic = schedule.get("topic", "")
            
            # 检查是否到达发送时间
            if current_time == schedule_time:
                # 避免同一分钟内重复发送
                last_send_time = self.last_send_times.get(schedule_time, 0)
                current_timestamp = time.time()
                
                if current_timestamp - last_send_time > 60:  # 超过1分钟才允许发送
                    logger.info(f"定时任务触发: {schedule_time} - 主题: {topic}")
                    self.last_send_times[schedule_time] = current_timestamp
                    
                    # 执行发送任务
                    success = await self._execute_scheduled_send(topic)
                    
                    if success:
                        logger.info(f"定时说说发送成功: {topic}")
                    else:
                        logger.error(f"定时说说发送失败: {topic}")
                else:
                    logger.debug(f"跳过重复发送: {schedule_time}")
                    
        except Exception as e:
            logger.error(f"检查定时任务失败: {str(e)}")

    async def _execute_scheduled_send(self, topic: str) -> bool:
        """执行定时发送任务"""
        try:
            # 生成说说内容
            story = await self._generate_story_content(topic)
            if not story:
                logger.error("生成定时说说内容失败")
                return False

            logger.info(f"定时任务生成说说内容: '{story}'")

            # 处理配图
            await self._handle_images(story)
            
            # 发送说说
            success = await self._send_scheduled_feed(story)
            
            return success
            
        except Exception as e:
            logger.error(f"执行定时发送任务失败: {str(e)}")
            return False

    async def _generate_story_content(self, topic: str) -> str:
        """生成定时说说内容"""
        try:
            # 获取模型配置
            models = llm_api.get_available_models()
            text_model = str(self.plugin.get_config("models.text_model", "replyer_1"))
            model_config = models.get(text_model)
            
            if not model_config:
                logger.error("未配置LLM模型")
                return ""

            # 获取机器人信息
            bot_personality = config_api.get_global_config("personality.personality_core", "一个机器人")
            bot_expression = config_api.get_global_config("expression.expression_style", "内容积极向上")
            qq_account = config_api.get_global_config("bot.qq_account", "")

            # 构建提示词
            if topic:
                prompt = f"""
                你是'{bot_personality}'，你想写一条主题是'{topic}'的说说发表在qq空间上，
                {bot_expression}
                不要刻意突出自身学科背景，不要浮夸，不要夸张修辞，可以适当使用颜文字，
                只输出一条说说正文的内容，不要有其他的任何正文以外的冗余输出
                """
            else:
                prompt = f"""
                你是'{bot_personality}'，你想写一条说说发表在qq空间上，主题不限
                {bot_expression}
                不要刻意突出自身学科背景，不要浮夸，不要夸张修辞，可以适当使用颜文字，
                只输出一条说说正文的内容，不要有其他的任何正文以外的冗余输出
                """

            # 添加历史记录避免重复
            prompt += "\n以下是你最近发过的说说，写新说说时注意不要在相隔不长的时间发送相似内容的说说\n"
            history_block = await get_send_history(qq_account)
            if history_block:
                prompt += history_block

            # 生成内容
            success, story, reasoning, model_name = await llm_api.generate_with_model(
                prompt=prompt,
                model_config=model_config,
                request_type="story.generate",
                temperature=0.3,
                max_tokens=1000
            )

            if success:
                return story
            else:
                logger.error("生成定时说说内容失败")
                return ""
                
        except Exception as e:
            logger.error(f"生成定时说说内容异常: {str(e)}")
            return ""

    async def _handle_images(self, story: str):
        """处理定时说说配图"""
        try:
            enable_ai_image = bool(self.plugin.get_config("send.enable_ai_image", False))
            apikey = str(self.plugin.get_config("models.siliconflow_apikey", ""))
            image_dir = str(self.plugin.get_config("send.image_directory", "./plugins/Maizone/images"))
            image_num = int(self.plugin.get_config("send.ai_image_number", 1) or 1)
            
            if enable_ai_image and apikey:
                from qzone_utils import generate_image_by_sf
                await generate_image_by_sf(
                    api_key=apikey, 
                    story=story, 
                    image_dir=image_dir, 
                    batch_size=image_num
                )
                logger.info("定时任务AI配图生成完成")
            elif enable_ai_image and not apikey:
                logger.warning('启用了AI配图但未填写API密钥')
                
        except Exception as e:
            logger.error(f"处理定时说说配图失败: {str(e)}")

    async def _send_scheduled_feed(self, story: str) -> bool:
        """发送定时说说"""
        try:
            # 获取配置
            port = str(self.plugin.get_config("plugin.http_port", "3000"))
            host = self.plugin.get_config("plugin.http_host", "127.0.0.1")
            qq_account = config_api.get_global_config("bot.qq_account", "")
            enable_image = self.plugin.get_config("send.enable_image", False)
            image_dir = str(self.plugin.get_config("send.image_directory", "./plugins/Maizone/images"))

            # 创建QZone管理器并发送
            qzone_manager = QZoneManager(port, host)
            success = await qzone_manager.send_feed(story, image_dir, qq_account, enable_image)
            
            if success:
                logger.info(f"定时说说发送成功: {story}")
            else:
                logger.error("定时说说发送失败")
                
            return success
            
        except Exception as e:
            logger.error(f"发送定时说说失败: {str(e)}")
            return False

    def get_status(self) -> Dict[str, Any]:
        """获取定时任务状态"""
        return {
            "is_running": self.is_running,
            "enabled": self.plugin.get_config("schedule.enable_schedule", False),
            "schedules": self.plugin.get_config("schedule.schedules", {}),
            "last_send_times": self.last_send_times
        }

    def add_schedule(self, time_str: str, topic: str) -> bool:
        """添加定时任务"""
        schedules = self.plugin.get_config("schedule.schedules", {})
        
        if time_str in schedules:
            logger.warning(f"时间 {time_str} 已存在定时任务")
            return False
        
        schedules[time_str] = topic
        # 注意：这里需要插件系统支持动态更新配置
        logger.info(f"添加定时任务: {time_str} - {topic}")
        return True

    def remove_schedule(self, time_str: str) -> bool:
        """移除定时任务"""
        schedules = self.plugin.get_config("schedule.schedules", {})
        
        if time_str in schedules:
            del schedules[time_str]
            # 注意：这里需要插件系统支持动态更新配置
            logger.info(f"移除定时任务: {time_str}")
            return True
        else:
            logger.warning(f"未找到时间为 {time_str} 的定时任务")
            return False