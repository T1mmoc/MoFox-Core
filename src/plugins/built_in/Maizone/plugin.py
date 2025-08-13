import asyncio
import random
import time
from typing import List, Tuple, Type

from src.common.logger import get_logger
from src.plugin_system import (
    BasePlugin, register_plugin, BaseAction, BaseCommand,
    ComponentInfo, ActionActivationType, ChatMode
)
from src.plugin_system.apis import llm_api, config_api, person_api, generator_api
from src.plugin_system.base.config_types import ConfigField

# 导入插件工具模块
import sys
import os
sys.path.append(os.path.dirname(__file__))

from qzone_utils import (
    QZoneManager, generate_image_by_sf, get_send_history
)
from scheduler import ScheduleManager
from config_loader import MaiZoneConfigLoader

# 获取日志记录器
logger = get_logger('MaiZone')


# ===== 发送说说命令组件 =====
class SendFeedCommand(BaseCommand):
    """发送说说命令 - 响应 /send_feed 命令"""

    command_name = "send_feed"
    command_description = "发送一条QQ空间说说"
    command_pattern = r"^/send_feed(?:\s+(?P<topic>\w+))?$"
    command_help = "发一条主题为<topic>或随机的说说"
    command_examples = ["/send_feed", "/send_feed 日常"]
    intercept_message = True
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 获取配置加载器引用
        self.config_loader = None
        self._init_config_loader()
    
    def _init_config_loader(self):
        """初始化配置加载器"""
        try:
            plugin_dir = os.path.dirname(__file__)
            self.config_loader = MaiZoneConfigLoader(plugin_dir)
            self.config_loader.load_config()
        except Exception as e:
            logger.error(f"初始化配置加载器失败: {e}")
    
    def get_config(self, key: str, default=None):
        """获取配置值"""
        if self.config_loader:
            return self.config_loader.get_config(key, default)
        return default

    def check_permission(self, qq_account: str) -> bool:
        """检查用户权限"""
        
        permission_list = self.get_config("send.permission", [])
        permission_type = self.get_config("send.permission_type", "whitelist")
        
        logger.info(f'权限检查: {permission_type}:{permission_list}')
            
        if not isinstance(permission_list, list):
            logger.error("权限列表配置错误")
            return False
            
        if permission_type == 'whitelist':
            return qq_account in permission_list
        elif permission_type == 'blacklist':
            return qq_account not in permission_list
        else:
            logger.error('权限类型配置错误，应为 whitelist 或 blacklist')
            return False

    async def execute(self) -> Tuple[bool, str, bool]:
        """执行发送说说命令"""
        try:
            # 获取用户信息
            user_id = self.message.message_info.user_info.user_id if self.message and self.message.message_info and self.message.message_info.user_info else None
            
            # 权限检查
            if not user_id or not self.check_permission(user_id):
                logger.info(f"用户 {user_id} 权限不足")
                await self.send_text("权限不足，无法使用此命令")
                return False, "权限不足", True

            # 获取主题
            topic = self.matched_groups.get("topic", "")
            
            # 生成说说内容
            story = await self._generate_story_content(topic)
            if not story:
                return False, "生成说说内容失败", True

            # 处理图片
            await self._handle_images(story)
            
            # 发送说说
            success = await self._send_feed(story)
            if success:
                if self.get_config("send.enable_reply", True):
                    await self.send_text(f"已发送说说：\n{story}")
                return True, "发送成功", True
            else:
                return False, "发送说说失败", True
                
        except Exception as e:
            logger.error(f"发送说说命令执行失败: {str(e)}")
            return False, "命令执行失败", True

    async def _generate_story_content(self, topic: str) -> str:
        """生成说说内容"""
        try:
            # 获取模型配置
            models = llm_api.get_available_models()
            text_model = str(self.get_config("models.text_model", "replyer_1"))
            model_config = models.get(text_model)
            
            if not model_config:
                logger.error("未配置LLM模型")
                return ""

            # 获取机器人信息
            bot_personality = config_api.get_global_config("personality.personality_core", "一个机器人")
            bot_expression = config_api.get_global_config("personality.reply_style", "内容积极向上")
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

            # 添加历史记录
            prompt += "\n以下是你以前发过的说说，写新说说时注意不要在相隔不长的时间发送相同主题的说说"
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
                logger.info(f"成功生成说说内容：'{story}'")
                return story
            else:
                logger.error("生成说说内容失败")
                return ""
                
        except Exception as e:
            logger.error(f"生成说说内容异常: {str(e)}")
            return ""

    async def _handle_images(self, story: str):
        """处理说说配图"""
        try:
            enable_ai_image = bool(self.get_config("send.enable_ai_image", False))
            apikey = str(self.get_config("models.siliconflow_apikey", ""))
            image_dir = str(self.get_config("send.image_directory", "./plugins/Maizone/images"))
            image_num_raw = self.get_config("send.ai_image_number", 1)
            image_num = int(image_num_raw if image_num_raw is not None else 1)
            
            if enable_ai_image and apikey:
                await generate_image_by_sf(
                    api_key=apikey, 
                    story=story, 
                    image_dir=image_dir, 
                    batch_size=image_num
                )
            elif enable_ai_image and not apikey:
                logger.error('启用了AI配图但未填写API密钥')
                
        except Exception as e:
            logger.error(f"处理配图失败: {str(e)}")

    async def _send_feed(self, story: str) -> bool:
        """发送说说到QQ空间"""
        try:
            # 获取配置
            qq_account = config_api.get_global_config("bot.qq_account", "")
            enable_image = bool(self.get_config("send.enable_image", False))
            image_dir = str(self.get_config("send.image_directory", "./plugins/Maizone/images"))

            # 获取聊天流ID
            stream_id = self.message.chat_stream.stream_id if self.message and self.message.chat_stream else None

            # 创建QZone管理器并发送
            qzone_manager = QZoneManager(stream_id)
            success = await qzone_manager.send_feed(story, image_dir, qq_account, enable_image)
            
            return success
            
        except Exception as e:
            logger.error(f"发送说说失败: {str(e)}")
            return False


