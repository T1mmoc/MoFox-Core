"""
相似度计算工具

提供统一的向量相似度计算函数
"""

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np


def cosine_similarity(vec1: "np.ndarray", vec2: "np.ndarray") -> float:
    """
    计算两个向量的余弦相似度

    Args:
        vec1: 第一个向量
        vec2: 第二个向量

    Returns:
        余弦相似度 (0.0-1.0)
    """
    try:
        import numpy as np

        # 确保是numpy数组
        if not isinstance(vec1, np.ndarray):
            vec1 = np.array(vec1)
        if not isinstance(vec2, np.ndarray):
            vec2 = np.array(vec2)

        # 归一化
        vec1_norm = np.linalg.norm(vec1)
        vec2_norm = np.linalg.norm(vec2)

        if vec1_norm == 0 or vec2_norm == 0:
            return 0.0

        # 余弦相似度
        similarity = np.dot(vec1, vec2) / (vec1_norm * vec2_norm)

        # 确保在 [0, 1] 范围内（处理浮点误差）
        return float(np.clip(similarity, 0.0, 1.0))

    except Exception:
        return 0.0


async def cosine_similarity_async(vec1: "np.ndarray", vec2: "np.ndarray") -> float:
    """
    异步计算两个向量的余弦相似度，使用to_thread避免阻塞

    Args:
        vec1: 第一个向量
        vec2: 第二个向量

    Returns:
        余弦相似度 (0.0-1.0)
    """
    return await asyncio.to_thread(cosine_similarity, vec1, vec2)


def batch_cosine_similarity(vec1: "np.ndarray", vec_list: list["np.ndarray"]) -> list[float]:
    """
    批量计算向量相似度

    Args:
        vec1: 基础向量
        vec_list: 待比较的向量列表

    Returns:
        相似度列表
    """
    try:
        import numpy as np

        # 确保是numpy数组
        if not isinstance(vec1, np.ndarray):
            vec1 = np.array(vec1)

        # 批量转换为numpy数组
        vec_list = [np.array(vec) for vec in vec_list]

        # 计算归一化
        vec1_norm = np.linalg.norm(vec1)
        if vec1_norm == 0:
            return [0.0] * len(vec_list)

        # 计算所有向量的归一化
        vec_norms = np.array([np.linalg.norm(vec) for vec in vec_list])

        # 避免除以0
        valid_mask = vec_norms != 0
        similarities = np.zeros(len(vec_list))

        if np.any(valid_mask):
            # 批量计算点积
            valid_vecs = np.array(vec_list)[valid_mask]
            dot_products = np.dot(valid_vecs, vec1)

            # 计算相似度
            valid_norms = vec_norms[valid_mask]
            valid_similarities = dot_products / (vec1_norm * valid_norms)

            # 确保在 [0, 1] 范围内
            valid_similarities = np.clip(valid_similarities, 0.0, 1.0)

            # 填充结果
            similarities[valid_mask] = valid_similarities

        return similarities.tolist()

    except Exception:
        return [0.0] * len(vec_list)


async def batch_cosine_similarity_async(vec1: "np.ndarray", vec_list: list["np.ndarray"]) -> list[float]:
    """
    异步批量计算向量相似度，使用to_thread避免阻塞

    Args:
        vec1: 基础向量
        vec_list: 待比较的向量列表

    Returns:
        相似度列表
    """
    return await asyncio.to_thread(batch_cosine_similarity, vec1, vec_list)


__all__ = [
    "cosine_similarity",
    "cosine_similarity_async",
    "batch_cosine_similarity",
    "batch_cosine_similarity_async"
]
