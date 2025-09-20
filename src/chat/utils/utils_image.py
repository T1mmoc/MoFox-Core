import base64
import os
import time
import hashlib
import uuid
import io
import numpy as np

from typing import Optional, Tuple, Dict, Any
from PIL import Image
from rich.traceback import install

from src.common.logger import get_logger
from src.common.database.sqlalchemy_models import Images, ImageDescriptions
from src.config.config import global_config, model_config
from src.llm_models.utils_model import LLMRequest
from src.common.database.sqlalchemy_models import get_db_session

from sqlalchemy import select, and_

install(extra_lines=3)

logger = get_logger("chat_image")


def is_image_message(message: Dict[str, Any]) -> bool:
    """
    判断消息是否为图片消息

    Args:
        message: 消息字典

    Returns:
        bool: 是否为图片消息
    """
    return message.get("type") == "image" or (
        isinstance(message.get("content"), dict) and message["content"].get("type") == "image"
    )


class ImageManager:
    _instance = None
    IMAGE_DIR = "data"  # 图像存储根目录

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if not self._initialized:
            self._ensure_image_dir()

            self._initialized = True
            self.vlm = LLMRequest(model_set=model_config.model_task_config.vlm, request_type="image")

            # try:
            #     db.connect(reuse_if_open=True)
            #     # 使用SQLAlchemy创建表已在初始化时完成
            #     logger.debug("使用SQLAlchemy进行表管理")
            # except Exception as e:
            #     logger.error(f"数据库连接失败: {e}")

            self._initialized = True

    def _ensure_image_dir(self):
        """确保图像存储目录存在"""
        os.makedirs(self.IMAGE_DIR, exist_ok=True)

    @staticmethod
    async def _get_description_from_db(image_hash: str, description_type: str) -> Optional[str]:
        """从数据库获取图片描述

        Args:
            image_hash: 图片哈希值
            description_type: 描述类型 ('emoji' 或 'image')

        Returns:
            Optional[str]: 描述文本，如果不存在则返回None
        """
        try:
            async with get_db_session() as session:
                record = (await session.execute(
                    select(ImageDescriptions).where(
                        and_(
                            ImageDescriptions.image_description_hash == image_hash,
                            ImageDescriptions.type == description_type,
                        )
                    )
                )).scalar()
                return record.description if record else None
        except Exception as e:
            logger.error(f"从数据库获取描述失败 (SQLAlchemy): {str(e)}")
            return None

    @staticmethod
    async def _save_description_to_db(image_hash: str, description: str, description_type: str) -> None:
        """保存图片描述到数据库

        Args:
            image_hash: 图片哈希值
            description: 描述文本
            description_type: 描述类型 ('emoji' 或 'image')
        """
        try:
            current_timestamp = time.time()
            async with get_db_session() as session:
                # 查找现有记录
                existing = (await session.execute(
                    select(ImageDescriptions).where(
                        and_(
                            ImageDescriptions.image_description_hash == image_hash,
                            ImageDescriptions.type == description_type,
                        )
                    )
                )).scalar()

                if existing:
                    # 更新现有记录
                    existing.description = description
                    existing.timestamp = current_timestamp
                else:
                    # 创建新记录
                    new_desc = ImageDescriptions(
                        image_description_hash=image_hash,
                        type=description_type,
                        description=description,
                        timestamp=current_timestamp,
                    )
                    await session.add(new_desc)
                await session.commit()
                #  会在上下文管理器中自动调用
        except Exception as e:
            logger.error(f"保存描述到数据库失败 (SQLAlchemy): {str(e)}")

    @staticmethod
    async def get_emoji_tag(image_base64: str) -> str:
        from src.chat.emoji_system.emoji_manager import get_emoji_manager

        emoji_manager = get_emoji_manager()
        if isinstance(image_base64, str):
            image_base64 = image_base64.encode("ascii", errors="ignore").decode("ascii")
        image_bytes = base64.b64decode(image_base64)
        image_hash = hashlib.md5(image_bytes).hexdigest()
        emoji = await emoji_manager.get_emoji_from_manager(image_hash)
        if not emoji:
            return "[表情包：未知]"
        emotion_list = emoji.emotion
        tag_str = ",".join(emotion_list)
        return f"[表情包：{tag_str}]"

    async def get_emoji_description(self, image_base64: str) -> str:
        """获取表情包描述，优先使用Emoji表中的缓存数据"""
        try:
            # 计算图片哈希
            # 确保base64字符串只包含ASCII字符
            if isinstance(image_base64, str):
                image_base64 = image_base64.encode("ascii", errors="ignore").decode("ascii")
            image_bytes = base64.b64decode(image_base64)
            image_hash = hashlib.md5(image_bytes).hexdigest()
            image_format = Image.open(io.BytesIO(image_bytes)).format.lower()  # type: ignore

            # 优先使用EmojiManager查询已注册表情包的描述
            try:
                from src.chat.emoji_system.emoji_manager import get_emoji_manager

                emoji_manager = get_emoji_manager()
                tags = await emoji_manager.get_emoji_tag_by_hash(image_hash)
                if tags:
                    tag_str = ",".join(tags)
                    logger.info(f"[缓存命中] 使用已注册表情包描述: {tag_str}...")
                    return f"[表情包：{tag_str}]"
            except Exception as e:
                logger.debug(f"查询EmojiManager时出错: {e}")

            # 查询ImageDescriptions表的缓存描述
            if cached_description := await self._get_description_from_db(image_hash, "emoji"):
                logger.info(f"[缓存命中] 使用ImageDescriptions表中的描述: {cached_description}...")
                return f"[表情包：{cached_description}]"

            # === 二步走识别流程 ===

            # 第一步：VLM视觉分析 - 生成详细描述
            if image_format in ["gif", "GIF"]:
                image_base64_processed = self.transform_gif(image_base64)
                if image_base64_processed is None:
                    logger.warning("GIF转换失败，无法获取描述")
                    return "[表情包(GIF处理失败)]"
                vlm_prompt = "这是一个动态图表情包，每一张图代表了动态图的某一帧，黑色背景代表透明，描述一下表情包表达的情感和内容，描述细节，从互联网梗,meme的角度去分析"
                detailed_description, _ = await self.vlm.generate_response_for_image(
                    vlm_prompt, image_base64_processed, "jpeg", temperature=0.4, max_tokens=300
                )
            else:
                vlm_prompt = (
                    "这是一个表情包，请详细描述一下表情包所表达的情感和内容，描述细节，从互联网梗,meme的角度去分析"
                )
                detailed_description, _ = await self.vlm.generate_response_for_image(
                    vlm_prompt, image_base64, image_format, temperature=0.4, max_tokens=300
                )

            if detailed_description is None:
                logger.warning("VLM未能生成表情包详细描述")
                return "[表情包(VLM描述生成失败)]"

            # 第二步：LLM情感分析 - 基于详细描述生成简短的情感标签
            emotion_prompt = f"""
            请你基于这个表情包的详细描述，提取出最核心的情感含义，用1-2个词概括。
            详细描述：'{detailed_description}'
            
            要求：
            1. 只输出1-2个最核心的情感词汇
            2. 从互联网梗、meme的角度理解
            3. 输出简短精准，不要解释
            4. 如果有多个词用逗号分隔
            """

            # 使用较低温度确保输出稳定
            emotion_llm = LLMRequest(model_set=model_config.model_task_config.utils, request_type="emoji")
            emotion_result, _ = await emotion_llm.generate_response_async(
                emotion_prompt, temperature=0.3, max_tokens=50
            )

            if emotion_result is None:
                logger.warning("LLM未能生成情感标签，使用详细描述的前几个词")
                # 降级处理：从详细描述中提取关键词
                import jieba

                words = list(jieba.cut(detailed_description))
                emotion_result = "，".join(words[:2]) if len(words) >= 2 else (words[0] if words else "表情")

            # 处理情感结果，取前1-2个最重要的标签
            emotions = [e.strip() for e in emotion_result.replace("，", ",").split(",") if e.strip()]
            final_emotion = emotions[0] if emotions else "表情"

            # 如果有第二个情感且不重复，也包含进来
            if len(emotions) > 1 and emotions[1] != emotions[0]:
                final_emotion = f"{emotions[0]}，{emotions[1]}"

            logger.info(f"[emoji识别] 详细描述: {detailed_description}... -> 情感标签: {final_emotion}")

            if cached_description := await self._get_description_from_db(image_hash, "emoji"):
                logger.warning(f"虽然生成了描述，但是找到缓存表情包描述: {cached_description}")
                return f"[表情包：{cached_description}]"

            # 只有在开启“偷表情包”功能时，才将接收到的表情包保存到待注册目录
            if global_config.emoji.steal_emoji:
                logger.debug(f"偷取表情包功能已开启，保存表情包: {image_hash}")
                current_timestamp = time.time()
                filename = f"{int(current_timestamp)}_{image_hash[:8]}.{image_format}"
                emoji_dir = os.path.join(self.IMAGE_DIR, "emoji")
                os.makedirs(emoji_dir, exist_ok=True)
                file_path = os.path.join(emoji_dir, filename)

                try:
                    # 保存文件
                    with open(file_path, "wb") as f:
                        f.write(image_bytes)

                    # 保存到数据库 (Images表) - 包含详细描述用于可能的注册流程
                    try:
                        from src.common.database.sqlalchemy_models import get_db_session

                        async with get_db_session() as session:
                            existing_img = (await session.execute(
                                select(Images).where(and_(Images.emoji_hash == image_hash, Images.type == "emoji"))
                            )).scalar()

                            if existing_img:
                                existing_img.path = file_path
                                existing_img.description = detailed_description  # 保存详细描述
                                existing_img.timestamp = current_timestamp
                            else:
                                new_img = Images(
                                    emoji_hash=image_hash,
                                    path=file_path,
                                    type="emoji",
                                    description=detailed_description,  # 保存详细描述
                                    timestamp=current_timestamp,
                                )
                                await session.add(new_img)
                            await session.commit()
                    except Exception as e:
                        logger.error(f"保存到Images表失败: {str(e)}")

                except Exception as e:
                    logger.error(f"保存表情包文件或元数据失败: {str(e)}")
            else:
                logger.debug("偷取表情包功能已关闭，跳过保存。")

            # 保存最终的情感标签到缓存 (ImageDescriptions表)
            await self._save_description_to_db(image_hash, final_emotion, "emoji")

            return f"[表情包：{final_emotion}]"

        except Exception as e:
            logger.error(f"获取表情包描述失败: {str(e)}")
            return "[表情包(处理失败)]"

    async def get_image_description(self, image_base64: str) -> str:
        """获取普通图片描述，优先使用Images表中的缓存数据"""
        try:
            # 计算图片哈希
            if isinstance(image_base64, str):
                image_base64 = image_base64.encode("ascii", errors="ignore").decode("ascii")
            image_bytes = base64.b64decode(image_base64)
            image_hash = hashlib.md5(image_bytes).hexdigest()

            async with get_db_session() as session:
                # 优先检查Images表中是否已有完整的描述
                existing_image = (await session.execute(select(Images).where(Images.emoji_hash == image_hash))).scalar()
                if existing_image:
                    # 更新计数
                    if hasattr(existing_image, "count") and existing_image.count is not None:
                        existing_image.count += 1
                    else:
                        existing_image.count = 1

                    # 如果已有描述，直接返回
                    if existing_image.description:
                        await session.commit()
                        logger.debug(f"[缓存命中] 使用Images表中的图片描述: {existing_image.description}...")
                        return f"[图片：{existing_image.description}]"

                # 如果没有描述，继续在当前会话中操作
                if cached_description := await self._get_description_from_db(image_hash, "image"):
                    logger.debug(f"[缓存命中] 使用ImageDescriptions表中的描述: {cached_description}...")
                    return f"[图片：{cached_description}]"

                # 调用AI获取描述
                image_format = Image.open(io.BytesIO(image_bytes)).format.lower()  # type: ignore
                prompt = global_config.custom_prompt.image_prompt
                logger.info(f"[VLM调用] 为图片生成新描述 (Hash: {image_hash[:8]}...)")
                description, _ = await self.vlm.generate_response_for_image(
                    prompt, image_base64, image_format, temperature=0.4, max_tokens=300
                )

                if description is None:
                    logger.warning("AI未能生成图片描述")
                    return "[图片(描述生成失败)]"

                # 保存图片和描述
                current_timestamp = time.time()
                filename = f"{int(current_timestamp)}_{image_hash[:8]}.{image_format}"
                image_dir = os.path.join(self.IMAGE_DIR, "image")
                os.makedirs(image_dir, exist_ok=True)
                file_path = os.path.join(image_dir, filename)

                with open(file_path, "wb") as f:
                    f.write(image_bytes)

                # 保存到数据库，补充缺失字段
                if existing_image:
                    existing_image.path = file_path
                    existing_image.description = description
                    existing_image.timestamp = current_timestamp
                    if not hasattr(existing_image, "image_id") or not existing_image.image_id:
                        existing_image.image_id = str(uuid.uuid4())
                    if not hasattr(existing_image, "vlm_processed") or existing_image.vlm_processed is None:
                        existing_image.vlm_processed = True
                    logger.debug(f"[数据库] 更新已有图片记录: {image_hash[:8]}...")
                else:
                    new_img = Images(
                        image_id=str(uuid.uuid4()),
                        emoji_hash=image_hash,
                        path=file_path,
                        type="image",
                        description=description,
                        timestamp=current_timestamp,
                        vlm_processed=True,
                        count=1,
                    )
                    await session.add(new_img)
                    logger.debug(f"[数据库] 创建新图片记录: {image_hash[:8]}...")

                await session.commit()

                # 保存描述到ImageDescriptions表作为备用缓存
                await self._save_description_to_db(image_hash, description, "image")

                logger.info(f"[VLM完成] 图片描述生成: {description}...")
                return f"[图片：{description}]"

            logger.info(f"[VLM完成] 图片描述生成: {description}...")
            return f"[图片：{description}]"
        except Exception as e:
            logger.error(f"获取图片描述失败: {str(e)}")
            return "[图片(处理失败)]"

    @staticmethod
    def transform_gif(gif_base64: str, similarity_threshold: float = 1000.0, max_frames: int = 15) -> Optional[str]:
        # sourcery skip: use-contextlib-suppress
        """将GIF转换为水平拼接的静态图像, 跳过相似的帧

        Args:
            gif_base64: GIF的base64编码字符串
            similarity_threshold: 判定帧相似的阈值 (MSE)，越小表示要求差异越大才算不同帧，默认1000.0
            max_frames: 最大抽取的帧数，默认15

        Returns:
            Optional[str]: 拼接后的JPG图像的base64编码字符串, 或者在失败时返回None
        """
        try:
            # 确保base64字符串只包含ASCII字符
            if isinstance(gif_base64, str):
                gif_base64 = gif_base64.encode("ascii", errors="ignore").decode("ascii")
            # 解码base64
            gif_data = base64.b64decode(gif_base64)
            gif = Image.open(io.BytesIO(gif_data))

            # 收集所有帧
            all_frames = []
            try:
                while True:
                    gif.seek(len(all_frames))
                    # 确保是RGB格式方便比较
                    frame = gif.convert("RGB")
                    all_frames.append(frame.copy())
            except EOFError:
                ...  # 读完啦

            if not all_frames:
                logger.warning("GIF中没有找到任何帧")
                return None  # 空的GIF直接返回None

            # --- 新的帧选择逻辑 ---
            selected_frames = []
            last_selected_frame_np = None

            for i, current_frame in enumerate(all_frames):
                current_frame_np = np.array(current_frame)

                # 第一帧总是要选的
                if i == 0:
                    selected_frames.append(current_frame)
                    last_selected_frame_np = current_frame_np
                    continue

                # 计算和上一张选中帧的差异（均方误差 MSE）
                if last_selected_frame_np is not None:
                    mse = np.mean((current_frame_np - last_selected_frame_np) ** 2)
                    # logger.debug(f"帧 {i} 与上一选中帧的 MSE: {mse}") # 可以取消注释来看差异值

                    # 如果差异够大，就选它！
                    if mse > similarity_threshold:
                        selected_frames.append(current_frame)
                        last_selected_frame_np = current_frame_np
                        # 检查是不是选够了
                        if len(selected_frames) >= max_frames:
                            # logger.debug(f"已选够 {max_frames} 帧，停止选择。")
                            break
                # 如果差异不大就跳过这一帧啦

            # --- 帧选择逻辑结束 ---

            # 如果选择后连一帧都没有（比如GIF只有一帧且后续处理失败？）或者原始GIF就没帧，也返回None
            if not selected_frames:
                logger.warning("处理后没有选中任何帧")
                return None

            # logger.debug(f"总帧数: {len(all_frames)}, 选中帧数: {len(selected_frames)}")

            # 获取选中的第一帧的尺寸（假设所有帧尺寸一致）
            frame_width, frame_height = selected_frames[0].size

            # 计算目标尺寸，保持宽高比
            target_height = 200  # 固定高度
            # 防止除以零
            if frame_height == 0:
                logger.error("帧高度为0，无法计算缩放尺寸")
                return None
            target_width = int((target_height / frame_height) * frame_width)
            # 宽度也不能是0
            if target_width == 0:
                logger.warning(f"计算出的目标宽度为0 (原始尺寸 {frame_width}x{frame_height})，调整为1")
                target_width = 1

            # 调整所有选中帧的大小
            resized_frames = [
                frame.resize((target_width, target_height), Image.Resampling.LANCZOS) for frame in selected_frames
            ]

            # 创建拼接图像
            total_width = target_width * len(resized_frames)
            # 防止总宽度为0
            if total_width == 0 and resized_frames:
                logger.warning("计算出的总宽度为0，但有选中帧，可能目标宽度太小")
                # 至少给点宽度吧
                total_width = len(resized_frames)
            elif total_width == 0:
                logger.error("计算出的总宽度为0且无选中帧")
                return None

            combined_image = Image.new("RGB", (total_width, target_height))

            # 水平拼接图像
            for idx, frame in enumerate(resized_frames):
                combined_image.paste(frame, (idx * target_width, 0))

            # 转换为base64
            buffer = io.BytesIO()
            combined_image.save(buffer, format="JPEG", quality=85)  # 保存为JPEG
            return base64.b64encode(buffer.getvalue()).decode("utf-8")
        except MemoryError:
            logger.error("GIF转换失败: 内存不足，可能是GIF太大或帧数太多")
            return None  # 内存不够啦
        except Exception as e:
            logger.error(f"GIF转换失败: {str(e)}", exc_info=True)  # 记录详细错误信息
            return None  # 其他错误也返回None

    async def process_image(self, image_base64: str) -> Tuple[str, str]:
        # sourcery skip: hoist-if-from-if
        """处理图片并返回图片ID和描述

        Args:
            image_base64: 图片的base64编码

        Returns:
            Tuple[str, str]: (图片ID, 描述)
        """
        try:
            # 生成图片ID
            # 计算图片哈希
            # 确保base64字符串只包含ASCII字符
            if isinstance(image_base64, str):
                image_base64 = image_base64.encode("ascii", errors="ignore").decode("ascii")
            image_bytes = base64.b64decode(image_base64)
            image_hash = hashlib.md5(image_bytes).hexdigest()
            async with get_db_session() as session:
                existing_image = (await session.execute(select(Images).where(Images.emoji_hash == image_hash))).scalar()
                if existing_image:
                    # 检查是否缺少必要字段，如果缺少则创建新记录
                    if (
                        not hasattr(existing_image, "image_id")
                        or not existing_image.image_id
                        or not hasattr(existing_image, "count")
                        or existing_image.count is None
                        or not hasattr(existing_image, "vlm_processed")
                        or existing_image.vlm_processed is None
                    ):
                        logger.debug(f"图片记录缺少必要字段，补全旧记录: {image_hash}")
                        if not existing_image.image_id:
                            existing_image.image_id = str(uuid.uuid4())
                        if existing_image.count is None:
                            existing_image.count = 0
                        if existing_image.vlm_processed is None:
                            existing_image.vlm_processed = False

                    existing_image.count += 1
                    await session.commit()

                    # 如果已有描述，直接返回
                    if existing_image.description and existing_image.description.strip():
                        return existing_image.image_id, f"[picid:{existing_image.image_id}]"
                    else:
                        # 同步处理图片描述
                        description = await self.get_image_description(image_base64)
                        # 更新数据库中的描述
                        existing_image.description = description.replace("[图片：", "").replace("]", "")
                        existing_image.vlm_processed = True
                        await session.commit()
                        return existing_image.image_id, f"[picid:{existing_image.image_id}]"

                # print(f"图片不存在: {image_hash}")
                image_id = str(uuid.uuid4())

                # 同步获取图片描述
                description = await self.get_image_description(image_base64)
                clean_description = description.replace("[图片：", "").replace("]", "")

                # 保存新图片
                current_timestamp = time.time()
                image_dir = os.path.join(self.IMAGE_DIR, "images")
                os.makedirs(image_dir, exist_ok=True)
                filename = f"{image_id}.png"
                file_path = os.path.join(image_dir, filename)

                # 保存文件
                with open(file_path, "wb") as f:
                    f.write(image_bytes)

                # 保存到数据库
                new_img = Images(
                    image_id=image_id,
                    emoji_hash=image_hash,
                    path=file_path,
                    type="image",
                    description=clean_description,
                    timestamp=current_timestamp,
                    vlm_processed=True,
                    count=1,
                )
                await session.add(new_img)
                await session.commit()

            return image_id, f"[picid:{image_id}]"

        except Exception as e:
            logger.error(f"处理图片失败: {str(e)}")
            return "", "[图片]"


# 创建全局单例
image_manager = None


def get_image_manager() -> ImageManager:
    """获取全局图片管理器单例"""
    global image_manager
    if image_manager is None:
        image_manager = ImageManager()
    return image_manager


def image_path_to_base64(image_path: str) -> str:
    """将图片路径转换为base64编码
    Args:
        image_path: 图片文件路径
    Returns:
        str: base64编码的图片数据
    Raises:
        FileNotFoundError: 当图片文件不存在时
        IOError: 当读取图片文件失败时
    """
    if not os.path.exists(image_path):
        raise FileNotFoundError(f"图片文件不存在: {image_path}")

    with open(image_path, "rb") as f:
        if image_data := f.read():
            return base64.b64encode(image_data).decode("utf-8")
        else:
            raise IOError(f"读取图片文件失败: {image_path}")
