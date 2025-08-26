# mmc/src/manager/monthly_plan_manager.py

import asyncio
from datetime import datetime, timedelta
from typing import List, Optional
import orjson
from json_repair import repair_json

from src.common.database.monthly_plan_db import (
    add_new_plans,
    get_archived_plans_for_month,
    archive_active_plans_for_month,
    has_active_plans
)
from src.config.config import global_config, model_config
from src.llm_models.utils_model import LLMRequest
from src.common.logger import get_logger
from src.manager.async_task_manager import AsyncTask, async_task_manager

logger = get_logger("monthly_plan_manager")

# 默认的月度计划生成指导原则
DEFAULT_MONTHLY_PLAN_GUIDELINES = """
我希望你能为自己制定一些有意义的月度小目标和计划。
这些计划应该涵盖学习、娱乐、社交、个人成长等各个方面。
每个计划都应该是具体可行的，能够在一个月内通过日常活动逐步实现。
请确保计划既有挑战性又不会过于繁重，保持生活的平衡和乐趣。
"""

class MonthlyPlanManager:
    """月度计划管理器
    
    负责月度计划的生成、管理和生命周期控制。
    与 ScheduleManager 解耦，专注于月度层面的计划管理。
    """
    
    def __init__(self):
        self.llm = LLMRequest(
            model_set=model_config.model_task_config.schedule_generator, 
            request_type="monthly_plan"
        )
        self.generation_running = False
        self.monthly_task_started = False

    async def start_monthly_plan_generation(self):
        """启动每月初自动生成新月度计划的任务，并在启动时检查一次"""
        if not self.monthly_task_started:
            logger.info("正在启动每月月度计划生成任务...")
            task = MonthlyPlanGenerationTask(self)
            await async_task_manager.add_task(task)
            self.monthly_task_started = True
            logger.info("每月月度计划生成任务已成功启动。")
            
            # 启动时立即检查并按需生成
            logger.info("执行启动时月度计划检查...")
            await self.ensure_and_generate_plans_if_needed()
        else:
            logger.info("每月月度计划生成任务已在运行中。")

    async def ensure_and_generate_plans_if_needed(self, target_month: Optional[str] = None) -> bool:
        """
        确保指定月份有计划，如果没有则触发生成。
        这是按需生成的主要入口点。
        """
        if target_month is None:
            target_month = datetime.now().strftime("%Y-%m")
        
        if not has_active_plans(target_month):
            logger.info(f"{target_month} 没有任何有效的月度计划，将立即生成。")
            return await self.generate_monthly_plans(target_month)
        else:
            # logger.info(f"{target_month} 已存在有效的月度计划，跳过生成。")
            return True # 已经有计划，也算成功

    async def generate_monthly_plans(self, target_month: Optional[str] = None) -> bool:
        """
        生成指定月份的月度计划
        
        :param target_month: 目标月份，格式为 "YYYY-MM"。如果为 None，则为当前月份。
        :return: 是否生成成功
        """
        if self.generation_running:
            logger.info("月度计划生成任务已在运行中，跳过重复启动")
            return False
            
        self.generation_running = True
        
        try:
            # 确定目标月份
            if target_month is None:
                target_month = datetime.now().strftime("%Y-%m")
            
            logger.info(f"开始为 {target_month} 生成月度计划...")
            
            # 检查是否启用月度计划系统
            if not global_config.monthly_plan_system or not global_config.monthly_plan_system.enable:
                logger.info("月度计划系统已禁用，跳过计划生成。")
                return False
            
            # 获取上个月的归档计划作为参考
            last_month = self._get_previous_month(target_month)
            archived_plans = get_archived_plans_for_month(last_month)
            
            # 构建生成 Prompt
            prompt = self._build_generation_prompt(target_month, archived_plans)
            
            # 调用 LLM 生成计划
            plans = await self._generate_plans_with_llm(prompt)
            
            if plans:
                # 保存到数据库
                add_new_plans(plans, target_month)
                logger.info(f"成功为 {target_month} 生成并保存了 {len(plans)} 条月度计划。")
                return True
            else:
                logger.warning(f"未能为 {target_month} 生成有效的月度计划。")
                return False
                
        except Exception as e:
            logger.error(f"生成 {target_month} 月度计划时发生错误: {e}")
            return False
        finally:
            self.generation_running = False

    def _get_previous_month(self, current_month: str) -> str:
        """获取上个月的月份字符串"""
        try:
            year, month = map(int, current_month.split('-'))
            if month == 1:
                return f"{year-1}-12"
            else:
                return f"{year}-{month-1:02d}"
        except Exception:
            # 如果解析失败，返回一个不存在的月份
            return "1900-01"

    def _build_generation_prompt(self, target_month: str, archived_plans: List) -> str:
        """构建月度计划生成的 Prompt"""
        
        # 获取配置
        guidelines = getattr(global_config.monthly_plan_system, 'guidelines', None) or DEFAULT_MONTHLY_PLAN_GUIDELINES
        personality = global_config.personality.personality_core
        personality_side = global_config.personality.personality_side
        max_plans = global_config.monthly_plan_system.max_plans_per_month
        
        # 构建上月未完成计划的参考信息
        archived_plans_block = ""
        if archived_plans:
            archived_texts = [f"- {plan.plan_text}" for plan in archived_plans[:5]]  # 最多显示5个
            archived_plans_block = f"""
**上个月未完成的一些计划（可作为参考）**:
{chr(10).join(archived_texts)}

你可以考虑是否要在这个月继续推进这些计划，或者制定全新的计划。
"""
        
        prompt = f"""
我，{global_config.bot.nickname}，需要为自己制定 {target_month} 的月度计划。

**关于我**:
- **核心人设**: {personality}
- **具体习惯与兴趣**:
{personality_side}

{archived_plans_block}

**我的月度计划制定原则**:
{guidelines}

**重要要求**:
1. 请为我生成 {max_plans} 条左右的月度计划
2. 每条计划都应该是一句话，简洁明了，具体可行
3. 计划应该涵盖不同的生活方面（学习、娱乐、社交、个人成长等）
4. 返回格式必须是纯文本，每行一条计划，不要使用 JSON 或其他格式
5. 不要包含任何解释性文字，只返回计划列表

**示例格式**:
学习一门新的编程语言或技术
每周至少看两部有趣的电影
与朋友们组织一次户外活动
阅读3本感兴趣的书籍
尝试制作一道新的料理

请你扮演我，以我的身份和兴趣，为 {target_month} 制定合适的月度计划。
"""
        
        return prompt

    async def _generate_plans_with_llm(self, prompt: str) -> List[str]:
        """使用 LLM 生成月度计划列表"""
        max_retries = 3
        
        for attempt in range(1, max_retries + 1):
            try:
                logger.info(f"正在生成月度计划 (第 {attempt} 次尝试)")
                
                response, _ = await self.llm.generate_response_async(prompt)
                
                # 解析响应
                plans = self._parse_plans_response(response)
                
                if plans:
                    logger.info(f"成功生成 {len(plans)} 条月度计划")
                    return plans
                else:
                    logger.warning(f"第 {attempt} 次生成的计划为空，继续重试...")
                    
            except Exception as e:
                logger.error(f"第 {attempt} 次生成月度计划失败: {e}")
                
            # 添加短暂延迟，避免过于频繁的请求
            if attempt < max_retries:
                await asyncio.sleep(2)
        
        logger.error("所有尝试都失败，无法生成月度计划")
        return []

    def _parse_plans_response(self, response: str) -> List[str]:
        """解析 LLM 响应，提取计划列表"""
        try:
            # 清理响应文本
            response = response.strip()
            
            # 按行分割
            lines = [line.strip() for line in response.split('\n') if line.strip()]
            
            # 过滤掉明显不是计划的行（比如包含特殊标记的行）
            plans = []
            for line in lines:
                # 跳过包含特殊标记的行
                if any(marker in line for marker in ['**', '##', '```', '---', '===', '###']):
                    continue
                
                # 移除可能的序号前缀
                line = line.lstrip('0123456789.- ')
                
                # 确保计划不为空且有意义
                if len(line) > 5 and not line.startswith(('请', '以上', '总结', '注意')):
                    plans.append(line)
            
            # 限制计划数量
            max_plans = global_config.monthly_plan_system.max_plans_per_month
            if len(plans) > max_plans:
                plans = plans[:max_plans]
            
            return plans
            
        except Exception as e:
            logger.error(f"解析月度计划响应时发生错误: {e}")
            return []

    async def archive_current_month_plans(self, target_month: Optional[str] = None):
        """
        归档当前月份的活跃计划
        
        :param target_month: 目标月份，格式为 "YYYY-MM"。如果为 None，则为当前月份。
        """
        try:
            if target_month is None:
                target_month = datetime.now().strftime("%Y-%m")
            
            logger.info(f"开始归档 {target_month} 的活跃月度计划...")
            archived_count = archive_active_plans_for_month(target_month)
            logger.info(f"成功归档了 {archived_count} 条 {target_month} 的月度计划。")
            
        except Exception as e:
            logger.error(f"归档 {target_month} 月度计划时发生错误: {e}")