# ===== 发送说说动作组件 =====
class SendFeedAction(BaseAction):
    """发送说说动作 - 当用户要求发说说时激活"""

    action_name = "send_feed"
    action_description = "发一条相应主题的说说"
    activation_type = ActionActivationType.KEYWORD
    mode_enable = ChatMode.ALL
    
    activation_keywords = ["说说", "空间", "动态"]
    keyword_case_sensitive = False

    action_parameters = {
        "topic": "要发送的说说主题",
        "user_name": "要求你发说说的好友的qq名称",
    }
    action_require = [
        "用户要求发说说时使用",
        "当有人希望你更新qq空间时使用",
        "当你认为适合发说说时使用",
    ]
    associated_types = ["text"]
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 获取配置加载器引用
        self.config_loader = None
        self._init_config_loader()
    
    def _init_config_loader(self):
        """初始化配置加载器"""
        try:
            plugin_dir = os.path.dirname(__file__)
            self.config_loader = MaiZoneConfigLoader(plugin_dir)
            self.config_loader.load_config()
        except Exception as e:
            logger.error(f"初始化配置加载器失败: {e}")
    
    def get_config(self, key: str, default=None):
        """获取配置值"""
        if self.config_loader:
            return self.config_loader.get_config(key, default)
        return default

    def check_permission(self, qq_account: str) -> bool:
        """检查用户权限"""
        permission_list = self.get_config("send.permission", [])
        permission_type = self.get_config("send.permission_type", "whitelist")
        
        logger.info(f'权限检查: {permission_type}:{permission_list}')
        
        if isinstance(permission_list, list):
            if permission_type == 'whitelist':
                return qq_account in permission_list
            elif permission_type == 'blacklist':
                return qq_account not in permission_list
        
        logger.error('权限类型配置错误')
        return False

    async def execute(self) -> Tuple[bool, str]:
        """执行发送说说动作"""
        try:
            # 获取用户信息
            user_name = self.action_data.get("user_name", "")
            person_id = person_api.get_person_id_by_name(user_name)
            user_id = await person_api.get_person_value(person_id, "user_id")
            
            # 权限检查
            if not self.check_permission(user_id):
                logger.info(f"用户 {user_id} 权限不足")
                success, reply_set, _ = await generator_api.generate_reply(
                    chat_stream=self.chat_stream,
                    action_data={"extra_info_block": f'{user_name}无权命令你发送说说，请用符合你人格特点的方式拒绝请求'}
                )
                if success and reply_set:
                    for reply_type, reply_content in reply_set:
                        if reply_type == "text":
                            await self.send_text(reply_content)
                return False, "权限不足"

            # 获取主题并生成内容
            topic = self.action_data.get("topic", "")
            story = await self._generate_story_content(topic)
            if not story:
                return False, "生成说说内容失败"

            # 处理图片
            await self._handle_images(story)
            
            # 发送说说
            success = await self._send_feed(story)
            if success:
                logger.info(f"成功发送说说: {story}")
                
                # 生成回复
                success, reply_set, _ = await generator_api.generate_reply(
                    chat_stream=self.chat_stream,
                    action_data={"extra_info_block": f'你刚刚发了一条说说，内容为{story}'}
                )

                if success and reply_set:
                    for reply_type, reply_content in reply_set:
                        if reply_type == "text":
                            await self.send_text(reply_content)
                    return True, '发送成功'
                else:
                    await self.send_text('我发了一条说说啦~')
                    return True, '发送成功但回复生成失败'
            else:
                return False, "发送说说失败"
                
        except Exception as e:
            logger.error(f"发送说说动作执行失败: {str(e)}")
            return False, "动作执行失败"

    async def _generate_story_content(self, topic: str) -> str:
        """生成说说内容"""
        try:
            # 获取模型配置
            models = llm_api.get_available_models()
            text_model = str(self.get_config("models.text_model", "replyer_1"))
            model_config = models.get(text_model)
            
            if not model_config:
                return ""

            # 获取机器人信息
            bot_personality = config_api.get_global_config("personality.personality_core", "一个机器人")
            bot_expression = config_api.get_global_config("expression.expression_style", "内容积极向上")
            qq_account = config_api.get_global_config("bot.qq_account", "")

            # 构建提示词
            prompt = f"""
            你是{bot_personality}，你想写一条主题是{topic}的说说发表在qq空间上，
            {bot_expression}
            不要刻意突出自身学科背景，不要浮夸，不要夸张修辞，可以适当使用颜文字，
            只输出一条说说正文的内容，不要有其他的任何正文以外的冗余输出
            """
            
            # 添加历史记录
            prompt += "\n以下是你以前发过的说说，写新说说时注意不要在相隔不长的时间发送相同主题的说说"
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
                return ""
                
        except Exception as e:
            logger.error(f"生成说说内容异常: {str(e)}")
            return ""

    async def _handle_images(self, story: str):
        """处理说说配图"""
        try:
            enable_ai_image = bool(self.get_config("send.enable_ai_image", False))
            apikey = str(self.get_config("models.siliconflow_apikey", ""))
            image_dir = str(self.get_config("send.image_directory", "./plugins/Maizone/images"))
            image_num_raw = self.get_config("send.ai_image_number", 1)
            image_num = int(image_num_raw if image_num_raw is not None else 1)
            
            if enable_ai_image and apikey:
                await generate_image_by_sf(
                    api_key=apikey, 
                    story=story, 
                    image_dir=image_dir, 
                    batch_size=image_num
                )
            elif enable_ai_image and not apikey:
                logger.error('启用了AI配图但未填写API密钥')
                
        except Exception as e:
            logger.error(f"处理配图失败: {str(e)}")

    async def _send_feed(self, story: str) -> bool:
        """发送说说到QQ空间"""
        try:
            # 获取配置
            qq_account = config_api.get_global_config("bot.qq_account", "")
            enable_image = bool(self.get_config("send.enable_image", False))
            image_dir = str(self.get_config("send.image_directory", "./plugins/Maizone/images"))

            # 获取聊天流ID
            stream_id = self.chat_stream.stream_id if self.chat_stream else None

            # 创建QZone管理器并发送
            qzone_manager = QZoneManager(stream_id)
            success = await qzone_manager.send_feed(story, image_dir, qq_account, enable_image)
            
            return success
            
        except Exception as e:
            logger.error(f"发送说说失败: {str(e)}")
            return False


