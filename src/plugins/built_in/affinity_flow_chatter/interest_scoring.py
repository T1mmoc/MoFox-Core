"""
兴趣度评分系统
基于多维度评分机制，包括兴趣匹配度、用户关系分、提及度和时间因子
现在使用embedding计算智能兴趣匹配
"""

import traceback
from typing import Dict, List, Any

from src.common.data_models.database_data_model import DatabaseMessages
from src.common.data_models.info_data_model import InterestScore
from src.chat.interest_system import bot_interest_manager
from src.common.logger import get_logger
from src.config.config import global_config

logger = get_logger("chatter_interest_scoring")


class ChatterInterestScoringSystem:
    """兴趣度评分系统"""

    def __init__(self):
        # 智能兴趣匹配配置
        self.use_smart_matching = True

        # 从配置加载评分权重
        affinity_config = global_config.affinity_flow
        self.score_weights = {
            "interest_match": affinity_config.keyword_match_weight,  # 兴趣匹配度权重
            "relationship": affinity_config.relationship_weight,  # 关系分权重
            "mentioned": affinity_config.mention_bot_weight,  # 是否提及bot权重
        }

        # 评分阈值
        self.reply_threshold = affinity_config.reply_action_interest_threshold  # 回复动作兴趣阈值
        self.mention_threshold = affinity_config.mention_bot_adjustment_threshold  # 提及bot后的调整阈值

        # 连续不回复概率提升
        self.no_reply_count = 0
        self.max_no_reply_count = affinity_config.max_no_reply_count
        self.probability_boost_per_no_reply = (
            affinity_config.no_reply_threshold_adjustment / affinity_config.max_no_reply_count
        )  # 每次不回复增加的概率

        # 用户关系数据
        self.user_relationships: Dict[str, float] = {}  # user_id -> relationship_score

    async def calculate_interest_scores(
        self, messages: List[DatabaseMessages], bot_nickname: str
    ) -> List[InterestScore]:
        """计算消息的兴趣度评分"""
        user_messages = [msg for msg in messages if str(msg.user_info.user_id) != str(global_config.bot.qq_account)]
        if not user_messages:
            return []

        scores = []
        for _, msg in enumerate(user_messages, 1):
            score = await self._calculate_single_message_score(msg, bot_nickname)
            scores.append(score)

        return scores

    async def _calculate_single_message_score(self, message: DatabaseMessages, bot_nickname: str) -> InterestScore:
        """计算单条消息的兴趣度评分"""

        keywords = self._extract_keywords_from_database(message)
        interest_match_score = await self._calculate_interest_match_score(message.processed_plain_text, keywords)
        relationship_score = self._calculate_relationship_score(message.user_info.user_id)
        mentioned_score = self._calculate_mentioned_score(message, bot_nickname)

        total_score = (
            interest_match_score * self.score_weights["interest_match"]
            + relationship_score * self.score_weights["relationship"]
            + mentioned_score * self.score_weights["mentioned"]
        )

        details = {
            "interest_match": f"兴趣匹配: {interest_match_score:.3f}",
            "relationship": f"关系: {relationship_score:.3f}",
            "mentioned": f"提及: {mentioned_score:.3f}",
        }

        logger.info(
            f"消息得分: {total_score:.3f} (匹配: {interest_match_score:.2f}, 关系: {relationship_score:.2f}, 提及: {mentioned_score:.2f})"
        )

        return InterestScore(
            message_id=message.message_id,
            total_score=total_score,
            interest_match_score=interest_match_score,
            relationship_score=relationship_score,
            mentioned_score=mentioned_score,
            details=details,
        )

    async def _calculate_interest_match_score(self, content: str, keywords: List[str] = None) -> float:
        """计算兴趣匹配度 - 使用智能embedding匹配"""
        if not content:
            return 0.0

        # 使用智能匹配（embedding）
        if self.use_smart_matching and bot_interest_manager.is_initialized:
            return await self._calculate_smart_interest_match(content, keywords)
        else:
            # 智能匹配未初始化，返回默认分数
            return 0.3

    async def _calculate_smart_interest_match(self, content: str, keywords: List[str] = None) -> float:
        """使用embedding计算智能兴趣匹配"""
        try:
            # 如果没有传入关键词，则提取
            if not keywords:
                keywords = self._extract_keywords_from_content(content)

            # 使用机器人兴趣管理器计算匹配度
            match_result = await bot_interest_manager.calculate_interest_match(content, keywords)

            if match_result:
                # 返回匹配分数，考虑置信度和匹配标签数量
                affinity_config = global_config.affinity_flow
                match_count_bonus = min(
                    len(match_result.matched_tags) * affinity_config.match_count_bonus, affinity_config.max_match_bonus
                )
                final_score = match_result.overall_score * 1.15 * match_result.confidence + match_count_bonus
                return final_score
            else:
                return 0.0

        except Exception as e:
            logger.error(f"智能兴趣匹配计算失败: {e}")
            return 0.0

    def _extract_keywords_from_database(self, message: DatabaseMessages) -> List[str]:
        """从数据库消息中提取关键词"""
        keywords = []

        # 尝试从 key_words 字段提取（存储的是JSON字符串）
        if message.key_words:
            try:
                import orjson

                keywords = orjson.loads(message.key_words)
                if not isinstance(keywords, list):
                    keywords = []
            except (orjson.JSONDecodeError, TypeError):
                keywords = []

        # 如果没有 keywords，尝试从 key_words_lite 提取
        if not keywords and message.key_words_lite:
            try:
                import orjson

                keywords = orjson.loads(message.key_words_lite)
                if not isinstance(keywords, list):
                    keywords = []
            except (orjson.JSONDecodeError, TypeError):
                keywords = []

        # 如果还是没有，从消息内容中提取（降级方案）
        if not keywords:
            keywords = self._extract_keywords_from_content(message.processed_plain_text)

        return keywords[:15]  # 返回前15个关键词

    def _extract_keywords_from_content(self, content: str) -> List[str]:
        """从内容中提取关键词（降级方案）"""
        import re

        # 清理文本
        content = re.sub(r"[^\w\s\u4e00-\u9fff]", " ", content)  # 保留中文、英文、数字
        words = content.split()

        # 过滤和关键词提取
        keywords = []
        for word in words:
            word = word.strip()
            if (
                len(word) >= 2  # 至少2个字符
                and word.isalnum()  # 字母数字
                and not word.isdigit()
            ):  # 不是纯数字
                keywords.append(word.lower())

        # 去重并限制数量
        unique_keywords = list(set(keywords))
        return unique_keywords[:10]  # 返回前10个唯一关键词

    def _calculate_relationship_score(self, user_id: str) -> float:
        """计算关系分 - 从数据库获取关系分"""
        # 优先使用内存中的关系分
        if user_id in self.user_relationships:
            relationship_value = self.user_relationships[user_id]
            return min(relationship_value, 1.0)

        # 如果内存中没有，尝试从关系追踪器获取
        if hasattr(self, "relationship_tracker") and self.relationship_tracker:
            try:
                relationship_score = self.relationship_tracker.get_user_relationship_score(user_id)
                # 同时更新内存缓存
                self.user_relationships[user_id] = relationship_score
                return relationship_score
            except Exception:
                pass
        else:
            # 尝试从全局关系追踪器获取
            try:
                from .relationship_tracker import ChatterRelationshipTracker

                global_tracker = ChatterRelationshipTracker()
                if global_tracker:
                    relationship_score = global_tracker.get_user_relationship_score(user_id)
                    # 同时更新内存缓存
                    self.user_relationships[user_id] = relationship_score
                    return relationship_score
            except Exception:
                pass

        # 默认新用户的基础分
        return global_config.affinity_flow.base_relationship_score

    def _calculate_mentioned_score(self, msg: DatabaseMessages, bot_nickname: str) -> float:
        """计算提及分数"""
        if not msg.processed_plain_text:
            return 0.0

        # 检查是否被提及
        bot_aliases = [bot_nickname] + global_config.bot.alias_names
        is_mentioned = msg.is_mentioned or any(alias in msg.processed_plain_text for alias in bot_aliases if alias)

        # 如果被提及或是私聊，都视为提及了bot
        if is_mentioned or not hasattr(msg, "chat_info_group_id"):
            return global_config.affinity_flow.mention_bot_interest_score

        return 0.0

    def should_reply(self, score: InterestScore, message: "DatabaseMessages") -> bool:
        """判断是否应该回复"""
        base_threshold = self.reply_threshold

        # 如果被提及，降低阈值
        if score.mentioned_score >= global_config.affinity_flow.mention_bot_adjustment_threshold:
            base_threshold = self.mention_threshold

        # 计算连续不回复的概率提升
        probability_boost = min(self.no_reply_count * self.probability_boost_per_no_reply, 0.8)
        effective_threshold = base_threshold - probability_boost

        # 做出决策
        should_reply = score.total_score >= effective_threshold
        decision = "回复" if should_reply else "不回复"
        logger.info(f"决策: {decision} (分数: {score.total_score:.3f})")

        return should_reply, score.total_score

    def record_reply_action(self, did_reply: bool):
        """记录回复动作"""
        old_count = self.no_reply_count
        if did_reply:
            self.no_reply_count = max(0, self.no_reply_count - global_config.affinity_flow.reply_cooldown_reduction)
            action = "回复"
        else:
            self.no_reply_count += 1
            action = "不回复"

        # 限制最大计数
        self.no_reply_count = min(self.no_reply_count, self.max_no_reply_count)
        logger.info(f"{action} | 不回复次数: {old_count} -> {self.no_reply_count}")

    def update_user_relationship(self, user_id: str, relationship_change: float):
        """更新用户关系"""
        old_score = self.user_relationships.get(
            user_id, global_config.affinity_flow.base_relationship_score
        )  # 默认新用户分数
        new_score = max(0.0, min(1.0, old_score + relationship_change))

        self.user_relationships[user_id] = new_score

        logger.info(f"用户关系: {user_id} | {old_score:.3f} → {new_score:.3f}")

    def get_user_relationship(self, user_id: str) -> float:
        """获取用户关系分"""
        return self.user_relationships.get(user_id, 0.3)

    def get_scoring_stats(self) -> Dict:
        """获取评分系统统计"""
        return {
            "no_reply_count": self.no_reply_count,
            "max_no_reply_count": self.max_no_reply_count,
            "reply_threshold": self.reply_threshold,
            "mention_threshold": self.mention_threshold,
            "user_relationships": len(self.user_relationships),
        }

    def reset_stats(self):
        """重置统计信息"""
        self.no_reply_count = 0
        logger.info("重置兴趣度评分系统统计")

    async def initialize_smart_interests(self, personality_description: str, personality_id: str = "default"):
        """初始化智能兴趣系统"""
        try:
            logger.info("开始初始化智能兴趣系统...")
            logger.info(f"人设ID: {personality_id}, 描述长度: {len(personality_description)}")

            await bot_interest_manager.initialize(personality_description, personality_id)
            logger.info("智能兴趣系统初始化完成。")

            # 显示初始化后的统计信息
            bot_interest_manager.get_interest_stats()

        except Exception as e:
            logger.error(f"初始化智能兴趣系统失败: {e}")
            traceback.print_exc()

    def get_matching_config(self) -> Dict[str, Any]:
        """获取匹配配置信息"""
        return {
            "use_smart_matching": self.use_smart_matching,
            "smart_system_initialized": bot_interest_manager.is_initialized,
            "smart_system_stats": bot_interest_manager.get_interest_stats()
            if bot_interest_manager.is_initialized
            else None,
        }


# 创建全局兴趣评分系统实例
chatter_interest_scoring_system = ChatterInterestScoringSystem()
