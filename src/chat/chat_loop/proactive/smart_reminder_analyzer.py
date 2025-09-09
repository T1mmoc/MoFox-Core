"""
智能提醒分析器

使用LLM分析用户消息，识别提醒请求并提取时间和内容信息
"""

import re
import json
from datetime import datetime, timedelta
from typing import Optional

from src.common.logger import get_logger
from src.llm_models.utils_model import LLMRequest
from src.config.config import model_config

logger = get_logger("smart_reminder")


class ReminderEvent:
    """提醒事件数据类"""
    def __init__(self, user_id: str, reminder_time: datetime, content: str, confidence: float):
        self.user_id = user_id
        self.reminder_time = reminder_time
        self.content = content
        self.confidence = confidence
        
    def __repr__(self):
        return f"ReminderEvent(user_id={self.user_id}, time={self.reminder_time}, content={self.content}, confidence={self.confidence})"
    
    def to_dict(self):
        return {
            'user_id': self.user_id,
            'reminder_time': self.reminder_time.isoformat(),
            'content': self.content,
            'confidence': self.confidence
        }


class SmartReminderAnalyzer:
    """智能提醒分析器"""
    
    def __init__(self):
        self.confidence_threshold = 0.7
        # 使用规划器模型进行分析
        self.analyzer_llm = LLMRequest(
            model_set=model_config.model_task_config.utils_small,
            request_type="reminder_analyzer"
        )
        
    async def analyze_message(self, user_id: str, message: str) -> Optional[ReminderEvent]:
        """分析消息是否包含提醒请求
        
        Args:
            user_id: 用户ID
            message: 用户消息内容
            
        Returns:
            ReminderEvent对象，如果没有检测到提醒请求则返回None
        """
        if not message or len(message.strip()) == 0:
            return None
            
        logger.debug(f"分析消息中的提醒请求: {message}")
        
        # 使用LLM分析消息
        analysis_result = await self._analyze_with_llm(message)
        
        if not analysis_result or analysis_result.get('confidence', 0) < 0.5:  # 降低置信度阈值
            return None
            
        try:
            # 解析时间
            reminder_time = self._parse_relative_time(analysis_result['relative_time'])
            if not reminder_time:
                return None
                
            # 创建提醒事件
            reminder_event = ReminderEvent(
                user_id=user_id,
                reminder_time=reminder_time,
                content=analysis_result.get('content', '提醒'),
                confidence=analysis_result['confidence']
            )
            
            logger.info(f"检测到提醒请求: {reminder_event}")
            return reminder_event
            
        except Exception as e:
            logger.error(f"创建提醒事件失败: {e}")
            return None
            
    async def _analyze_with_llm(self, message: str) -> Optional[dict]:
        """使用LLM分析消息中的提醒请求"""
        try:
            prompt = f"""分析以下消息是否包含提醒请求。

消息: {message}
当前时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

请判断用户是否想要设置提醒，如果是，请提取：
1. 是否包含提醒请求 (has_reminder: true/false)
2. 置信度 (confidence: 0.0-1.0)  
3. 相对时间表达 (relative_time: 如"3分钟后", "2小时后")
4. 提醒内容 (content: 提醒的具体内容)
5. 分析原因 (reasoning: 判断理由)

请以JSON格式输出:
{{
    "has_reminder": true/false,
    "confidence": 0.0-1.0,
    "relative_time": "时间表达",
    "content": "提醒内容", 
    "reasoning": "判断理由"
}}"""

            response, _ = await self.analyzer_llm.generate_response_async(prompt=prompt)
            if not response:
                return None
                
            # 解析JSON响应，处理可能的markdown包装
            try:
                # 清理响应文本
                cleaned_response = response.strip()
                
                # 移除markdown代码块包装
                if cleaned_response.startswith('```json'):
                    cleaned_response = cleaned_response[7:]  # 移除 ```json
                elif cleaned_response.startswith('```'):
                    cleaned_response = cleaned_response[3:]   # 移除 ```
                    
                if cleaned_response.endswith('```'):
                    cleaned_response = cleaned_response[:-3]  # 移除结尾的 ```
                    
                cleaned_response = cleaned_response.strip()
                
                # 解析JSON
                result = json.loads(cleaned_response)
                if result.get('has_reminder', False):
                    logger.info(f"LLM分析结果: {result}")
                    return result
            except json.JSONDecodeError as e:
                logger.error(f"LLM响应JSON解析失败: {response}, Error: {e}")
                # 尝试使用更宽松的JSON修复
                try:
                    import re
                    # 提取JSON部分的正则表达式
                    json_match = re.search(r'\{.*\}', cleaned_response, re.DOTALL)
                    if json_match:
                        json_str = json_match.group()
                        result = json.loads(json_str)
                        if result.get('has_reminder', False):
                            logger.info(f"备用解析成功: {result}")
                            return result
                except Exception as fallback_error:
                    logger.error(f"备用JSON解析也失败: {fallback_error}")
                
        except Exception as e:
            logger.error(f"LLM分析失败: {e}")
            
        return None
        
    def _parse_relative_time(self, time_expr: str) -> Optional[datetime]:
        """解析时间表达式（支持相对时间和绝对时间）"""
        try:
            now = datetime.now()
            
            # 1. 匹配相对时间：X分钟后，包括中文数字
            # 先尝试匹配阿拉伯数字
            minutes_match = re.search(r'(\d+)\s*分钟后', time_expr)
            if minutes_match:
                minutes = int(minutes_match.group(1))
                result = now + timedelta(minutes=minutes)
                logger.info(f"相对时间解析结果: timedelta(minutes={minutes}) -> {result}")
                return result
            
            # 匹配中文数字分钟
            chinese_minutes_patterns = [
                (r'一分钟后', 1), (r'二分钟后', 2), (r'两分钟后', 2), (r'三分钟后', 3), (r'四分钟后', 4), (r'五分钟后', 5),
                (r'六分钟后', 6), (r'七分钟后', 7), (r'八分钟后', 8), (r'九分钟后', 9), (r'十分钟后', 10),
                (r'十一分钟后', 11), (r'十二分钟后', 12), (r'十三分钟后', 13), (r'十四分钟后', 14), (r'十五分钟后', 15),
                (r'二十分钟后', 20), (r'三十分钟后', 30), (r'四十分钟后', 40), (r'五十分钟后', 50), (r'六十分钟后', 60)
            ]
            
            for pattern, minutes in chinese_minutes_patterns:
                if re.search(pattern, time_expr):
                    result = now + timedelta(minutes=minutes)
                    logger.info(f"中文时间解析结果: {pattern} -> {minutes}分钟 -> {result}")
                    return result
                
            # 2. 匹配相对时间：X小时后
            hours_match = re.search(r'(\d+)\s*小时后', time_expr)
            if hours_match:
                hours = int(hours_match.group(1))
                result = now + timedelta(hours=hours)
                logger.info(f"相对时间解析结果: timedelta(hours={hours})")
                return result
                
            # 3. 匹配相对时间：X秒后
            seconds_match = re.search(r'(\d+)\s*秒后', time_expr)
            if seconds_match:
                seconds = int(seconds_match.group(1))
                result = now + timedelta(seconds=seconds)
                logger.info(f"相对时间解析结果: timedelta(seconds={seconds})")
                return result
            
            # 4. 匹配明天+具体时间：明天下午2点、明天上午10点
            tomorrow_match = re.search(r'明天.*?(\d{1,2})\s*[点时]', time_expr)
            if tomorrow_match:
                hour = int(tomorrow_match.group(1))
                # 如果是下午且小于12，加12小时
                if '下午' in time_expr and hour < 12:
                    hour += 12
                elif '上午' in time_expr and hour == 12:
                    hour = 0
                
                tomorrow = now + timedelta(days=1)
                result = tomorrow.replace(hour=hour, minute=0, second=0, microsecond=0)
                logger.info(f"绝对时间解析结果: 明天{hour}点")
                return result
            
            # 5. 匹配今天+具体时间：今天下午3点、今天晚上8点
            today_match = re.search(r'今天.*?(\d{1,2})\s*[点时]', time_expr)
            if today_match:
                hour = int(today_match.group(1))
                # 如果是下午且小于12，加12小时
                if '下午' in time_expr and hour < 12:
                    hour += 12
                elif '晚上' in time_expr and hour < 12:
                    hour += 12
                elif '上午' in time_expr and hour == 12:
                    hour = 0
                
                result = now.replace(hour=hour, minute=0, second=0, microsecond=0)
                # 如果时间已过，设为明天
                if result <= now:
                    result += timedelta(days=1)
                    
                logger.info(f"绝对时间解析结果: 今天{hour}点")
                return result
            
            # 6. 匹配纯数字时间：14点、2点
            pure_time_match = re.search(r'(\d{1,2})\s*[点时]', time_expr)
            if pure_time_match:
                hour = int(pure_time_match.group(1))
                result = now.replace(hour=hour, minute=0, second=0, microsecond=0)
                # 如果时间已过，设为明天
                if result <= now:
                    result += timedelta(days=1)
                    
                logger.info(f"绝对时间解析结果: {hour}点")
                return result
                
        except Exception as e:
            logger.error(f"时间解析失败: {time_expr}, Error: {e}")
            
        return None


# 全局智能提醒分析器实例
smart_reminder_analyzer = SmartReminderAnalyzer()