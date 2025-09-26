# -*- coding: utf-8 -*-
"""
@File    :   llm_utils.py
@Time    :   2024/05/24 17:00:00
@Author  :   墨墨
@Version :   1.0
@Desc    :   LLM相关通用工具函数
"""
from typing import List, Dict, Any, Tuple

from src.common.logger import get_logger
from .payload_content.tool_option import ToolOption, ToolOptionBuilder, ToolParamType

logger = get_logger("llm_utils")

def normalize_image_format(image_format: str) -> str:
    """
    标准化图片格式名称，确保与各种API的兼容性
    """
    format_mapping = {
        "jpg": "jpeg", "JPG": "jpeg", "JPEG": "jpeg", "jpeg": "jpeg",
        "png": "png", "PNG": "png",
        "webp": "webp", "WEBP": "webp",
        "gif": "gif", "GIF": "gif",
        "heic": "heic", "HEIC": "heic",
        "heif": "heif", "HEIF": "heif",
    }
    normalized = format_mapping.get(image_format, image_format.lower())
    logger.debug(f"图片格式标准化: {image_format} -> {normalized}")
    return normalized

def build_tool_options(tools: List[Dict[str, Any]] | None) -> List[ToolOption] | None:
    """构建工具选项列表"""
    if not tools:
        return None
    tool_options: List[ToolOption] = []
    for tool in tools:
        try:
            tool_options_builder = ToolOptionBuilder()
            tool_options_builder.set_name(tool.get("name", ""))
            tool_options_builder.set_description(tool.get("description", ""))
            parameters: List[Tuple[str, str, str, bool, List[str] | None]] = tool.get("parameters", [])
            for param in parameters:
                # 参数校验
                assert isinstance(param, tuple) and len(param) == 5, "参数必须是包含5个元素的元组"
                assert isinstance(param[0], str), "参数名称必须是字符串"
                assert isinstance(param[1], ToolParamType), "参数类型必须是ToolParamType枚举"
                assert isinstance(param[2], str), "参数描述必须是字符串"
                assert isinstance(param[3], bool), "参数是否必填必须是布尔值"
                assert isinstance(param[4], list) or param[4] is None, "参数枚举值必须是列表或None"
                
                tool_options_builder.add_param(
                    name=param[0],
                    param_type=param[1],
                    description=param[2],
                    required=param[3],
                    enum_values=param[4],
                )
            tool_options.append(tool_options_builder.build())
        except AssertionError as ae:
            logger.error(f"工具 '{tool.get('name', 'unknown')}' 的参数定义错误: {str(ae)}")
        except Exception as e:
            logger.error(f"构建工具 '{tool.get('name', 'unknown')}' 失败: {str(e)}")
            
    return tool_options or None