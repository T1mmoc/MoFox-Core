"""
KFC 响应后处理器

实现与全局后处理流程的集成：
- 中文错别字生成（typo_generator）
- 消息分割（punctuation/llm模式）

设计理念：复用全局配置和AFC的核心分割逻辑，与AFC保持一致的后处理行为。
"""

import re
from typing import Any, Optional, TYPE_CHECKING

from src.common.logger import get_logger
from src.config.config import global_config

if TYPE_CHECKING:
    from src.chat.utils.typo_generator import ChineseTypoGenerator

logger = get_logger("kokoro_post_processor")

# 延迟导入错别字生成器（避免循环导入和启动时的额外开销）
_typo_generator: Optional["ChineseTypoGenerator"] = None


def _get_typo_generator():
    """延迟加载错别字生成器"""
    global _typo_generator
    if _typo_generator is None:
        try:
            from src.chat.utils.typo_generator import ChineseTypoGenerator
            
            if global_config is None:
                logger.warning("[KFC PostProcessor] global_config 未初始化")
                return None
            
            # 从全局配置读取参数
            typo_cfg = global_config.chinese_typo
            _typo_generator = ChineseTypoGenerator(
                error_rate=typo_cfg.error_rate,
                min_freq=typo_cfg.min_freq,
                tone_error_rate=typo_cfg.tone_error_rate,
                word_replace_rate=typo_cfg.word_replace_rate,
            )
            logger.info("[KFC PostProcessor] 错别字生成器已初始化")
        except Exception as e:
            logger.warning(f"[KFC PostProcessor] 初始化错别字生成器失败: {e}")
            _typo_generator = None
    return _typo_generator


def split_by_punctuation(text: str, max_length: int = 256, max_sentences: int = 8) -> list[str]:
    """
    基于标点符号分割消息 - 复用AFC的核心逻辑
    
    V6修复: 不再依赖长度判断，而是直接调用AFC的分割函数
    
    Args:
        text: 原始文本
        max_length: 单条消息最大长度（用于二次合并过长片段）
        max_sentences: 最大句子数
        
    Returns:
        list[str]: 分割后的消息列表
    """
    if not text:
        return []
    
    # 直接复用AFC的核心分割逻辑
    from src.chat.utils.utils import split_into_sentences_w_remove_punctuation
    
    # AFC的分割函数会根据标点分割并概率性合并
    sentences = split_into_sentences_w_remove_punctuation(text)
    
    if not sentences:
        return [text] if text else []
    
    # 限制句子数量
    if len(sentences) > max_sentences:
        sentences = sentences[:max_sentences]
    
    # 如果某个片段超长，进行二次切分
    result = []
    for sentence in sentences:
        if len(sentence) > max_length:
            # 超长片段按max_length硬切分
            for i in range(0, len(sentence), max_length):
                chunk = sentence[i:i + max_length]
                if chunk.strip():
                    result.append(chunk.strip())
        else:
            if sentence.strip():
                result.append(sentence.strip())
    
    return result if result else [text]


async def process_reply_content(content: str) -> list[str]:
    """
    处理回复内容（主入口）
    
    遵循全局配置：
    - [response_post_process].enable_response_post_process
    - [chinese_typo].enable
    - [response_splitter].enable 和 .split_mode
    
    Args:
        content: 原始回复内容
        
    Returns:
        list[str]: 处理后的消息列表（可能被分割成多条）
    """
    if not content:
        return []
    
    if global_config is None:
        logger.warning("[KFC PostProcessor] global_config 未初始化，返回原始内容")
        return [content]
    
    # 检查全局开关
    post_process_cfg = global_config.response_post_process
    if not post_process_cfg.enable_response_post_process:
        logger.info("[KFC PostProcessor] 全局后处理已禁用，返回原始内容")
        return [content]
    
    processed_content = content
    
    # Step 1: 错别字生成
    typo_cfg = global_config.chinese_typo
    if typo_cfg.enable:
        try:
            typo_gen = _get_typo_generator()
            if typo_gen:
                processed_content, correction_suggestion = typo_gen.create_typo_sentence(content)
                if correction_suggestion:
                    logger.info(f"[KFC PostProcessor] 生成错别字，建议纠正: {correction_suggestion}")
                else:
                    logger.info("[KFC PostProcessor] 已应用错别字生成")
        except Exception as e:
            logger.warning(f"[KFC PostProcessor] 错别字生成失败: {e}")
            # 失败时使用原内容
            processed_content = content
    
    # Step 2: 消息分割 - 已禁用
    # KFC 的 LLM 会自己通过多个 reply 动作来分割消息，
    # 后处理器不再进行二次分割，避免破坏 LLM 的自然分割决策。
    # 
    # 参考提示词中的指导：
    # - LLM 被引导在合适的语气词、标点处自然分段
    # - 每个分段作为独立的 reply 动作发送
    # - 这样更符合真人发微信的习惯
    logger.debug("[KFC PostProcessor] 消息分割已禁用（由LLM自行通过多个reply分割）")
    return [processed_content]
