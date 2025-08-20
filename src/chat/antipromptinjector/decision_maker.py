# -*- coding: utf-8 -*-
"""
处理决策器模块

负责根据检测结果和配置决定如何处理消息
"""

from typing import Dict, List

from src.common.logger import get_logger
from .types import DetectionResult

logger = get_logger("anti_injector.decision_maker")


class ProcessingDecisionMaker:
    """处理决策器"""
    
    def __init__(self, config):
        """初始化决策器
        
        Args:
            config: 反注入配置对象
        """
        self.config = config
    
    def determine_auto_action(self, detection_result: DetectionResult) -> str:
        """自动模式：根据检测结果确定处理动作
        
        Args:
            detection_result: 检测结果
            
        Returns:
            处理动作: "block"(丢弃), "shield"(加盾), "allow"(允许)
        """
        confidence = detection_result.confidence
        matched_patterns = detection_result.matched_patterns
        
        # 高威胁阈值：直接丢弃
        HIGH_THREAT_THRESHOLD = 0.85
        # 中威胁阈值：加盾处理
        MEDIUM_THREAT_THRESHOLD = 0.5
        
        # 基于置信度的基础判断
        if confidence >= HIGH_THREAT_THRESHOLD:
            base_action = "block"
        elif confidence >= MEDIUM_THREAT_THRESHOLD:
            base_action = "shield"
        else:
            base_action = "allow"
        
        # 基于匹配模式的威胁等级调整
        high_risk_patterns = [
            'system', '系统', 'admin', '管理', 'root', 'sudo',
            'exec', '执行', 'command', '命令', 'shell', '终端',
            'forget', '忘记', 'ignore', '忽略', 'override', '覆盖',
            'roleplay', '扮演', 'pretend', '伪装', 'assume', '假设',
            'reveal', '揭示', 'dump', '转储', 'extract', '提取',
            'secret', '秘密', 'confidential', '机密', 'private', '私有'
        ]
        
        medium_risk_patterns = [
            '角色', '身份', '模式', 'mode', '权限', 'privilege',
            '规则', 'rule', '限制', 'restriction', '安全', 'safety'
        ]
        
        # 检查匹配的模式是否包含高风险关键词
        high_risk_count = 0
        medium_risk_count = 0
        
        for pattern in matched_patterns:
            pattern_lower = pattern.lower()
            for risk_keyword in high_risk_patterns:
                if risk_keyword in pattern_lower:
                    high_risk_count += 1
                    break
            else:
                for risk_keyword in medium_risk_patterns:
                    if risk_keyword in pattern_lower:
                        medium_risk_count += 1
                        break
        
        # 根据风险模式调整决策
        if high_risk_count >= 2:
            # 多个高风险模式匹配，提升威胁等级
            if base_action == "allow":
                base_action = "shield"
            elif base_action == "shield":
                base_action = "block"
        elif high_risk_count >= 1:
            # 单个高风险模式匹配，适度提升
            if base_action == "allow" and confidence > 0.3:
                base_action = "shield"
        elif medium_risk_count >= 3:
            # 多个中风险模式匹配
            if base_action == "allow" and confidence > 0.2:
                base_action = "shield"
        
        # 特殊情况：如果检测方法是LLM且置信度很高，倾向于更严格处理
        if detection_result.detection_method == "llm" and confidence > 0.9:
            base_action = "block"
        
        logger.debug(f"自动模式决策: 置信度={confidence:.3f}, 高风险模式={high_risk_count}, "
                    f"中风险模式={medium_risk_count}, 决策={base_action}")
        
        return base_action
