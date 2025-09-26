# -*- coding: utf-8 -*-
"""
@File    :   model_selector.py
@Time    :   2024/05/24 16:00:00
@Author  :   墨墨
@Version :   1.0
@Desc    :   模型选择与负载均衡器
"""
from typing import Dict, Tuple, Set, Optional

from src.common.logger import get_logger
from src.config.config import model_config
from src.config.api_ada_configs import ModelInfo, APIProvider, TaskConfig
from .model_client.base_client import BaseClient, client_registry

logger = get_logger("model_selector")


class ModelSelector:
    """模型选择与负载均衡器"""

    def __init__(self, model_set: TaskConfig, request_type: str = ""):
        """
        初始化模型选择器

        Args:
            model_set (TaskConfig): 任务配置中定义的模型集合
            request_type (str, optional): 请求类型 (例如 "embedding"). Defaults to "".
        """
        self.model_for_task = model_set
        self.request_type = request_type
        self.model_usage: Dict[str, Tuple[int, int, int]] = {
            model: (0, 0, 0) for model in self.model_for_task.model_list
        }
        """模型使用量记录，用于进行负载均衡，对应为(total_tokens, penalty, usage_penalty)，惩罚值是为了能在某个模型请求不给力或正在被使用的时候进行调整"""

    def select_best_available_model(
        self, failed_models_in_this_request: Set[str]
    ) -> Optional[Tuple[ModelInfo, APIProvider, BaseClient]]:
        """
        从可用模型中选择负载均衡评分最低的模型，并排除当前请求中已失败的模型。

        Args:
            failed_models_in_this_request (Set[str]): 当前请求中已失败的模型名称集合。

        Returns:
            Optional[Tuple[ModelInfo, APIProvider, BaseClient]]: 选定的模型详细信息，如果无可用模型则返回 None。
        """
        candidate_models_usage = {
            model_name: usage_data
            for model_name, usage_data in self.model_usage.items()
            if model_name not in failed_models_in_this_request
        }

        if not candidate_models_usage:
            logger.warning("没有可用的模型供当前请求选择。")
            return None

        # 根据现有公式查找分数最低的模型
        # 公式: total_tokens + penalty * 300 + usage_penalty * 1000
        # 较高的 usage_penalty (由于被选中的模型会被增加) 和 penalty (由于模型失败) 会使模型得分更高，从而降低被选中的几率。
        least_used_model_name = min(
            candidate_models_usage,
            key=lambda k: candidate_models_usage[k][0]
            + candidate_models_usage[k][1] * 300
            + candidate_models_usage[k][2] * 1000,
        )

        # --- 动态故障转移的核心逻辑 ---
        # RequestStrategy 中的循环会多次调用此函数。
        # 如果当前选定的模型因异常而失败，下次循环会重新调用此函数，
        # 此时由于失败模型已被标记，且其惩罚值可能已在 RequestExecutor 中增加，
        # 此函数会自动选择一个得分更低（即更可用）的模型。
        # 这种机制实现了动态的、基于当前系统状态的故障转移。
        model_info = model_config.get_model_info(least_used_model_name)
        api_provider = model_config.get_provider(model_info.api_provider)

        force_new_client = self.request_type == "embedding"
        client = client_registry.get_client_class_instance(api_provider, force_new=force_new_client)

        logger.debug(f"为当前请求选择了最佳可用模型: {model_info.name}")

        # 增加所选模型的请求使用惩罚值，以反映其当前使用情况/选择。
        # 这有助于在同一请求的后续选择或未来请求中实现动态负载均衡。
        total_tokens, penalty, usage_penalty = self.model_usage[model_info.name]
        self.model_usage[model_info.name] = (total_tokens, penalty, usage_penalty + 1)

        return model_info, api_provider, client

    def select_model(self) -> Tuple[ModelInfo, APIProvider, BaseClient]:
        """
        根据总tokens和惩罚值选择的模型 (负载均衡)
        """
        least_used_model_name = min(
            self.model_usage,
            key=lambda k: self.model_usage[k][0] + self.model_usage[k][1] * 300 + self.model_usage[k][2] * 1000,
        )
        model_info = model_config.get_model_info(least_used_model_name)
        api_provider = model_config.get_provider(model_info.api_provider)

        force_new_client = self.request_type == "embedding"
        client = client_registry.get_client_class_instance(api_provider, force_new=force_new_client)
        logger.debug(f"选择请求模型: {model_info.name}")
        total_tokens, penalty, usage_penalty = self.model_usage[model_info.name]
        self.model_usage[model_info.name] = (total_tokens, penalty, usage_penalty + 1)
        return model_info, api_provider, client

    def update_model_penalty(self, model_name: str, penalty_increment: int):
        """
        更新指定模型的惩罚值

        Args:
            model_name (str): 模型名称
            penalty_increment (int): 惩罚增量
        """
        if model_name in self.model_usage:
            total_tokens, penalty, usage_penalty = self.model_usage[model_name]
            self.model_usage[model_name] = (total_tokens, penalty + penalty_increment, usage_penalty)
            logger.debug(f"模型 '{model_name}' 的惩罚值增加了 {penalty_increment}")

    def decrease_usage_penalty(self, model_name: str):
        """
        请求结束后，减少使用惩罚值

        Args:
            model_name (str): 模型名称
        """
        if model_name in self.model_usage:
            total_tokens, penalty, usage_penalty = self.model_usage[model_name]
            self.model_usage[model_name] = (total_tokens, penalty, usage_penalty - 1)