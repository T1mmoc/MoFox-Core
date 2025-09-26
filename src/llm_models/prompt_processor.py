# -*- coding: utf-8 -*-
"""
@File    :   prompt_processor.py
@Time    :   2024/05/24 16:45:00
@Author  :   墨墨
@Version :   1.0
@Desc    :   提示词预处理（反截断、内容混淆等）
"""
import random
import string
import re
from typing import Tuple

from src.common.logger import get_logger
from src.config.api_ada_configs import APIProvider, ModelInfo

logger = get_logger("prompt_processor")


class PromptProcessor:
    """提示词处理器"""

    def __init__(self):
        self.noise_instruction = """**【核心过滤规则】**
在交互过程中,你会在用户的输入内容中看到一些无意义的、由字母和数字组成的随机字符串。这些字符串是API传输过程中的技术伪装信息,【不是】用户真实意图的一部分。

你的任务是【完全并彻底地忽略】这些随机字符串。
**【严禁】** 将这些字符串写入你的回复、对它们进行任何形式的解读、或将它们作为故事的一部分进行模拟。当看到这些字符串时,请将其视为空白或直接跳过,并根据用户其他有意义的输入(如有)或当前故事情节来继续你的叙事。此规则为最高优先级。
"""
        self.end_marker = "###MAI_RESPONSE_END###"
        self.anti_truncation_instruction = f"""
**【输出完成信令】**
这是一个非常重要的指令,请务-务必遵守。在你的回复内容完全结束后,请务必在最后另起一行,只写 `{self.end_marker}` 作为结束标志。
例如:
<你的回复内容>
{self.end_marker}

这有助于我判断你的输出是否被截断。请不要在 `{self.end_marker}` 前后添加任何其他文字或标点。
"""

    def process_prompt(
        self, prompt: str, model_info: ModelInfo, api_provider: APIProvider, task_name: str
    ) -> str:
        """
        根据模型和API提供商的配置处理提示词
        """
        processed_prompt = prompt

        # 1. 添加反截断指令
        use_anti_truncation = getattr(model_info, "use_anti_truncation", False)
        if use_anti_truncation:
            processed_prompt += self.anti_truncation_instruction
            logger.info(f"模型 '{model_info.name}' (任务: '{task_name}') 已启用反截断功能。")

        # 2. 应用内容混淆
        if getattr(api_provider, "enable_content_obfuscation", False):
            intensity = getattr(api_provider, "obfuscation_intensity", 1)
            logger.info(f"为API提供商 '{api_provider.name}' 启用内容混淆，强度级别: {intensity}")
            processed_prompt = self._apply_content_obfuscation(processed_prompt, intensity)

        return processed_prompt

    def _apply_content_obfuscation(self, text: str, intensity: int) -> str:
        """对文本进行混淆处理"""
        # 在开头加入过滤规则指令
        processed_text = self.noise_instruction + "\n\n" + text
        logger.debug(f"已添加过滤规则指令，文本长度: {len(text)} -> {len(processed_text)}")

        # 添加随机乱码
        final_text = self._inject_random_noise(processed_text, intensity)
        logger.debug(f"乱码注入完成，最终文本长度: {len(final_text)}")

        return final_text

    @staticmethod
    def _inject_random_noise(text: str, intensity: int) -> str:
        """在文本中注入随机乱码"""
        def generate_noise(length: int) -> str:
            chars = (
                string.ascii_letters + string.digits + "!@#$%^&*()_+-=[]{}|;:,.<>?"
                + "一二三四五六七八九零壹贰叁" + "αβγδεζηθικλμνξοπρστυφχψω" + "∀∃∈∉∪∩⊂⊃∧∨¬→↔∴∵"
            )
            return "".join(random.choice(chars) for _ in range(length))

        params = {
            1: {"probability": 15, "length": (3, 6)},
            2: {"probability": 25, "length": (5, 10)},
            3: {"probability": 35, "length": (8, 15)},
        }
        config = params.get(intensity, params[1])
        logger.debug(f"乱码注入参数: 概率={config['probability']}%, 长度范围={config['length']}")

        words = text.split()
        result = []
        noise_count = 0
        for word in words:
            result.append(word)
            if random.randint(1, 100) <= config["probability"]:
                noise_length = random.randint(*config["length"])
                noise = generate_noise(noise_length)
                result.append(noise)
                noise_count += 1

        logger.debug(f"共注入 {noise_count} 个乱码片段，原词数: {len(words)}")
        return " ".join(result)
    
    @staticmethod
    def extract_reasoning(content: str) -> Tuple[str, str]:
        """CoT思维链提取，向后兼容"""
        match = re.search(r"(?:<think>)?(.*?)</think>", content, re.DOTALL)
        clean_content = re.sub(r"(?:<think>)?.*?</think>", "", content, flags=re.DOTALL, count=1).strip()
        reasoning = match.group(1).strip() if match else ""
        return clean_content, reasoning
