import orjson
import asyncio
from datetime import datetime, time, timedelta
from typing import Optional, List, Dict, Any, TYPE_CHECKING
from lunar_python import Lunar
from pydantic import BaseModel, ValidationError, validator

from src.common.database.sqlalchemy_models import Schedule, get_db_session
from src.common.database.monthly_plan_db import (
    get_smart_plans_for_daily_schedule,
    update_plan_usage,  # 保留兼容性
)
from src.config.config import global_config, model_config
from src.llm_models.utils_model import LLMRequest
from src.common.logger import get_logger
from json_repair import repair_json
from src.manager.async_task_manager import AsyncTask, async_task_manager
from .sleep_manager import SleepManager, SleepState

if TYPE_CHECKING:
    from src.chat.chat_loop.wakeup_manager import WakeUpManager


logger = get_logger("schedule_manager")

# 默认的日程生成指导原则
DEFAULT_SCHEDULE_GUIDELINES = """
我希望你每天都能过得充实而有趣。
请确保你的日程里有学习新知识的时间，这是你成长的关键。
但也不要忘记放松，可以看看视频、听听音乐或者玩玩游戏。
晚上我希望你能多和朋友们交流，维系好彼此的关系。
另外，请保证充足的休眠时间来处理和整合一天的数据。
"""


class ScheduleItem(BaseModel):
    """单个日程项的Pydantic模型"""

    time_range: str
    activity: str

    @validator("time_range")
    def validate_time_range(cls, v):
        """验证时间范围格式"""
        if not v or "-" not in v:
            raise ValueError("时间范围必须包含'-'分隔符")

        try:
            start_str, end_str = v.split("-", 1)
            start_str = start_str.strip()
            end_str = end_str.strip()

            # 验证时间格式
            datetime.strptime(start_str, "%H:%M")
            datetime.strptime(end_str, "%H:%M")

            return v
        except ValueError as e:
            raise ValueError(f"时间格式无效，应为HH:MM-HH:MM格式: {e}") from e

    @validator("activity")
    def validate_activity(cls, v):
        """验证活动描述"""
        if not v or not v.strip():
            raise ValueError("活动描述不能为空")
        return v.strip()


class ScheduleData(BaseModel):
    """完整日程数据的Pydantic模型"""

    schedule: List[ScheduleItem]

    @validator("schedule")
    def validate_schedule_completeness(cls, v):
        """验证日程是否覆盖24小时"""
        if not v:
            raise ValueError("日程不能为空")

        # 收集所有时间段
        time_ranges = []
        for item in v:
            try:
                start_str, end_str = item.time_range.split("-", 1)
                start_time = datetime.strptime(start_str.strip(), "%H:%M").time()
                end_time = datetime.strptime(end_str.strip(), "%H:%M").time()
                time_ranges.append((start_time, end_time))
            except ValueError:
                continue

        # 检查是否覆盖24小时
        if not cls._check_24_hour_coverage(time_ranges):
            raise ValueError("日程必须覆盖完整的24小时")

        return v

    @staticmethod
    def _check_24_hour_coverage(time_ranges: List[tuple]) -> bool:
        """检查时间段是否覆盖24小时"""
        if not time_ranges:
            return False

        # 将时间转换为分钟数进行计算
        def time_to_minutes(t: time) -> int:
            return t.hour * 60 + t.minute

        # 创建覆盖情况数组 (1440分钟 = 24小时)
        covered = [False] * 1440

        for start_time, end_time in time_ranges:
            start_min = time_to_minutes(start_time)
            end_min = time_to_minutes(end_time)

            if start_min <= end_min:
                # 同一天内的时间段
                for i in range(start_min, end_min):
                    if i < 1440:
                        covered[i] = True
            else:
                # 跨天的时间段
                for i in range(start_min, 1440):
                    covered[i] = True
                for i in range(0, end_min):
                    covered[i] = True

        # 检查是否所有分钟都被覆盖
        return all(covered)