class MonthlyPlanGenerationTask(AsyncTask):
    """每月初自动生成新月度计划的任务"""

    def __init__(self, monthly_plan_manager: MonthlyPlanManager):
        super().__init__(task_name="MonthlyPlanGenerationTask")
        self.monthly_plan_manager = monthly_plan_manager

    async def run(self):
        while True:
            try:
                # 计算到下个月1号凌晨的时间
                now = datetime.now()
                
                # 获取下个月的第一天
                if now.month == 12:
                    next_month = datetime(now.year + 1, 1, 1)
                else:
                    next_month = datetime(now.year, now.month + 1, 1)
                
                sleep_seconds = (next_month - now).total_seconds()
                
                logger.info(f"下一次月度计划生成任务将在 {sleep_seconds:.2f} 秒后运行 (北京时间 {next_month.strftime('%Y-%m-%d %H:%M:%S')})")
                
                # 等待直到下个月1号
                await asyncio.sleep(sleep_seconds)
                
                # 先归档上个月的计划
                last_month = (next_month - timedelta(days=1)).strftime("%Y-%m")
                await self.monthly_plan_manager.archive_current_month_plans(last_month)
                
                # 生成新月份的计划
                current_month = next_month.strftime("%Y-%m")
                logger.info(f"到达月初，开始生成 {current_month} 的月度计划...")
                await self.monthly_plan_manager.generate_monthly_plans(current_month)
                
            except asyncio.CancelledError:
                logger.info("每月月度计划生成任务被取消。")
                break
            except Exception as e:
                logger.error(f"每月月度计划生成任务发生未知错误: {e}")
                # 发生错误后，等待1小时再重试，避免频繁失败
                await asyncio.sleep(3600)


# 全局实例
monthly_plan_manager = MonthlyPlanManager()