# ===== 阅读说说动作组件 =====
class ReadFeedAction(BaseAction):
    """阅读说说动作 - 当用户要求读说说时激活"""

    action_name = "read_feed"
    action_description = "读取好友最近的动态/说说/qq空间并评论点赞"
    activation_type = ActionActivationType.KEYWORD
    mode_enable = ChatMode.ALL
    
    activation_keywords = ["说说", "空间", "动态"]
    keyword_case_sensitive = False

    action_parameters = {
        "target_name": "需要阅读动态的好友的qq名称",
        "user_name": "要求你阅读动态的好友的qq名称"
    }

    action_require = [
        "需要阅读某人动态、说说、QQ空间时使用",
        "当有人希望你评价某人的动态、说说、QQ空间",
        "当你认为适合阅读说说、动态、QQ空间时使用",
    ]
    associated_types = ["text"]
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 获取配置加载器引用
        self.config_loader = None
        self._init_config_loader()
    
    def _init_config_loader(self):
        """初始化配置加载器"""
        try:
            plugin_dir = os.path.dirname(__file__)
            self.config_loader = MaiZoneConfigLoader(plugin_dir)
            self.config_loader.load_config()
        except Exception as e:
            logger.error(f"初始化配置加载器失败: {e}")
    
    def get_config(self, key: str, default=None):
        """获取配置值"""
        if self.config_loader:
            return self.config_loader.get_config(key, default)
        return default

    def check_permission(self, qq_account: str) -> bool:
        """检查用户权限"""
        permission_list = self.get_config("read.permission", [])
        permission_type = self.get_config("read.permission_type", "blacklist")
        
        if not isinstance(permission_list, list):
            return False
            
        logger.info(f'权限检查: {permission_type}:{permission_list}')
        
        if permission_type == 'whitelist':
            return qq_account in permission_list
        elif permission_type == 'blacklist':
            return qq_account not in permission_list
        else:
            logger.error('权限类型配置错误')
            return False

    async def execute(self) -> Tuple[bool, str]:
        """执行阅读说说动作"""
        try:
            # 获取用户信息
            user_name = self.action_data.get("user_name", "")
            person_id = person_api.get_person_id_by_name(user_name)
            user_id = await person_api.get_person_value(person_id, "user_id")
            
            # 权限检查
            if not self.check_permission(user_id):
                logger.info(f"用户 {user_id} 权限不足")
                success, reply_set, _ = await generator_api.generate_reply(
                    chat_stream=self.chat_stream,
                    action_data={"extra_info_block": f'{user_name}无权命令你阅读说说，请用符合人格的方式进行拒绝的回复'}
                )
                if success and reply_set:
                    for reply_type, reply_content in reply_set:
                        if reply_type == "text":
                            await self.send_text(reply_content)
                return False, "权限不足"

            # 获取目标用户
            target_name = self.action_data.get("target_name", "")
            target_person_id = person_api.get_person_id_by_name(target_name)
            target_qq = await person_api.get_person_value(target_person_id, "user_id")
            
            # 读取并处理说说
            success = await self._read_and_process_feeds(target_qq, target_name)
            
            if success:
                # 生成回复
                success, reply_set, _ = await generator_api.generate_reply(
                    chat_stream=self.chat_stream,
                    action_data={"extra_info_block": f'你刚刚成功读了{target_name}的说说，请告知你已经读了说说'}
                )

                if success and reply_set:
                    for reply_type, reply_content in reply_set:
                        if reply_type == "text":
                            await self.send_text(reply_content)
                    return True, '阅读成功'
                return True, '阅读成功但回复生成失败'
            else:
                return False, "阅读说说失败"
                
        except Exception as e:
            logger.error(f"阅读说说动作执行失败: {str(e)}")
            return False, "动作执行失败"

    async def _read_and_process_feeds(self, target_qq: str, target_name: str) -> bool:
        """读取并处理说说"""
        try:
            # 获取配置
            qq_account = config_api.get_global_config("bot.qq_account", "")
            num_raw = self.get_config("read.read_number", 5)
            num = int(num_raw if num_raw is not None else 5)
            like_raw = self.get_config("read.like_possibility", 1.0)
            like_possibility = float(like_raw if like_raw is not None else 1.0)
            comment_raw = self.get_config("read.comment_possibility", 1.0)
            comment_possibility = float(comment_raw if comment_raw is not None else 1.0)

            # 获取聊天流ID
            stream_id = self.chat_stream.stream_id if self.chat_stream else None

            # 创建QZone管理器并读取说说
            qzone_manager = QZoneManager(stream_id)
            feeds_list = await qzone_manager.read_feed(qq_account, target_qq, num)
            
            # 处理错误情况
            if isinstance(feeds_list, list) and len(feeds_list) > 0 and isinstance(feeds_list[0], dict) and 'error' in feeds_list[0]:
                success, reply_set, _ = await generator_api.generate_reply(
                    chat_stream=self.chat_stream,
                    action_data={"extra_info_block": f'你在读取说说的时候出现了错误，错误原因：{feeds_list[0].get("error")}'}
                )

                if success and reply_set:
                    for reply_type, reply_content in reply_set:
                        if reply_type == "text":
                            await self.send_text(reply_content)
                return True

            # 处理说说列表
            if isinstance(feeds_list, list):
                logger.info(f"成功读取到{len(feeds_list)}条说说")
                
                for feed in feeds_list:
                    # 随机延迟
                    time.sleep(3 + random.random())
                    
                    # 处理说说内容
                    await self._process_single_feed(
                        feed, target_qq, target_name, 
                        like_possibility, comment_possibility, qzone_manager
                    )
                    
                return True
            else:
                return False
                
        except Exception as e:
            logger.error(f"读取并处理说说失败: {str(e)}")
            return False

    async def _process_single_feed(self, feed: dict, target_qq: str, target_name: str, 
                                 like_possibility: float, comment_possibility: float, 
                                 qzone_manager):
        """处理单条说说"""
        try:
            content = feed.get("content", "")
            images = feed.get("images", [])
            if images:
                for image in images:
                    content = content + str(image)
            fid = feed.get("tid", "")
            rt_con = feed.get("rt_con", "")
            
            # 随机评论
            if random.random() <= comment_possibility:
                comment = await self._generate_comment(content, rt_con, target_name)
                if comment:
                    success = await qzone_manager.comment_feed(
                        config_api.get_global_config("bot.qq_account", ""), 
                        target_qq, fid, comment
                    )
                    if success:
                        logger.info(f"发送评论'{comment}'成功")
                    else:
                        logger.error(f"评论说说'{content[:20]}...'失败")
            
            # 随机点赞
            if random.random() <= like_possibility:
                success = await qzone_manager.like_feed(
                    config_api.get_global_config("bot.qq_account", ""), 
                    target_qq, fid
                )
                if success:
                    logger.info(f"点赞说说'{content[:10]}..'成功")
                else:
                    logger.error(f"点赞说说'{content[:20]}...'失败")
                    
        except Exception as e:
            logger.error(f"处理单条说说失败: {str(e)}")

    async def _generate_comment(self, content: str, rt_con: str, target_name: str) -> str:
        """生成评论内容"""
        try:
            # 获取模型配置
            models = llm_api.get_available_models()
            text_model = str(self.get_config("models.text_model", "replyer_1"))
            model_config = models.get(text_model)
            
            if not model_config:
                return ""

            # 获取机器人信息
            bot_personality = config_api.get_global_config("personality.personality_core", "一个机器人")
            bot_expression = config_api.get_global_config("expression.expression_style", "内容积极向上")

            # 构建提示词
            if not rt_con:
                prompt = f"""
                你是'{bot_personality}'，你正在浏览你好友'{target_name}'的QQ空间，
                你看到了你的好友'{target_name}'qq空间上内容是'{content}'的说说，你想要发表你的一条评论，
                {bot_expression}，回复的平淡一些，简短一些，说中文，
                不要刻意突出自身学科背景，不要浮夸，不要夸张修辞，不要输出多余内容(包括前后缀，冒号和引号，括号()，表情包，at或 @等 )。只输出回复内容
                """
            else:
                prompt = f"""
                你是'{bot_personality}'，你正在浏览你好友'{target_name}'的QQ空间，
                你看到了你的好友'{target_name}'在qq空间上转发了一条内容为'{rt_con}'的说说，你的好友的评论为'{content}'
                你想要发表你的一条评论，{bot_expression}，回复的平淡一些，简短一些，说中文，
                不要刻意突出自身学科背景，不要浮夸，不要夸张修辞，不要输出多余内容(包括前后缀，冒号和引号，括号()，表情包，at或 @等 )。只输出回复内容
                """
            
            logger.info(f"正在评论'{target_name}'的说说：{content[:20]}...")
            
            # 生成评论
            success, comment, reasoning, model_name = await llm_api.generate_with_model(
                prompt=prompt,
                model_config=model_config,
                request_type="story.generate",
                temperature=0.3,
                max_tokens=1000
            )

            if success:
                logger.info(f"成功生成评论内容：'{comment}'")
                return comment
            else:
                logger.error("生成评论内容失败")
                return ""
                
        except Exception as e:
            logger.error(f"生成评论内容异常: {str(e)}")
            return ""


