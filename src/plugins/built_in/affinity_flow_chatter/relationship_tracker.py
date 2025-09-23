"""
用户关系追踪器
负责追踪用户交互历史，并通过LLM分析更新用户关系分
支持数据库持久化存储和回复后自动关系更新
"""

import time
from typing import Dict, List, Optional

from src.common.logger import get_logger
from src.config.config import model_config, global_config
from src.llm_models.utils_model import LLMRequest
from src.common.database.sqlalchemy_database_api import get_db_session
from src.common.database.sqlalchemy_models import UserRelationships, Messages
from sqlalchemy import select, desc
from src.common.data_models.database_data_model import DatabaseMessages

logger = get_logger("chatter_relationship_tracker")


class ChatterRelationshipTracker:
    """用户关系追踪器"""

    def __init__(self, interest_scoring_system=None):
        self.tracking_users: Dict[str, Dict] = {}  # user_id -> interaction_data
        self.max_tracking_users = 3
        self.update_interval_minutes = 30
        self.last_update_time = time.time()
        self.relationship_history: List[Dict] = []
        self.interest_scoring_system = interest_scoring_system

        # 数据库访问 - 使用SQLAlchemy
        pass

        # 用户关系缓存 (user_id -> {"relationship_text": str, "relationship_score": float, "last_tracked": float})
        self.user_relationship_cache: Dict[str, Dict] = {}
        self.cache_expiry_hours = 1  # 缓存过期时间(小时)

        # 关系更新LLM
        try:
            self.relationship_llm = LLMRequest(
                model_set=model_config.model_task_config.relationship_tracker, request_type="relationship_tracker"
            )
        except AttributeError:
            # 如果relationship_tracker配置不存在，尝试其他可用的模型配置
            available_models = [
                attr
                for attr in dir(model_config.model_task_config)
                if not attr.startswith("_") and attr != "model_dump"
            ]

            if available_models:
                # 使用第一个可用的模型配置
                fallback_model = available_models[0]
                logger.warning(f"relationship_tracker model configuration not found, using fallback: {fallback_model}")
                self.relationship_llm = LLMRequest(
                    model_set=getattr(model_config.model_task_config, fallback_model),
                    request_type="relationship_tracker",
                )
            else:
                # 如果没有任何模型配置，创建一个简单的LLMRequest
                logger.warning("No model configurations found, creating basic LLMRequest")
                self.relationship_llm = LLMRequest(
                    model_set="gpt-3.5-turbo",  # 默认模型
                    request_type="relationship_tracker",
                )

    def set_interest_scoring_system(self, interest_scoring_system):
        """设置兴趣度评分系统引用"""
        self.interest_scoring_system = interest_scoring_system

    def add_interaction(self, user_id: str, user_name: str, user_message: str, bot_reply: str, reply_timestamp: float):
        """添加用户交互记录"""
        if len(self.tracking_users) >= self.max_tracking_users:
            # 移除最旧的记录
            oldest_user = min(
                self.tracking_users.keys(), key=lambda k: self.tracking_users[k].get("reply_timestamp", 0)
            )
            del self.tracking_users[oldest_user]

        # 获取当前关系分
        current_relationship_score = global_config.affinity_flow.base_relationship_score  # 默认值
        if self.interest_scoring_system:
            current_relationship_score = self.interest_scoring_system.get_user_relationship(user_id)

        self.tracking_users[user_id] = {
            "user_id": user_id,
            "user_name": user_name,
            "user_message": user_message,
            "bot_reply": bot_reply,
            "reply_timestamp": reply_timestamp,
            "current_relationship_score": current_relationship_score,
        }

        logger.debug(f"添加用户交互追踪: {user_id}")

    async def check_and_update_relationships(self) -> List[Dict]:
        """检查并更新用户关系"""
        current_time = time.time()
        if current_time - self.last_update_time < self.update_interval_minutes * 60:
            return []

        updates = []
        for user_id, interaction in list(self.tracking_users.items()):
            if current_time - interaction["reply_timestamp"] > 60 * 5:  # 5分钟
                update = await self._update_user_relationship(interaction)
                if update:
                    updates.append(update)
                    del self.tracking_users[user_id]

        self.last_update_time = current_time
        return updates

    async def _update_user_relationship(self, interaction: Dict) -> Optional[Dict]:
        """更新单个用户的关系"""
        try:
            # 获取bot人设信息
            from src.individuality.individuality import Individuality

            individuality = Individuality()
            bot_personality = await individuality.get_personality_block()

            prompt = f"""
你现在是一个有着特定性格和身份的AI助手。你的人设是：{bot_personality}

请以你独特的性格视角，严格按现实逻辑分析以下用户交互，更新用户关系：

用户ID: {interaction["user_id"]}
用户名: {interaction["user_name"]}
用户消息: {interaction["user_message"]}
你的回复: {interaction["bot_reply"]}
当前关系分: {interaction["current_relationship_score"]}

【重要】关系分数档次定义：
- 0.0-0.2：陌生人/初次认识 - 仅礼貌性交流
- 0.2-0.4：普通网友 - 有基本互动但不熟悉
- 0.4-0.6：熟悉网友 - 经常交流，有一定了解
- 0.6-0.8：朋友 - 可以分享心情，互相关心
- 0.8-1.0：好朋友/知己 - 深度信任，亲密无间

【严格要求】：
1. 加分必须符合现实关系发展逻辑 - 不能因为对方态度好就盲目加分到不符合当前关系档次的分数
2. 关系提升需要足够的互动积累和时间验证
3. 即使是朋友关系，单次互动加分通常不超过0.05-0.1
4. 关系描述要详细具体，包括：
   - 用户性格特点观察
   - 印象深刻的互动记忆
   - 你们关系的具体状态描述

根据你的人设性格，思考：
1. 以你的性格，你会如何看待这次互动？
2. 用户的行为是否符合你性格的喜好？
3. 这次互动是否真的让你们的关系提升了一个档次？为什么？
4. 有什么特别值得记住的互动细节？

请以JSON格式返回更新结果：
{{
    "new_relationship_score": 0.0~1.0的数值（必须符合现实逻辑）,
    "reasoning": "从你的性格角度说明更新理由，重点说明是否符合现实关系发展逻辑",
    "interaction_summary": "基于你性格的交互总结，包含印象深刻的互动记忆"
}}
"""

            llm_response, _ = await self.relationship_llm.generate_response_async(prompt=prompt)
            if llm_response:
                import json

                try:
                    # 清理LLM响应，移除可能的格式标记
                    cleaned_response = self._clean_llm_json_response(llm_response)
                    response_data = json.loads(cleaned_response)
                    new_score = max(
                        0.0,
                        min(
                            1.0,
                            float(
                                response_data.get(
                                    "new_relationship_score", global_config.affinity_flow.base_relationship_score
                                )
                            ),
                        ),
                    )

                    if self.interest_scoring_system:
                        self.interest_scoring_system.update_user_relationship(
                            interaction["user_id"], new_score - interaction["current_relationship_score"]
                        )

                    return {
                        "user_id": interaction["user_id"],
                        "new_relationship_score": new_score,
                        "reasoning": response_data.get("reasoning", ""),
                        "interaction_summary": response_data.get("interaction_summary", ""),
                    }

                except json.JSONDecodeError as e:
                    logger.error(f"LLM响应JSON解析失败: {e}")
                    logger.debug(f"LLM原始响应: {llm_response}")
                except Exception as e:
                    logger.error(f"处理关系更新数据失败: {e}")

        except Exception as e:
            logger.error(f"更新用户关系时出错: {e}")

        return None

    def get_tracking_users(self) -> Dict[str, Dict]:
        """获取正在追踪的用户"""
        return self.tracking_users.copy()

    def get_user_interaction(self, user_id: str) -> Optional[Dict]:
        """获取特定用户的交互记录"""
        return self.tracking_users.get(user_id)

    def remove_user_tracking(self, user_id: str):
        """移除用户追踪"""
        if user_id in self.tracking_users:
            del self.tracking_users[user_id]
            logger.debug(f"移除用户追踪: {user_id}")

    def clear_all_tracking(self):
        """清空所有追踪"""
        self.tracking_users.clear()
        logger.info("清空所有用户追踪")

    def get_relationship_history(self) -> List[Dict]:
        """获取关系历史记录"""
        return self.relationship_history.copy()

    def add_to_history(self, relationship_update: Dict):
        """添加到关系历史"""
        self.relationship_history.append({**relationship_update, "update_time": time.time()})

        # 限制历史记录数量
        if len(self.relationship_history) > 100:
            self.relationship_history = self.relationship_history[-100:]

    def get_tracker_stats(self) -> Dict:
        """获取追踪器统计"""
        return {
            "tracking_users": len(self.tracking_users),
            "max_tracking_users": self.max_tracking_users,
            "update_interval_minutes": self.update_interval_minutes,
            "relationship_history": len(self.relationship_history),
            "last_update_time": self.last_update_time,
        }

    def update_config(self, max_tracking_users: int = None, update_interval_minutes: int = None):
        """更新配置"""
        if max_tracking_users is not None:
            self.max_tracking_users = max_tracking_users
            logger.info(f"更新最大追踪用户数: {max_tracking_users}")

        if update_interval_minutes is not None:
            self.update_interval_minutes = update_interval_minutes
            logger.info(f"更新关系更新间隔: {update_interval_minutes} 分钟")

    def force_update_relationship(self, user_id: str, new_score: float, reasoning: str = ""):
        """强制更新用户关系分"""
        if user_id in self.tracking_users:
            current_score = self.tracking_users[user_id]["current_relationship_score"]
            if self.interest_scoring_system:
                self.interest_scoring_system.update_user_relationship(user_id, new_score - current_score)

            update_info = {
                "user_id": user_id,
                "new_relationship_score": new_score,
                "reasoning": reasoning or "手动更新",
                "interaction_summary": "手动更新关系分",
            }
            self.add_to_history(update_info)
            logger.info(f"强制更新用户关系: {user_id} -> {new_score:.2f}")

    def get_user_summary(self, user_id: str) -> Dict:
        """获取用户交互总结"""
        if user_id not in self.tracking_users:
            return {}

        interaction = self.tracking_users[user_id]
        return {
            "user_id": user_id,
            "user_name": interaction["user_name"],
            "current_relationship_score": interaction["current_relationship_score"],
            "interaction_count": 1,  # 简化版本，每次追踪只记录一次交互
            "last_interaction": interaction["reply_timestamp"],
            "recent_message": interaction["user_message"][:100] + "..."
            if len(interaction["user_message"]) > 100
            else interaction["user_message"],
        }

    # ===== 数据库支持方法 =====

    def get_user_relationship_score(self, user_id: str) -> float:
        """获取用户关系分"""
        # 先检查缓存
        if user_id in self.user_relationship_cache:
            cache_data = self.user_relationship_cache[user_id]
            # 检查缓存是否过期
            cache_time = cache_data.get("last_tracked", 0)
            if time.time() - cache_time < self.cache_expiry_hours * 3600:
                return cache_data.get("relationship_score", global_config.affinity_flow.base_relationship_score)

        # 缓存过期或不存在，从数据库获取
        relationship_data = self._get_user_relationship_from_db(user_id)
        if relationship_data:
            # 更新缓存
            self.user_relationship_cache[user_id] = {
                "relationship_text": relationship_data.get("relationship_text", ""),
                "relationship_score": relationship_data.get(
                    "relationship_score", global_config.affinity_flow.base_relationship_score
                ),
                "last_tracked": time.time(),
            }
            return relationship_data.get("relationship_score", global_config.affinity_flow.base_relationship_score)

        # 数据库中也没有，返回默认值
        return global_config.affinity_flow.base_relationship_score

    def _get_user_relationship_from_db(self, user_id: str) -> Optional[Dict]:
        """从数据库获取用户关系数据"""
        try:
            with get_db_session() as session:
                # 查询用户关系表
                stmt = select(UserRelationships).where(UserRelationships.user_id == user_id)
                result = session.execute(stmt).scalar_one_or_none()

                if result:
                    return {
                        "relationship_text": result.relationship_text or "",
                        "relationship_score": float(result.relationship_score)
                        if result.relationship_score is not None
                        else 0.3,
                        "last_updated": result.last_updated,
                    }
        except Exception as e:
            logger.error(f"从数据库获取用户关系失败: {e}")

        return None

    def _update_user_relationship_in_db(self, user_id: str, relationship_text: str, relationship_score: float):
        """更新数据库中的用户关系"""
        try:
            current_time = time.time()

            with get_db_session() as session:
                # 检查是否已存在关系记录
                existing = session.execute(
                    select(UserRelationships).where(UserRelationships.user_id == user_id)
                ).scalar_one_or_none()

                if existing:
                    # 更新现有记录
                    existing.relationship_text = relationship_text
                    existing.relationship_score = relationship_score
                    existing.last_updated = current_time
                    existing.user_name = existing.user_name or user_id  # 更新用户名如果为空
                else:
                    # 插入新记录
                    new_relationship = UserRelationships(
                        user_id=user_id,
                        user_name=user_id,
                        relationship_text=relationship_text,
                        relationship_score=relationship_score,
                        last_updated=current_time,
                    )
                    session.add(new_relationship)

                session.commit()
                logger.info(f"已更新数据库中用户关系: {user_id} -> 分数: {relationship_score:.3f}")

        except Exception as e:
            logger.error(f"更新数据库用户关系失败: {e}")

    # ===== 回复后关系追踪方法 =====

    async def track_reply_relationship(
        self, user_id: str, user_name: str, bot_reply_content: str, reply_timestamp: float
    ):
        """回复后关系追踪 - 主要入口点"""
        try:
            logger.info(f"🔄 [RelationshipTracker] 开始回复后关系追踪: {user_id}")

            # 检查上次追踪时间
            last_tracked_time = self._get_last_tracked_time(user_id)
            time_diff = reply_timestamp - last_tracked_time

            if time_diff < 5 * 60:  # 5分钟内不重复追踪
                logger.debug(
                    f"⏱️ [RelationshipTracker] 用户 {user_id} 距离上次追踪时间不足5分钟 ({time_diff:.2f}s)，跳过"
                )
                return

            # 获取上次bot回复该用户的消息
            last_bot_reply = await self._get_last_bot_reply_to_user(user_id)
            if not last_bot_reply:
                logger.info(f"👋 [RelationshipTracker] 未找到用户 {user_id} 的历史回复记录，启动'初次见面'逻辑")
                await self._handle_first_interaction(user_id, user_name, bot_reply_content)
                return

            # 获取用户后续的反应消息
            user_reactions = await self._get_user_reactions_after_reply(user_id, last_bot_reply.time)
            logger.debug(f"💬 [RelationshipTracker] 找到用户 {user_id} 在上次回复后的 {len(user_reactions)} 条反应消息")

            # 获取当前关系数据
            current_relationship = self._get_user_relationship_from_db(user_id)
            current_score = (
                current_relationship.get("relationship_score", global_config.affinity_flow.base_relationship_score)
                if current_relationship
                else global_config.affinity_flow.base_relationship_score
            )
            current_text = current_relationship.get("relationship_text", "新用户") if current_relationship else "新用户"

            # 使用LLM分析并更新关系
            logger.debug(f"🧠 [RelationshipTracker] 开始为用户 {user_id} 分析并更新关系")
            await self._analyze_and_update_relationship(
                user_id, user_name, last_bot_reply, user_reactions, current_text, current_score, bot_reply_content
            )

        except Exception as e:
            logger.error(f"回复后关系追踪失败: {e}")
            logger.debug("错误详情:", exc_info=True)

    def _get_last_tracked_time(self, user_id: str) -> float:
        """获取上次追踪时间"""
        # 先检查缓存
        if user_id in self.user_relationship_cache:
            return self.user_relationship_cache[user_id].get("last_tracked", 0)

        # 从数据库获取
        relationship_data = self._get_user_relationship_from_db(user_id)
        if relationship_data:
            return relationship_data.get("last_updated", 0)

        return 0

    async def _get_last_bot_reply_to_user(self, user_id: str) -> Optional[DatabaseMessages]:
        """获取上次bot回复该用户的消息"""
        try:
            with get_db_session() as session:
                # 查询bot回复给该用户的最新消息
                stmt = (
                    select(Messages)
                    .where(Messages.user_id == user_id)
                    .where(Messages.reply_to.isnot(None))
                    .order_by(desc(Messages.time))
                    .limit(1)
                )

                result = session.execute(stmt).scalar_one_or_none()
                if result:
                    # 将SQLAlchemy模型转换为DatabaseMessages对象
                    return self._sqlalchemy_to_database_messages(result)

        except Exception as e:
            logger.error(f"获取上次回复消息失败: {e}")

        return None

    async def _get_user_reactions_after_reply(self, user_id: str, reply_time: float) -> List[DatabaseMessages]:
        """获取用户在bot回复后的反应消息"""
        try:
            with get_db_session() as session:
                # 查询用户在回复时间之后的5分钟内的消息
                end_time = reply_time + 5 * 60  # 5分钟

                stmt = (
                    select(Messages)
                    .where(Messages.user_id == user_id)
                    .where(Messages.time > reply_time)
                    .where(Messages.time <= end_time)
                    .order_by(Messages.time)
                )

                results = session.execute(stmt).scalars().all()
                if results:
                    return [self._sqlalchemy_to_database_messages(result) for result in results]

        except Exception as e:
            logger.error(f"获取用户反应消息失败: {e}")

        return []

    def _sqlalchemy_to_database_messages(self, sqlalchemy_message) -> DatabaseMessages:
        """将SQLAlchemy消息模型转换为DatabaseMessages对象"""
        try:
            return DatabaseMessages(
                message_id=sqlalchemy_message.message_id or "",
                time=float(sqlalchemy_message.time) if sqlalchemy_message.time is not None else 0.0,
                chat_id=sqlalchemy_message.chat_id or "",
                reply_to=sqlalchemy_message.reply_to,
                processed_plain_text=sqlalchemy_message.processed_plain_text or "",
                user_id=sqlalchemy_message.user_id or "",
                user_nickname=sqlalchemy_message.user_nickname or "",
                user_platform=sqlalchemy_message.user_platform or "",
            )
        except Exception as e:
            logger.error(f"SQLAlchemy消息转换失败: {e}")
            # 返回一个基本的消息对象
            return DatabaseMessages(
                message_id="",
                time=0.0,
                chat_id="",
                processed_plain_text="",
                user_id="",
                user_nickname="",
                user_platform="",
            )

    async def _analyze_and_update_relationship(
        self,
        user_id: str,
        user_name: str,
        last_bot_reply: DatabaseMessages,
        user_reactions: List[DatabaseMessages],
        current_text: str,
        current_score: float,
        current_reply: str,
    ):
        """使用LLM分析并更新用户关系"""
        try:
            # 构建分析提示
            user_reactions_text = "\n".join([f"- {msg.processed_plain_text}" for msg in user_reactions])

            # 获取bot人设信息
            from src.individuality.individuality import Individuality

            individuality = Individuality()
            bot_personality = await individuality.get_personality_block()

            prompt = f"""
你现在是一个有着特定性格和身份的AI助手。你的人设是：{bot_personality}

请以你独特的性格视角，严格按现实逻辑分析以下用户交互，更新用户关系印象和分数：

用户信息:
- 用户ID: {user_id}
- 用户名: {user_name}

你上次的回复: {last_bot_reply.processed_plain_text}

用户反应消息:
{user_reactions_text}

你当前的回复: {current_reply}

当前关系印象: {current_text}
当前关系分数: {current_score:.3f}

【重要】关系分数档次定义：
- 0.0-0.2：陌生人/初次认识 - 仅礼貌性交流
- 0.2-0.4：普通网友 - 有基本互动但不熟悉
- 0.4-0.6：熟悉网友 - 经常交流，有一定了解
- 0.6-0.8：朋友 - 可以分享心情，互相关心
- 0.8-1.0：好朋友/知己 - 深度信任，亲密无间

【严格要求】：
1. 加分必须符合现实关系发展逻辑 - 不能因为用户反应好就盲目加分
2. 关系提升需要足够的互动积累和时间验证，单次互动加分通常不超过0.05-0.1
3. 必须考虑当前关系档次，不能跳跃式提升（比如从0.3直接到0.7）
4. 关系印象描述要详细具体（100-200字），包括：
   - 用户性格特点和交流风格观察
   - 印象深刻的互动记忆和对话片段
   - 你们关系的具体状态描述和发展阶段
   - 根据你的性格，你对用户的真实感受

性格视角深度分析:
1. 以你的性格特点，用户这次的反应给你什么感受？
2. 用户的情绪和行为符合你性格的喜好吗？具体哪些方面？
3. 从现实角度看，这次互动是否足以让关系提升到下一个档次？为什么？
4. 有什么特别值得记住的互动细节或对话内容？
5. 基于你们的互动历史，用户给你留下了哪些深刻印象？

请以JSON格式返回更新结果:
{{
    "relationship_text": "详细的关系印象描述(100-200字)，包含用户性格观察、印象深刻记忆、关系状态描述",
    "relationship_score": 0.0~1.0的新分数（必须严格符合现实逻辑）,
    "analysis_reasoning": "从你性格角度的深度分析，重点说明分数调整的现实合理性",
    "interaction_quality": "high/medium/low"
}}
"""

            # 调用LLM进行分析
            llm_response, _ = await self.relationship_llm.generate_response_async(prompt=prompt)

            if llm_response:
                import json

                try:
                    # 清理LLM响应，移除可能的格式标记
                    cleaned_response = self._clean_llm_json_response(llm_response)
                    response_data = json.loads(cleaned_response)

                    new_text = response_data.get("relationship_text", current_text)
                    new_score = max(0.0, min(1.0, float(response_data.get("relationship_score", current_score))))
                    reasoning = response_data.get("analysis_reasoning", "")
                    quality = response_data.get("interaction_quality", "medium")

                    # 更新数据库
                    self._update_user_relationship_in_db(user_id, new_text, new_score)

                    # 更新缓存
                    self.user_relationship_cache[user_id] = {
                        "relationship_text": new_text,
                        "relationship_score": new_score,
                        "last_tracked": time.time(),
                    }

                    # 如果有兴趣度评分系统，也更新内存中的关系分
                    if self.interest_scoring_system:
                        self.interest_scoring_system.update_user_relationship(user_id, new_score - current_score)

                    # 记录分析历史
                    analysis_record = {
                        "user_id": user_id,
                        "timestamp": time.time(),
                        "old_score": current_score,
                        "new_score": new_score,
                        "old_text": current_text,
                        "new_text": new_text,
                        "reasoning": reasoning,
                        "quality": quality,
                        "user_reactions_count": len(user_reactions),
                    }
                    self.relationship_history.append(analysis_record)

                    # 限制历史记录数量
                    if len(self.relationship_history) > 100:
                        self.relationship_history = self.relationship_history[-100:]

                    logger.info(f"✅ 关系分析完成: {user_id}")
                    logger.info(f"   📝 印象: '{current_text}' -> '{new_text}'")
                    logger.info(f"   💝 分数: {current_score:.3f} -> {new_score:.3f}")
                    logger.info(f"   🎯 质量: {quality}")

                except json.JSONDecodeError as e:
                    logger.error(f"LLM响应JSON解析失败: {e}")
                    logger.debug(f"LLM原始响应: {llm_response}")
            else:
                logger.warning("LLM未返回有效响应")

        except Exception as e:
            logger.error(f"关系分析失败: {e}")
            logger.debug("错误详情:", exc_info=True)

    async def _handle_first_interaction(self, user_id: str, user_name: str, bot_reply_content: str):
        """处理与用户的初次交互"""
        try:
            logger.info(f"✨ [RelationshipTracker] 正在处理与用户 {user_id} 的初次交互")

            # 获取bot人设信息
            from src.individuality.individuality import Individuality

            individuality = Individuality()
            bot_personality = await individuality.get_personality_block()

            prompt = f"""
你现在是：{bot_personality}

你正在与一个新用户进行初次有效互动。请根据你对TA的第一印象，建立初始关系档案。

用户信息:
- 用户ID: {user_id}
- 用户名: {user_name}

你的首次回复: {bot_reply_content}

【严格要求】：
1. 建立一个初始关系分数，通常在0.2-0.4之间（普通网友）。
2. 关系印象描述要简洁地记录你对用户的初步看法（50-100字）。
   - 用户名给你的感觉？
   - 你的回复是基于什么考虑？
   - 你对接下来与TA的互动有什么期待？

请以JSON格式返回结果:
{{
    "relationship_text": "简洁的初始关系印象描述(50-100字)",
    "relationship_score": 0.2~0.4的新分数,
    "analysis_reasoning": "从你性格角度说明建立此初始印象的理由"
}}
"""
            # 调用LLM进行分析
            llm_response, _ = await self.relationship_llm.generate_response_async(prompt=prompt)
            if not llm_response:
                logger.warning(f"初次交互分析时LLM未返回有效响应: {user_id}")
                return

            import json

            cleaned_response = self._clean_llm_json_response(llm_response)
            response_data = json.loads(cleaned_response)

            new_text = response_data.get("relationship_text", "初次见面")
            new_score = max(
                0.0,
                min(
                    1.0,
                    float(response_data.get("relationship_score", global_config.affinity_flow.base_relationship_score)),
                ),
            )

            # 更新数据库和缓存
            self._update_user_relationship_in_db(user_id, new_text, new_score)
            self.user_relationship_cache[user_id] = {
                "relationship_text": new_text,
                "relationship_score": new_score,
                "last_tracked": time.time(),
            }

            logger.info(f"✅ [RelationshipTracker] 已成功为新用户 {user_id} 建立初始关系档案，分数为 {new_score:.3f}")

        except Exception as e:
            logger.error(f"处理初次交互失败: {user_id}, 错误: {e}")
            logger.debug("错误详情:", exc_info=True)

    def _clean_llm_json_response(self, response: str) -> str:
        """
        清理LLM响应，移除可能的JSON格式标记

        Args:
            response: LLM原始响应

        Returns:
            清理后的JSON字符串
        """
        try:
            import re

            # 移除常见的JSON格式标记
            cleaned = response.strip()

            # 移除 ```json 或 ``` 等标记
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.MULTILINE | re.IGNORECASE)
            cleaned = re.sub(r"\s*```$", "", cleaned, flags=re.MULTILINE)

            # 移除可能的Markdown代码块标记
            cleaned = re.sub(r"^`|`$", "", cleaned, flags=re.MULTILINE)

            # 尝试找到JSON对象的开始和结束
            json_start = cleaned.find("{")
            json_end = cleaned.rfind("}")

            if json_start != -1 and json_end != -1 and json_end > json_start:
                # 提取JSON部分
                cleaned = cleaned[json_start : json_end + 1]

            # 移除多余的空白字符
            cleaned = cleaned.strip()

            logger.debug(f"LLM响应清理: 原始长度={len(response)}, 清理后长度={len(cleaned)}")
            if cleaned != response:
                logger.debug(f"清理前: {response[:200]}...")
                logger.debug(f"清理后: {cleaned[:200]}...")

            return cleaned

        except Exception as e:
            logger.warning(f"清理LLM响应失败: {e}")
            return response  # 清理失败时返回原始响应
