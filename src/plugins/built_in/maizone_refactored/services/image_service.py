"""
图片服务模块
负责处理所有与图片相关的任务，特别是AI生成图片。
"""

import base64
import random
from collections.abc import Callable
from pathlib import Path
from io import BytesIO
from PIL import Image

import aiofiles
import aiohttp

from src.common.logger import get_logger
from src.plugin_system.apis import llm_api, config_api

logger = get_logger("MaiZone.ImageService")


class ImageService:
    """
    图片服务类，封装了生成和管理图片的所有逻辑。
    """

    def __init__(self, get_config: Callable):
        """
        初始化图片服务。

        :param get_config: 一个函数，用于从插件主类获取配置信息。
        """
        self.get_config = get_config

    async def generate_images_for_story(self, story: str) -> bool:
        """
        根据说说内容，判断是否需要生成AI配图，并执行生成任务。

        :param story: 说说内容。
        :return: 图片是否成功生成（或不需要生成）。
        """
        try:
            enable_ai_image = bool(self.get_config("send.enable_ai_image", False))
            api_key = str(self.get_config("models.siliconflow_apikey", ""))
            image_dir = str(self.get_config("send.image_directory", "./data/plugins/maizone_refactored/images"))
            image_num_raw = self.get_config("send.ai_image_number", 1)
            
            # 安全地处理图片数量配置，并限制在API允许的范围内
            try:
                image_num = int(image_num_raw) if image_num_raw not in [None, ""] else 1
                image_num = max(1, min(image_num, 4))  # SiliconFlow API限制：1 <= batch_size <= 4
            except (ValueError, TypeError):
                logger.warning(f"无效的图片数量配置: {image_num_raw}，使用默认值1")
                image_num = 1

            if not enable_ai_image:
                return True  # 未启用AI配图，视为成功

            if not api_key:
                logger.error("启用了AI配图但未填写SiliconFlow API密钥")
                return False

            # 确保图片目录存在
            Path(image_dir).mkdir(parents=True, exist_ok=True)

            # 生成图片提示词
            image_prompt = await self._generate_image_prompt(story)
            if not image_prompt:
                logger.error("生成图片提示词失败")
                return False

            logger.info(f"正在为说说生成 {image_num} 张AI配图...")
            return await self._call_siliconflow_api(api_key, image_prompt, image_dir, image_num)

        except Exception as e:
            logger.error(f"处理AI配图时发生异常: {e}")
            return False

    async def _generate_image_prompt(self, story_content: str) -> str:
        """
        使用LLM生成图片提示词，基于说说内容。
        
        :param story_content: 说说内容
        :return: 生成的图片提示词，失败时返回空字符串
        """
        try:
            # 获取配置
            identity = config_api.get_global_config("personality.identity", "年龄为19岁,是女孩子,身高为160cm,黑色短发")
            enable_ref = bool(self.get_config("models.image_ref", True))
            
            # 构建提示词
            prompt = f"""
                请根据以下QQ空间说说内容配图，并构建生成配图的风格和prompt。
                说说主人信息：'{identity}'。
                说说内容:'{story_content}'。
                请注意：仅回复用于生成图片的prompt，不要输出多余内容(包括前后缀，冒号和引号，括号()，表情包，at或 @等 )。
                """
            if enable_ref:
                prompt += "说说主人的人设参考图片将随同提示词一起发送给生图AI，可使用'in the style of'或'根据图中人物'等描述引导生成风格"

            # 获取模型配置
            models = llm_api.get_available_models()
            prompt_model = self.get_config("models.text_model", "replyer")
            model_config = models.get(prompt_model)
            
            if not model_config:
                logger.error(f"找不到模型配置: {prompt_model}")
                return ""

            # 调用LLM生成提示词
            logger.info("正在生成图片提示词...")
            success, image_prompt, reasoning, model_name = await llm_api.generate_with_model(
                prompt=prompt,
                model_config=model_config,
                request_type="story.generate",
                temperature=0.3,
                max_tokens=1000
            )

            if success:
                logger.info(f'成功生成图片提示词: {image_prompt}')
                return image_prompt
            else:
                logger.error('生成图片提示词失败')
                return ""

        except Exception as e:
            logger.error(f"生成图片提示词时发生异常: {e}")
            return ""

    async def _call_siliconflow_api(self, api_key: str, image_prompt: str, image_dir: str, batch_size: int) -> bool:
        """
        调用硅基流动（SiliconFlow）的API来生成图片。

        :param api_key: SiliconFlow API密钥。
        :param image_prompt: 用于生成图片的提示词。
        :param image_dir: 图片保存目录。
        :param batch_size: 生成图片的数量（1-4）。
        :return: API调用是否成功。
        """
        url = "https://api.siliconflow.cn/v1/images/generations"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        data = {
            "model": "Kwai-Kolors/Kolors",
            "prompt": image_prompt,
            "negative_prompt": "lowres, bad anatomy, bad hands, text, error, cropped, worst quality, low quality, "
                               "normal quality, jpeg artifacts, signature, watermark, username, blurry",
            "seed": random.randint(1, 9999999999),
            "batch_size": batch_size,
        }

        # 检查是否启用参考图片
        enable_ref = bool(self.get_config("models.image_ref", True))
        if enable_ref:
            # 修复：使用Path对象正确获取父目录
            parent_dir = Path(image_dir).parent
            ref_images = list(parent_dir.glob("done_ref.*"))
            if ref_images:
                try:
                    image = Image.open(ref_images[0])
                    encoded_image = self._encode_image_to_base64(image)
                    if encoded_image:  # 只有在编码成功时才添加
                        data["image"] = encoded_image
                        logger.info("已添加参考图片到生成参数")
                except Exception as e:
                    logger.warning(f"加载参考图片失败: {e}")

        try:
            async with aiohttp.ClientSession() as session:
                # 发送生成请求
                async with session.post(url, json=data, headers=headers) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        logger.error(f'生成图片出错，错误码[{response.status}]')
                        logger.error(f'错误响应: {error_text}')
                        return False
                    
                    json_data = await response.json()
                    image_urls = [img["url"] for img in json_data["images"]]

                    success_count = 0
                    # 下载并保存图片
                    for i, img_url in enumerate(image_urls):
                        try:
                            # 下载图片
                            async with session.get(img_url) as img_response:
                                img_response.raise_for_status()
                                img_data = await img_response.read()

                            # 处理图片
                            try:
                                image = Image.open(BytesIO(img_data))
                                
                                # 保存图片为PNG格式（确保兼容性）
                                filename = f"image_{i}.png"
                                save_path = Path(image_dir) / filename
                                
                                # 转换为RGB模式如果必要（避免RGBA等模式的问题）
                                if image.mode in ('RGBA', 'LA', 'P'):
                                    background = Image.new('RGB', image.size, (255, 255, 255))
                                    background.paste(image, mask=image.split()[-1] if image.mode == 'RGBA' else None)
                                    image = background
                                
                                image.save(save_path, format='PNG')
                                logger.info(f"图片已保存至: {save_path}")
                                success_count += 1
                                
                            except Exception as e:
                                logger.error(f"处理图片失败: {str(e)}")
                                continue

                        except Exception as e:
                            logger.error(f"下载第{i+1}张图片失败: {str(e)}")
                            continue

                    # 只要至少有一张图片成功就返回True
                    return success_count > 0

        except Exception as e:
            logger.error(f"调用AI生图API时发生异常: {e}")
            return False

    def _encode_image_to_base64(self, img: Image.Image) -> str:
        """
        将PIL.Image对象编码为base64 data URL
        
        :param img: PIL图片对象
        :return: base64 data URL字符串，失败时返回空字符串
        """
        try:
            # 强制转换为PNG格式，因为SiliconFlow API要求data:image/png
            buffer = BytesIO()
            
            # 转换为RGB模式如果必要
            if img.mode in ('RGBA', 'LA', 'P'):
                background = Image.new('RGB', img.size, (255, 255, 255))
                background.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
                img = background
            
            # 保存为PNG格式
            img.save(buffer, format="PNG")
            byte_data = buffer.getvalue()
            
            # Base64编码，使用固定的data:image/png
            encoded_string = base64.b64encode(byte_data).decode("utf-8")
            return f"data:image/png;base64,{encoded_string}"
            
        except Exception as e:
            logger.error(f"编码图片为base64失败: {e}")
            return ""