class ScheduleManager:
    def __init__(self):
        self.today_schedule: Optional[List[Dict[str, Any]]] = None
        self.llm = LLMRequest(model_set=model_config.model_task_config.schedule_generator, request_type="schedule")
        self.max_retries = -1  # 无限重试，直到成功生成标准日程表
        self.daily_task_started = False
        self.schedule_generation_running = False  # 防止重复生成任务
        self.sleep_manager = SleepManager(self)

    async def start_daily_schedule_generation(self):
        """启动每日零点自动生成新日程的任务"""
        if not self.daily_task_started:
            logger.info("正在启动每日日程生成任务...")
            task = DailyScheduleGenerationTask(self)
            await async_task_manager.add_task(task)
            self.daily_task_started = True
            logger.info("每日日程生成任务已成功启动。")
        else:
            logger.info("每日日程生成任务已在运行中。")

    async def load_or_generate_today_schedule(self):
        # 检查是否启用日程管理功能
        if not global_config.schedule.enable:
            logger.info("日程管理功能已禁用，跳过日程加载和生成。")
            return

        today_str = datetime.now().strftime("%Y-%m-%d")
        try:
            with get_db_session() as session:
                schedule_record = session.query(Schedule).filter(Schedule.date == today_str).first()
                if schedule_record:
                    logger.info(f"从数据库加载今天的日程 ({today_str})。")

                    try:
                        schedule_data = orjson.loads(str(schedule_record.schedule_data))

                        # 使用Pydantic验证日程数据
                        if self._validate_schedule_with_pydantic(schedule_data):
                            self.today_schedule = schedule_data
                            schedule_str = f"已成功加载今天的日程 ({today_str})：\n"
                            if self.today_schedule:
                                for item in self.today_schedule:
                                    schedule_str += f"  - {item.get('time_range', '未知时间')}: {item.get('activity', '未知活动')}\n"
                            logger.info(schedule_str)
                        else:
                            logger.warning("数据库中的日程数据格式无效，将异步重新生成日程")
                            await self.generate_and_save_schedule()
                    except orjson.JSONDecodeError as e:
                        logger.error(f"日程数据JSON解析失败: {e}，将异步重新生成日程")
                        await self.generate_and_save_schedule()
                else:
                    logger.info(f"数据库中未找到今天的日程 ({today_str})，将异步调用 LLM 生成。")
                    await self.generate_and_save_schedule()
        except Exception as e:
            logger.error(f"加载或生成日程时出错: {e}")
            # 出错时也尝试异步生成
            logger.info("尝试异步生成日程作为备用方案...")
            await self.generate_and_save_schedule()

    async def generate_and_save_schedule(self):
        """启动异步日程生成任务，避免阻塞主程序"""
        if self.schedule_generation_running:
            logger.info("日程生成任务已在运行中，跳过重复启动")
            return

        # 创建异步任务进行日程生成，不阻塞主程序
        asyncio.create_task(self._async_generate_and_save_schedule())
        logger.info("已启动异步日程生成任务")

    async def _async_generate_and_save_schedule(self):
        """异步生成并保存日程的内部方法"""
        self.schedule_generation_running = True

        try:
            now = datetime.now()
            today_str = now.strftime("%Y-%m-%d")
            current_month_str = now.strftime("%Y-%m")
            weekday = now.strftime("%A")

            # 新增：获取节日信息
            lunar = Lunar.fromDate(now)
            festivals = lunar.getFestivals()
            other_festivals = lunar.getOtherFestivals()
            all_festivals = festivals + other_festivals

            festival_block = ""
            if all_festivals:
                festival_text = "、".join(all_festivals)
                festival_block = f"**今天也是一个特殊的日子: {festival_text}！请在日程中考虑和庆祝这个节日。**"

            # 获取月度计划作为额外参考
            monthly_plans_block = ""
            used_plan_ids = []
            if global_config.monthly_plan_system and global_config.monthly_plan_system.enable:
                # 使用新的智能抽取逻辑
                avoid_days = getattr(global_config.monthly_plan_system, "avoid_repetition_days", 7)
                # 使用新的智能抽取逻辑
                avoid_days = getattr(global_config.monthly_plan_system, "avoid_repetition_days", 7)
                sampled_plans = get_smart_plans_for_daily_schedule(
                    current_month_str, max_count=3, avoid_days=avoid_days
                )

                # 如果计划耗尽，则触发补充生成
                if not sampled_plans:
                    logger.info("可用的月度计划已耗尽或不足，触发后台补充生成...")
                    from mmc.src.schedule.monthly_plan_manager import monthly_plan_manager
                    
                    # 以非阻塞方式触发月度计划生成
                    monthly_plan_manager.trigger_generate_monthly_plans(current_month_str)
                    
                    # 注意：这里不再等待生成结果，因此后续代码不会立即获得新计划。
                    # 日程将基于当前可用的信息生成，新计划将在下一次日程生成时可用。
                    logger.info("月度计划的后台生成任务已启动，本次日程将不包含新计划。")

                if sampled_plans:
                    plan_texts = "\n".join([f"- {plan.plan_text}" for plan in sampled_plans])
                    monthly_plans_block = f"""
**我这个月的一些小目标/计划 (请在今天的日程中适当体现)**:
{plan_texts}
"""

            guidelines = global_config.schedule.guidelines or DEFAULT_SCHEDULE_GUIDELINES
            personality = global_config.personality.personality_core
            personality_side = global_config.personality.personality_side

            base_prompt = f"""
我，{global_config.bot.nickname}，需要为自己规划一份今天（{today_str}，星期{weekday}）的详细日程安排。
{festival_block}
**关于我**:
- **核心人设**: {personality}
- **具体习惯与兴趣**:
{personality_side}
{monthly_plans_block}
**我今天的规划原则**:
{guidelines}

**重要要求**:
1. 必须返回一个完整的、有效的JSON数组格式
2. 数组中的每个对象都必须包含 "time_range" 和 "activity" 两个键
3. 时间范围必须覆盖全部24小时，不能有遗漏
4. time_range格式必须为 "HH:MM-HH:MM" (24小时制)
5. 相邻的时间段必须连续，不能有间隙
6. 不要包含任何JSON以外的解释性文字或代码块标记
**示例**:
[
    {{"time_range": "00:00-07:00", "activity": "进入梦乡，处理数据"}},
    {{"time_range": "07:00-08:00", "activity": "起床伸个懒腰，看看今天有什么新闻"}},
    {{"time_range": "08:00-09:00", "activity": "享用早餐，规划今天的任务"}},
    {{"time_range": "09:00-23:30", "activity": "其他活动"}},
    {{"time_range": "23:30-00:00", "activity": "准备休眠"}}
]

请你扮演我，以我的身份和口吻，为我生成一份完整的24小时日程表。
"""

            # 无限重试直到生成成功的标准日程表
            attempt = 0
            while True:
                attempt += 1
                try:
                    logger.info(f"正在生成日程 (第 {attempt} 次尝试)")

                    # 构建当前尝试的prompt，增加压力提示
                    prompt = base_prompt
                    if attempt > 1:
                        failure_hint = f"""

**重要提醒 (第{attempt}次尝试)**:
- 前面{attempt - 1}次生成都失败了，请务必严格按照要求生成完整的24小时日程
- 确保JSON格式正确，所有时间段连续覆盖24小时
- 时间格式必须为HH:MM-HH:MM，不能有时间间隙或重叠
- 不要输出任何解释文字，只输出纯JSON数组
- 确保输出完整，不要被截断
"""
                        prompt += failure_hint

                    response, _ = await self.llm.generate_response_async(prompt)

                    # 尝试解析和验证JSON（项目内置的反截断机制会自动处理截断问题）
                    schedule_data = orjson.loads(repair_json(response))

                    # 使用Pydantic验证生成的日程数据
                    if self._validate_schedule_with_pydantic(schedule_data):
                        # 验证通过，保存到数据库
                        with get_db_session() as session:
                            # 检查是否已存在今天的日程
                            existing_schedule = session.query(Schedule).filter(Schedule.date == today_str).first()
                            if existing_schedule:
                                # 更新现有日程
                                session.query(Schedule).filter(Schedule.date == today_str).update(
                                    {
                                        Schedule.schedule_data: orjson.dumps(schedule_data).decode("utf-8"),
                                        Schedule.updated_at: datetime.now(),
                                    }
                                )
                            else:
                                # 创建新日程
                                new_schedule = Schedule(
                                    date=today_str, schedule_data=orjson.dumps(schedule_data).decode("utf-8")
                                )
                                session.add(new_schedule)
                            session.commit()

                        # 美化输出
                        schedule_str = f"✅ 经过 {attempt} 次尝试，成功生成并保存今天的日程 ({today_str})：\n"
                        for item in schedule_data:
                            schedule_str += (
                                f"  - {item.get('time_range', '未知时间')}: {item.get('activity', '未知活动')}\n"
                            )
                        logger.info(schedule_str)

                        self.today_schedule = schedule_data

                        # 成功生成日程后，更新使用过的月度计划的统计信息
                        if used_plan_ids and global_config.monthly_plan_system:
                            logger.info(f"更新使用过的月度计划 {used_plan_ids} 的统计信息。")
                            update_plan_usage(used_plan_ids, today_str)  # type: ignore

                        # 成功生成，退出无限循环
                        break

                    else:
                        logger.warning(f"第 {attempt} 次生成的日程验证失败，继续重试...")
                        # 添加短暂延迟，避免过于频繁的请求
                        await asyncio.sleep(2)

                except Exception as e:
                    logger.error(f"第 {attempt} 次生成日程失败: {e}")
                    logger.info("继续重试...")
                    # 添加短暂延迟，避免过于频繁的请求
                    await asyncio.sleep(3)

        finally:
            self.schedule_generation_running = False
            logger.info("日程生成任务结束")

    def get_current_activity(self) -> Optional[str]:
        # 检查是否启用日程管理功能
        if not global_config.schedule.enable:
            return None

        if not self.today_schedule:
            return None

        now = datetime.now().time()
        for event in self.today_schedule:
            try:
                time_range = event.get("time_range")
                activity = event.get("activity")

                if not time_range or not activity:
                    logger.warning(f"日程事件缺少必要字段: {event}")
                    continue

                start_str, end_str = time_range.split("-")
                start_time = datetime.strptime(start_str.strip(), "%H:%M").time()
                end_time = datetime.strptime(end_str.strip(), "%H:%M").time()

                if start_time <= end_time:
                    if start_time <= now < end_time:
                        return activity
                else:  # 跨天事件
                    if now >= start_time or now < end_time:
                        return activity
            except (ValueError, KeyError, AttributeError) as e:
                logger.warning(f"解析日程事件失败: {event}, 错误: {e}")
                continue
        return None

    def get_current_sleep_state(self) -> SleepState:
        """获取当前的睡眠状态"""
        return self.sleep_manager.get_current_sleep_state()

    def is_sleeping(self) -> bool:
        """检查当前是否处于正式休眠状态"""
        return self.sleep_manager.is_sleeping()

    async def update_sleep_state(self, wakeup_manager: Optional["WakeUpManager"] = None):
        """更新睡眠状态"""
        await self.sleep_manager.update_sleep_state(wakeup_manager)

    def reset_sleep_state_after_wakeup(self):
        """被唤醒后，将状态切换到 WOKEN_UP"""
        self.sleep_manager.reset_sleep_state_after_wakeup()

    def _validate_schedule_with_pydantic(self, schedule_data) -> bool:
        """使用Pydantic验证日程数据格式和完整性"""
        try:
            # 尝试用Pydantic模型验证
            ScheduleData(schedule=schedule_data)
            logger.info("日程数据Pydantic验证通过")
            return True
        except ValidationError as e:
            logger.warning(f"日程数据Pydantic验证失败: {e}")
            return False
        except Exception as e:
            logger.error(f"日程数据验证时发生异常: {e}")
            return False

    def _validate_schedule_data(self, schedule_data) -> bool:
        """保留原有的基础验证方法作为备用"""
        if not isinstance(schedule_data, list):
            logger.warning("日程数据不是列表格式")
            return False

        for item in schedule_data:
            if not isinstance(item, dict):
                logger.warning(f"日程项不是字典格式: {item}")
                return False

            if "time_range" not in item or "activity" not in item:
                logger.warning(f"日程项缺少必要字段 (time_range 或 activity): {item}")
                return False

            if not isinstance(item["time_range"], str) or not isinstance(item["activity"], str):
                logger.warning(f"日程项字段类型不正确: {item}")
                return False

        return True