# ===== 插件主类 =====
@register_plugin
class MaiZonePlugin(BasePlugin):
    """MaiZone插件 - 让麦麦发QQ空间"""

    # 插件基本信息
    plugin_name: str = "MaiZonePlugin"
    enable_plugin: bool = True
    dependencies: List[str] = []
    python_dependencies: List[str] = []
    config_file_name: str = "config.toml"

    # 配置节描述
    config_section_descriptions = {
        "plugin": "插件基础配置",
        "models": "模型相关配置",
        "send": "发送说说配置",
        "read": "阅读说说配置",
        "monitor": "自动监控配置",
        "schedule": "定时发送配置",
    }

    # 配置模式定义
    config_schema: dict = {
        "plugin": {
            "enable": ConfigField(type=bool, default=True, description="是否启用插件"),
            "config_version": ConfigField(type=str, default="2.1.0", description="配置文件版本"),
        },
        "models": {
            "text_model": ConfigField(type=str, default="replyer_1", description="生成文本的模型名称"),
            "siliconflow_apikey": ConfigField(type=str, default="", description="硅基流动AI生图API密钥"),
        },
        "send": {
            "permission": ConfigField(type=list, default=['1145141919810'], description="发送权限QQ号列表"),
            "permission_type": ConfigField(type=str, default='whitelist', description="权限类型：whitelist(白名单) 或 blacklist(黑名单)"),
            "enable_image": ConfigField(type=bool, default=False, description="是否启用说说配图"),
            "enable_ai_image": ConfigField(type=bool, default=False, description="是否启用AI生成配图"),
            "enable_reply": ConfigField(type=bool, default=True, description="生成完成时是否发出回复"),
            "ai_image_number": ConfigField(type=int, default=1, description="AI生成图片数量(1-4张)"),
            "image_directory": ConfigField(type=str, default="./plugins/built_in/Maizone/images", description="图片存储目录")
        },
        "read": {
            "permission": ConfigField(type=list, default=[], description="阅读权限QQ号列表"),
            "permission_type": ConfigField(type=str, default='blacklist', description="权限类型：whitelist(白名单) 或 blacklist(黑名单)"),
            "read_number": ConfigField(type=int, default=5, description="一次读取的说说数量"),
            "like_possibility": ConfigField(type=float, default=1.0, description="点赞概率(0.0-1.0)"),
            "comment_possibility": ConfigField(type=float, default=0.3, description="评论概率(0.0-1.0)"),
        },
        "monitor": {
            "enable_auto_monitor": ConfigField(type=bool, default=False, description="是否启用自动监控好友说说"),
            "interval_minutes": ConfigField(type=int, default=10, description="监控间隔时间(分钟)"),
        },
        "schedule": {
            "enable_schedule": ConfigField(type=bool, default=False, description="是否启用定时发送说说"),
            "schedules": ConfigField(
                type=str,
                default=r"""{"08:00" = "早安","22:00" = "晚安"}""",
                description="定时发送任务列表, 格式为 {\"时间\"= \"主题\"}"
            ),
        },
    }

    def __init__(self, *args, **kwargs):
        """初始化插件"""
        super().__init__(*args, **kwargs)

        # 设置插件信息
        self.plugin_name = "MaiZone"
        self.plugin_description = "让麦麦实现QQ空间点赞、评论、发说说功能"
        self.plugin_version = "2.0.0"
        self.plugin_author = "重构版"
        self.config_file_name = "config.toml"

        # 初始化独立配置加载器
        plugin_dir = self.plugin_dir
        if plugin_dir is None:
            plugin_dir = os.path.dirname(__file__)
        self.config_loader = MaiZoneConfigLoader(plugin_dir, self.config_file_name)
        
        # 加载配置
        if not self.config_loader.load_config():
            logger.error("配置加载失败，使用默认设置")
        
        # 获取启用状态
        self.enable_plugin = self.config_loader.get_config("plugin.enable", True)
        
        # 初始化管理器
        self.monitor_manager = None
        self.schedule_manager = None
        
        # 根据配置启动功能
        if self.enable_plugin:
            self._init_managers()

    def _init_managers(self):
        """初始化管理器"""
        try:
            # 初始化监控管理器
            if self.config_loader.get_config("monitor.enable_auto_monitor", False):
                from .monitor import MonitorManager
                self.monitor_manager = MonitorManager(self)
                asyncio.create_task(self._start_monitor_delayed())

            # 初始化定时管理器
            if self.config_loader.get_config("schedule.enable_schedule", False):
                logger.info("定时任务启用状态: true")
                self.schedule_manager = ScheduleManager(self)
                asyncio.create_task(self._start_scheduler_delayed())
                
        except Exception as e:
            logger.error(f"初始化管理器失败: {str(e)}")

    async def _start_monitor_delayed(self):
        """延迟启动监控管理器"""
        try:
            await asyncio.sleep(10)  # 等待插件完全初始化
            if self.monitor_manager:
                await self.monitor_manager.start()
        except Exception as e:
            logger.error(f"启动监控管理器失败: {str(e)}")

    async def _start_scheduler_delayed(self):
        """延迟启动定时管理器"""
        try:
            await asyncio.sleep(10)  # 等待插件完全初始化
            if self.schedule_manager:
                await self.schedule_manager.start()
        except Exception as e:
            logger.error(f"启动定时管理器失败: {str(e)}")

    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        """获取插件组件列表"""
        return [
            (SendFeedAction.get_action_info(), SendFeedAction),
            (ReadFeedAction.get_action_info(), ReadFeedAction),
            (SendFeedCommand.get_command_info(), SendFeedCommand)
        ]
