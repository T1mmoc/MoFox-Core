import time
from typing import Dict, Any, Tuple

from src.common.logger import get_logger
from src.chat.chat_loop.hfc_utils import CycleDetail
from .hfc_context import HfcContext

logger = get_logger("hfc")


class CycleTracker:
    def __init__(self, context: HfcContext):
        """
        初始化循环跟踪器

        Args:
            context: HFC聊天上下文对象

        功能说明:
        - 负责跟踪和记录每次思考循环的详细信息
        - 管理循环的开始、结束和信息存储
        """
        self.context = context

    def start_cycle(self, is_proactive: bool = False) -> Tuple[Dict[str, float], str]:
        """
        开始新的思考循环

        Args:
            is_proactive: 标记这个循环是否由主动思考发起

        Returns:
            tuple: (循环计时器字典, 思考ID字符串)

        功能说明:
        - 增加循环计数器
        - 创建新的循环详情对象
        - 生成唯一的思考ID
        - 初始化循环计时器
        """
        if not is_proactive:
            self.context.cycle_counter += 1

        cycle_id = self.context.cycle_counter if not is_proactive else f"{self.context.cycle_counter}.p"
        self.context.current_cycle_detail = CycleDetail(cycle_id)
        self.context.current_cycle_detail.thinking_id = f"tid{str(round(time.time(), 2))}"
        cycle_timers = {}
        return cycle_timers, self.context.current_cycle_detail.thinking_id

    def end_cycle(self, loop_info: Dict[str, Any], cycle_timers: Dict[str, float]):
        """
        结束当前思考循环

        Args:
            loop_info: 循环信息，包含规划和动作信息
            cycle_timers: 循环计时器，记录各阶段耗时

        功能说明:
        - 设置循环详情的完整信息
        - 将当前循环加入历史记录
        - 记录计时器和结束时间
        - 打印循环统计信息
        """
        if self.context.current_cycle_detail:
            self.context.current_cycle_detail.set_loop_info(loop_info)
            self.context.history_loop.append(self.context.current_cycle_detail)
            self.context.current_cycle_detail.timers = cycle_timers
            self.context.current_cycle_detail.end_time = time.time()
            self.print_cycle_info(cycle_timers)

    def print_cycle_info(self, cycle_timers: Dict[str, float]):
        """
        打印循环统计信息

        Args:
            cycle_timers: 循环计时器字典

        功能说明:
        - 格式化各阶段的耗时信息
        - 计算总体循环持续时间
        - 输出详细的性能统计日志
        - 显示选择的动作类型
        """
        if not self.context.current_cycle_detail:
            return

        timer_strings = []
        for name, elapsed in cycle_timers.items():
            formatted_time = f"{elapsed * 1000:.2f}毫秒" if elapsed < 1 else f"{elapsed:.2f}秒"
            timer_strings.append(f"{name}: {formatted_time}")

        # 获取动作类型，兼容新旧格式
        # 获取动作类型
        action_type = "未知动作"
        if self.context.current_cycle_detail:
            loop_plan_info = self.context.current_cycle_detail.loop_plan_info
            actions = loop_plan_info.get("action_result")
            
            if isinstance(actions, list) and actions:
                # 从actions列表中提取所有action_type
                action_types = [a.get("action_type", "未知") for a in actions]
                action_type = ", ".join(action_types)
            elif isinstance(actions, dict):
                # 兼容旧格式
                action_type = actions.get("action_type", "未知动作")


        if self.context.current_cycle_detail.end_time and self.context.current_cycle_detail.start_time:
            duration = self.context.current_cycle_detail.end_time - self.context.current_cycle_detail.start_time
            logger.info(
                f"{self.context.log_prefix} 第{self.context.current_cycle_detail.cycle_id}次思考,"
                f"耗时: {duration:.1f}秒, "
                f"选择动作: {action_type}" + (f"\n详情: {'; '.join(timer_strings)}" if timer_strings else "")
            )