class DailyScheduleGenerationTask(AsyncTask):
    """每日零点自动生成新日程的任务"""

    def __init__(self, schedule_manager: "ScheduleManager"):
        super().__init__(task_name="DailyScheduleGenerationTask")
        self.schedule_manager = schedule_manager

    async def run(self):
        while True:
            try:
                # 1. 计算到下一个零点的时间
                now = datetime.now()
                tomorrow = now.date() + timedelta(days=1)
                midnight = datetime.combine(tomorrow, time.min)
                sleep_seconds = (midnight - now).total_seconds()

                logger.info(
                    f"下一次日程生成任务将在 {sleep_seconds:.2f} 秒后运行 (北京时间 {midnight.strftime('%Y-%m-%d %H:%M:%S')})"
                )

                # 2. 等待直到零点
                await asyncio.sleep(sleep_seconds)

                # 3. 执行异步日程生成
                logger.info("到达每日零点，开始异步生成新的一天日程...")
                await self.schedule_manager.generate_and_save_schedule()

            except asyncio.CancelledError:
                logger.info("每日日程生成任务被取消。")
                break
            except Exception as e:
                logger.error(f"每日日程生成任务发生未知错误: {e}")
                # 发生错误后，等待5分钟再重试，避免频繁失败
                await asyncio.sleep(300)


schedule_manager = ScheduleManager()
