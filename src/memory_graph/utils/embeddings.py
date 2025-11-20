"""
嵌入向量生成器：优先使用配置的 embedding API，失败时跳过向量生成
"""

from __future__ import annotations

import numpy as np

from src.common.logger import get_logger

logger = get_logger(__name__)


class EmbeddingGenerator:
    """
    嵌入向量生成器

    策略：
    1. 优先使用配置的 embedding API（通过 LLMRequest）
    2. 如果 API 不可用或失败，跳过向量生成，返回 None 或零向量
    3. 不再使用本地 sentence-transformers 模型，避免向量维度不匹配

    优点：
    - 完全避免本地运算负载
    - 避免向量维度不匹配问题
    - 简化错误处理逻辑
    - 保持与现有系统的一致性
    """

    def __init__(
        self,
        use_api: bool = True,
    ):
        """
        初始化嵌入生成器

        Args:
            use_api: 是否使用 API（默认 True）
        """
        self.use_api = use_api

        # API 相关
        self._llm_request = None
        self._api_available = False
        self._api_dimension = None

    async def _initialize_api(self):
        """初始化 embedding API"""
        if self._api_available:
            return

        try:
            from src.config.config import model_config
            from src.llm_models.utils_model import LLMRequest

            embedding_config = model_config.model_task_config.embedding
            self._llm_request = LLMRequest(
                model_set=embedding_config,
                request_type="memory_graph.embedding"
            )

            # 获取嵌入维度
            if hasattr(embedding_config, "embedding_dimension") and embedding_config.embedding_dimension:
                self._api_dimension = embedding_config.embedding_dimension

            self._api_available = True
            logger.info(f"✅ Embedding API 初始化成功 (维度: {self._api_dimension})")

        except Exception as e:
            logger.warning(f"⚠️  Embedding API 初始化失败: {e}")
            self._api_available = False


    async def generate(self, text: str) -> np.ndarray | None:
        """
        生成单个文本的嵌入向量

        策略：
        1. 使用 API 生成向量
        2. API 失败则返回 None，跳过向量生成

        Args:
            text: 输入文本

        Returns:
            嵌入向量，失败时返回 None
        """
        if not text or not text.strip():
            logger.debug("输入文本为空，返回 None")
            return None

        try:
            # 使用 API 生成嵌入
            if self.use_api:
                embedding = await self._generate_with_api(text)
                if embedding is not None:
                    return embedding

            # API 失败，记录日志并返回 None
            logger.debug(f"⚠️  嵌入生成失败，跳过: {text[:30]}...")
            return None

        except Exception as e:
            logger.error(f"❌ 嵌入生成异常: {e}", exc_info=True)
            return None

    async def _generate_with_api(self, text: str) -> np.ndarray | None:
        """使用 API 生成嵌入"""
        try:
            # 初始化 API
            if not self._api_available:
                await self._initialize_api()

            if not self._api_available or not self._llm_request:
                return None

            # 调用 API
            embedding_list, model_name = await self._llm_request.get_embedding(text)

            if embedding_list and len(embedding_list) > 0:
                embedding = np.array(embedding_list, dtype=np.float32)
                logger.debug(f"🌐 API 生成嵌入: {text[:30]}... -> {len(embedding)}维 (模型: {model_name})")
                return embedding

            return None

        except Exception as e:
            logger.debug(f"API 嵌入生成失败: {e}")
            return None


    def _get_dimension(self) -> int:
        """获取嵌入维度"""
        # 优先使用 API 维度
        if self._api_dimension:
            return self._api_dimension

        raise ValueError("无法确定嵌入向量维度，请确保已正确配置 embedding API")


    async def generate_batch(self, texts: list[str]) -> list[np.ndarray | None]:
        """保留输入顺序的批量嵌入生成"""
        if not texts:
            return []

        try:
            results: list[np.ndarray | None] = [None] * len(texts)
            valid_entries = [
                (idx, text) for idx, text in enumerate(texts) if text and text.strip()
            ]
            if not valid_entries:
                logger.debug('批量文本为空，返回空列表')
                return results

            batch_texts = [text for _, text in valid_entries]
            batch_embeddings: list[np.ndarray | None] | None = None

            if self.use_api:
                batch_embeddings = await self._generate_batch_with_api(batch_texts)

            if not batch_embeddings:
                batch_embeddings = []
                for _, text in valid_entries:
                    batch_embeddings.append(await self.generate(text))

            for (idx, _), embedding in zip(valid_entries, batch_embeddings):
                results[idx] = embedding

            success_count = sum(1 for r in results if r is not None)
            logger.debug(f"批量生成嵌入: {success_count}/{len(texts)}")
            return results

        except Exception as e:
            logger.error(f"批量生成嵌入失败: {e}", exc_info=True)
            return [None for _ in texts]

    async def _generate_batch_with_api(self, texts: list[str]) -> list[np.ndarray | None] | None:
        """使用嵌入 API 在单次请求中生成向量"""
        if not texts:
            return []

        try:
            if not self._api_available:
                await self._initialize_api()

            if not self._api_available or not self._llm_request:
                return None

            embeddings, model_name = await self._llm_request.get_embedding(texts)
            if not embeddings:
                return None

            results: list[np.ndarray | None] = []
            for emb in embeddings:
                if emb:
                    results.append(np.array(emb, dtype=np.float32))
                else:
                    results.append(None)

            logger.debug(f"API 批量生成 {len(texts)} 个嵌入向量，使用模型: {model_name}")
            return results

        except Exception as e:
            logger.debug(f"API 批量生成失败: {e}")
            return None

    def get_embedding_dimension(self) -> int:
        """获取嵌入向量维度"""
        return self._get_dimension()


# 全局单例
_global_generator: EmbeddingGenerator | None = None


def get_embedding_generator(
    use_api: bool = True,
) -> EmbeddingGenerator:
    """
    获取全局嵌入生成器单例

    Args:
        use_api: 是否使用 API

    Returns:
        EmbeddingGenerator 实例
    """
    global _global_generator
    if _global_generator is None:
        _global_generator = EmbeddingGenerator(use_api=use_api)
    return _global_generator
