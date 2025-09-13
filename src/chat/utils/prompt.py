"""
统一提示词系统 - 合并模板管理和智能构建功能
将原有的Prompt类和SmartPrompt功能整合为一个真正的Prompt类
"""

import re
import asyncio
import time
import contextvars
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, List, Literal, Tuple
from contextlib import asynccontextmanager

from rich.traceback import install
from src.common.logger import get_logger
from src.config.config import global_config
from src.chat.utils.chat_message_builder import build_readable_messages
from src.chat.message_receive.chat_stream import get_chat_manager
from src.person_info.person_info import get_person_info_manager

install(extra_lines=3)
logger = get_logger("unified_prompt")


@dataclass
class PromptParameters:
    """统一提示词参数系统"""
    
    # 基础参数
    chat_id: str = ""
    is_group_chat: bool = False
    sender: str = ""
    target: str = ""
    reply_to: str = ""
    extra_info: str = ""
    prompt_mode: Literal["s4u", "normal", "minimal"] = "s4u"
    
    # 功能开关
    enable_tool: bool = True
    enable_memory: bool = True
    enable_expression: bool = True
    enable_relation: bool = True
    enable_cross_context: bool = True
    enable_knowledge: bool = True
    
    # 性能控制
    max_context_messages: int = 50
    
    # 调试选项
    debug_mode: bool = False
    
    # 聊天历史和上下文
    chat_target_info: Optional[Dict[str, Any]] = None
    message_list_before_now_long: List[Dict[str, Any]] = field(default_factory=list)
    message_list_before_short: List[Dict[str, Any]] = field(default_factory=list)
    chat_talking_prompt_short: str = ""
    target_user_info: Optional[Dict[str, Any]] = None
    
    # 已构建的内容块
    expression_habits_block: str = ""
    relation_info_block: str = ""
    memory_block: str = ""
    tool_info_block: str = ""
    knowledge_prompt: str = ""
    cross_context_block: str = ""
    
    # 其他内容块
    keywords_reaction_prompt: str = ""
    extra_info_block: str = ""
    time_block: str = ""
    identity_block: str = ""
    schedule_block: str = ""
    moderation_prompt_block: str = ""
    safety_guidelines_block: str = ""
    reply_target_block: str = ""
    mood_prompt: str = ""
    action_descriptions: str = ""
    
    # 可用动作信息
    available_actions: Optional[Dict[str, Any]] = None
    
    def validate(self) -> List[str]:
        """参数验证"""
        errors = []
        if not self.chat_id:
            errors.append("chat_id不能为空")
        if self.prompt_mode not in ["s4u", "normal", "minimal"]:
            errors.append("prompt_mode必须是's4u'、'normal'或'minimal'")
        if self.max_context_messages <= 0:
            errors.append("max_context_messages必须大于0")
        return errors


class PromptContext:
    """提示词上下文管理器"""
    
    def __init__(self):
        self._context_prompts: Dict[str, Dict[str, "Prompt"]] = {}
        self._current_context_var = contextvars.ContextVar("current_context", default=None)
        self._context_lock = asyncio.Lock()
    
    @property
    def _current_context(self) -> Optional[str]:
        """获取当前协程的上下文ID"""
        return self._current_context_var.get()
    
    @_current_context.setter
    def _current_context(self, value: Optional[str]):
        """设置当前协程的上下文ID"""
        self._current_context_var.set(value)  # type: ignore
    
    @asynccontextmanager
    async def async_scope(self, context_id: Optional[str] = None):
        """创建一个异步的临时提示模板作用域"""
        if context_id is not None:
            try:
                await asyncio.wait_for(self._context_lock.acquire(), timeout=5.0)
                try:
                    if context_id not in self._context_prompts:
                        self._context_prompts[context_id] = {}
                finally:
                    self._context_lock.release()
            except asyncio.TimeoutError:
                logger.warning(f"获取上下文锁超时，context_id: {context_id}")
                context_id = None
            
            previous_context = self._current_context
            token = self._current_context_var.set(context_id) if context_id else None
        else:
            previous_context = self._current_context
            token = None
        
        try:
            yield self
        finally:
            if context_id is not None and token is not None:
                try:
                    self._current_context_var.reset(token)
                except Exception as e:
                    logger.warning(f"恢复上下文时出错: {e}")
                    try:
                        self._current_context = previous_context
                    except Exception:
                        ...
    
    async def get_prompt_async(self, name: str) -> Optional["Prompt"]:
        """异步获取当前作用域中的提示模板"""
        async with self._context_lock:
            current_context = self._current_context
            logger.debug(f"获取提示词: {name} 当前上下文: {current_context}")
            if (
                current_context
                and current_context in self._context_prompts
                and name in self._context_prompts[current_context]
            ):
                return self._context_prompts[current_context][name]
            return None
    
    async def register_async(self, prompt: "Prompt", context_id: Optional[str] = None) -> None:
        """异步注册提示模板到指定作用域"""
        async with self._context_lock:
            if target_context := context_id or self._current_context:
                if prompt.name:
                    self._context_prompts.setdefault(target_context, {})[prompt.name] = prompt


class PromptManager:
    """统一提示词管理器"""
    
    def __init__(self):
        self._prompts = {}
        self._counter = 0
        self._context = PromptContext()
        self._lock = asyncio.Lock()
    
    @asynccontextmanager
    async def async_message_scope(self, message_id: Optional[str] = None):
        """为消息处理创建异步临时作用域"""
        async with self._context.async_scope(message_id):
            yield self
    
    async def get_prompt_async(self, name: str) -> "Prompt":
        """异步获取提示模板"""
        context_prompt = await self._context.get_prompt_async(name)
        if context_prompt is not None:
            logger.debug(f"从上下文中获取提示词: {name} {context_prompt}")
            return context_prompt
        
        async with self._lock:
            if name not in self._prompts:
                raise KeyError(f"Prompt '{name}' not found")
            return self._prompts[name]
    
    def generate_name(self, template: str) -> str:
        """为未命名的prompt生成名称"""
        self._counter += 1
        return f"prompt_{self._counter}"
    
    def register(self, prompt: "Prompt") -> None:
        """注册一个prompt"""
        if not prompt.name:
            prompt.name = self.generate_name(prompt.template)
        self._prompts[prompt.name] = prompt
    
    def add_prompt(self, name: str, fstr: str) -> "Prompt":
        """添加新提示模板"""
        prompt = Prompt(fstr, name=name)
        if prompt.name:
            self._prompts[prompt.name] = prompt
        return prompt
    
    async def format_prompt(self, name: str, **kwargs) -> str:
        """格式化提示模板"""
        prompt = await self.get_prompt_async(name)
        result = prompt.format(**kwargs)
        return result


# 全局单例
global_prompt_manager = PromptManager()


class Prompt:
    """
    统一提示词类 - 合并模板管理和智能构建功能
    真正的Prompt类，支持模板管理和智能上下文构建
    """
    
    # 临时标记，作为类常量
    _TEMP_LEFT_BRACE = "__ESCAPED_LEFT_BRACE__"
    _TEMP_RIGHT_BRACE = "__ESCAPED_RIGHT_BRACE__"
    
    def __init__(
        self,
        template: str,
        name: Optional[str] = None,
        parameters: Optional[PromptParameters] = None,
        should_register: bool = True
    ):
        """
        初始化统一提示词
        
        Args:
            template: 提示词模板字符串
            name: 提示词名称
            parameters: 构建参数
            should_register: 是否自动注册到全局管理器
        """
        self.template = template
        self.name = name
        self.parameters = parameters or PromptParameters()
        self.args = self._parse_template_args(template)
        self._formatted_result = ""
        
        # 预处理模板中的转义花括号
        self._processed_template = self._process_escaped_braces(template)
        
        # 自动注册
        if should_register and not global_prompt_manager._context._current_context:
            global_prompt_manager.register(self)
    
    @staticmethod
    def _process_escaped_braces(template) -> str:
        """处理模板中的转义花括号"""
        if isinstance(template, list):
            template = "\n".join(str(item) for item in template)
        elif not isinstance(template, str):
            template = str(template)
        
        return template.replace("\\{", Prompt._TEMP_LEFT_BRACE).replace("\\}", Prompt._TEMP_RIGHT_BRACE)
    
    @staticmethod
    def _restore_escaped_braces(template: str) -> str:
        """将临时标记还原为实际的花括号字符"""
        return template.replace(Prompt._TEMP_LEFT_BRACE, "{").replace(Prompt._TEMP_RIGHT_BRACE, "}")
    
    def _parse_template_args(self, template: str) -> List[str]:
        """解析模板参数"""
        template_args = []
        processed_template = self._process_escaped_braces(template)
        result = re.findall(r"\{(.*?)}", processed_template)
        for expr in result:
            if expr and expr not in template_args:
                template_args.append(expr)
        return template_args
    
    async def build(self) -> str:
        """
        构建完整的提示词，包含智能上下文
        
        Returns:
            str: 构建完成的提示词文本
        """
        # 参数验证
        errors = self.parameters.validate()
        if errors:
            logger.error(f"参数验证失败: {', '.join(errors)}")
            raise ValueError(f"参数验证失败: {', '.join(errors)}")
        
        start_time = time.time()
        try:
            # 构建上下文数据
            context_data = await self._build_context_data()
            
            # 格式化模板
            result = await self._format_with_context(context_data)
            
            total_time = time.time() - start_time
            logger.debug(f"Prompt构建完成，模式: {self.parameters.prompt_mode}, 耗时: {total_time:.2f}s")
            
            self._formatted_result = result
            return result
            
        except asyncio.TimeoutError as e:
            logger.error(f"构建Prompt超时: {e}")
            raise TimeoutError(f"构建Prompt超时: {e}") from e
        except Exception as e:
            logger.error(f"构建Prompt失败: {e}")
            raise RuntimeError(f"构建Prompt失败: {e}") from e
    
    async def _build_context_data(self) -> Dict[str, Any]:
        """构建智能上下文数据"""
        # 并行执行所有构建任务
        start_time = time.time()
        
        try:
            # 准备构建任务
            tasks = []
            task_names = []
            
            # 初始化预构建参数
            pre_built_params = {}
            if self.parameters.expression_habits_block:
                pre_built_params["expression_habits_block"] = self.parameters.expression_habits_block
            if self.parameters.relation_info_block:
                pre_built_params["relation_info_block"] = self.parameters.relation_info_block
            if self.parameters.memory_block:
                pre_built_params["memory_block"] = self.parameters.memory_block
            if self.parameters.tool_info_block:
                pre_built_params["tool_info_block"] = self.parameters.tool_info_block
            if self.parameters.knowledge_prompt:
                pre_built_params["knowledge_prompt"] = self.parameters.knowledge_prompt
            if self.parameters.cross_context_block:
                pre_built_params["cross_context_block"] = self.parameters.cross_context_block
            
            # 根据参数确定要构建的项
            if self.parameters.enable_expression and not pre_built_params.get("expression_habits_block"):
                tasks.append(self._build_expression_habits())
                task_names.append("expression_habits")
            
            if self.parameters.enable_memory and not pre_built_params.get("memory_block"):
                tasks.append(self._build_memory_block())
                task_names.append("memory_block")
            
            if self.parameters.enable_relation and not pre_built_params.get("relation_info_block"):
                tasks.append(self._build_relation_info())
                task_names.append("relation_info")
            
            if self.parameters.enable_tool and not pre_built_params.get("tool_info_block"):
                tasks.append(self._build_tool_info())
                task_names.append("tool_info")
            
            if self.parameters.enable_knowledge and not pre_built_params.get("knowledge_prompt"):
                tasks.append(self._build_knowledge_info())
                task_names.append("knowledge_info")
            
            if self.parameters.enable_cross_context and not pre_built_params.get("cross_context_block"):
                tasks.append(self._build_cross_context())
                task_names.append("cross_context")
            
            # 性能优化
            base_timeout = 10.0
            task_timeout = 2.0
            timeout_seconds = min(
                max(base_timeout, len(tasks) * task_timeout),
                30.0,
            )
            
            max_concurrent_tasks = 5
            if len(tasks) > max_concurrent_tasks:
                results = []
                for i in range(0, len(tasks), max_concurrent_tasks):
                    batch_tasks = tasks[i : i + max_concurrent_tasks]
                    
                    batch_results = await asyncio.wait_for(
                        asyncio.gather(*batch_tasks, return_exceptions=True), timeout=timeout_seconds
                    )
                    results.extend(batch_results)
            else:
                results = await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True), timeout=timeout_seconds
                )
            
            # 处理结果
            context_data = {}
            for i, result in enumerate(results):
                task_name = task_names[i] if i < len(task_names) else f"task_{i}"
                
                if isinstance(result, Exception):
                    logger.error(f"构建任务{task_name}失败: {str(result)}")
                elif isinstance(result, dict):
                    context_data.update(result)
            
            # 添加预构建的参数
            for key, value in pre_built_params.items():
                if value:
                    context_data[key] = value
            
        except asyncio.TimeoutError:
            logger.error(f"构建超时 ({timeout_seconds}s)")
            context_data = {}
            for key, value in pre_built_params.items():
                if value:
                    context_data[key] = value
        
        # 构建聊天历史
        if self.parameters.prompt_mode == "s4u":
            await self._build_s4u_chat_context(context_data)
        else:
            await self._build_normal_chat_context(context_data)
        
        # 补充基础信息
        context_data.update({
            "keywords_reaction_prompt": self.parameters.keywords_reaction_prompt,
            "extra_info_block": self.parameters.extra_info_block,
            "time_block": self.parameters.time_block or f"当前时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
            "identity": self.parameters.identity_block,
            "schedule_block": self.parameters.schedule_block,
            "moderation_prompt": self.parameters.moderation_prompt_block,
            "reply_target_block": self.parameters.reply_target_block,
            "mood_state": self.parameters.mood_prompt,
            "action_descriptions": self.parameters.action_descriptions,
        })
        
        total_time = time.time() - start_time
        logger.debug(f"上下文构建完成，总耗时: {total_time:.2f}s")
        
        return context_data
    
    async def _build_s4u_chat_context(self, context_data: Dict[str, Any]) -> None:
        """构建S4U模式的聊天上下文"""
        if not self.parameters.message_list_before_now_long:
            return
        
        core_dialogue, background_dialogue = await self._build_s4u_chat_history_prompts(
            self.parameters.message_list_before_now_long,
            self.parameters.target_user_info.get("user_id") if self.parameters.target_user_info else "",
            self.parameters.sender
        )
        
        context_data["core_dialogue_prompt"] = core_dialogue
        context_data["background_dialogue_prompt"] = background_dialogue
    
    async def _build_normal_chat_context(self, context_data: Dict[str, Any]) -> None:
        """构建normal模式的聊天上下文"""
        if not self.parameters.chat_talking_prompt_short:
            return
        
        context_data["chat_info"] = f"""群里的聊天内容：
{self.parameters.chat_talking_prompt_short}"""
    
    async def _build_s4u_chat_history_prompts(
        self, message_list_before_now: List[Dict[str, Any]], target_user_id: str, sender: str
    ) -> Tuple[str, str]:
        """构建S4U风格的分离对话prompt"""
        # 实现逻辑与原有SmartPromptBuilder相同
        core_dialogue_list = []
        bot_id = str(global_config.bot.qq_account)
        
        for msg_dict in message_list_before_now:
            try:
                msg_user_id = str(msg_dict.get("user_id"))
                reply_to = msg_dict.get("reply_to", "")
                platform, reply_to_user_id = Prompt.parse_reply_target(reply_to)
                if (msg_user_id == bot_id and reply_to_user_id == target_user_id) or msg_user_id == target_user_id:
                    core_dialogue_list.append(msg_dict)
            except Exception as e:
                logger.error(f"处理消息记录时出错: {msg_dict}, 错误: {e}")
        
        # 构建背景对话 prompt
        all_dialogue_prompt = ""
        if message_list_before_now:
            latest_25_msgs = message_list_before_now[-int(global_config.chat.max_context_size) :]
            all_dialogue_prompt_str = build_readable_messages(
                latest_25_msgs,
                replace_bot_name=True,
                timestamp_mode="normal",
                truncate=True,
            )
            all_dialogue_prompt = f"所有用户的发言：\n{all_dialogue_prompt_str}"
        
        # 构建核心对话 prompt
        core_dialogue_prompt = ""
        if core_dialogue_list:
            latest_5_messages = core_dialogue_list[-5:] if len(core_dialogue_list) >= 5 else core_dialogue_list
            has_bot_message = any(str(msg.get("user_id")) == bot_id for msg in latest_5_messages)
            
            if not has_bot_message:
                core_dialogue_prompt = ""
            else:
                core_dialogue_list = core_dialogue_list[-int(global_config.chat.max_context_size * 2) :]
                
                core_dialogue_prompt_str = build_readable_messages(
                    core_dialogue_list,
                    replace_bot_name=True,
                    merge_messages=False,
                    timestamp_mode="normal_no_YMD",
                    read_mark=0.0,
                    truncate=True,
                    show_actions=True,
                )
                core_dialogue_prompt = f"""--------------------------------
这是你和{sender}的对话，你们正在交流中：
{core_dialogue_prompt_str}
--------------------------------
"""
        
        return core_dialogue_prompt, all_dialogue_prompt
    
    async def _build_expression_habits(self) -> Dict[str, Any]:
        """构建表达习惯"""
        if not global_config.expression.enable_expression:
            return {"expression_habits_block": ""}
        
        try:
            from src.chat.express.expression_selector import ExpressionSelector
            
            # 获取聊天历史用于表情选择
            chat_history = ""
            if self.parameters.message_list_before_now_long:
                recent_messages = self.parameters.message_list_before_now_long[-10:]
                chat_history = build_readable_messages(
                    recent_messages,
                    replace_bot_name=True,
                    timestamp_mode="normal",
                    truncate=True
                )
            
            # 创建表情选择器
            expression_selector = ExpressionSelector(self.parameters.chat_id)
            
            # 选择合适的表情
            selected_expressions = await expression_selector.select_suitable_expressions_llm(
                chat_history=chat_history,
                current_message=self.parameters.target,
                emotional_tone="neutral",
                topic_type="general"
            )
            
            # 构建表达习惯块
            if selected_expressions:
                style_habits_str = "\n".join([f"- {expr}" for expr in selected_expressions])
                expression_habits_block = f"- 你可以参考以下的语言习惯，当情景合适就使用，但不要生硬使用，以合理的方式结合到你的回复中：\n{style_habits_str}"
            else:
                expression_habits_block = ""
            
            return {"expression_habits_block": expression_habits_block}
            
        except Exception as e:
            logger.error(f"构建表达习惯失败: {e}")
            return {"expression_habits_block": ""}
    
    async def _build_memory_block(self) -> Dict[str, Any]:
        """构建记忆块"""
        if not global_config.memory.enable_memory:
            return {"memory_block": ""}
        
        try:
            from src.chat.memory_system.memory_activator import MemoryActivator
            from src.chat.memory_system.async_instant_memory_wrapper import get_async_instant_memory
            
            # 获取聊天历史
            chat_history = ""
            if self.parameters.message_list_before_now_long:
                recent_messages = self.parameters.message_list_before_now_long[-20:]
                chat_history = build_readable_messages(
                    recent_messages,
                    replace_bot_name=True,
                    timestamp_mode="normal",
                    truncate=True
                )
            
            # 激活长期记忆
            memory_activator = MemoryActivator()
            running_memories = await memory_activator.activate_memory_with_chat_history(
                target_message=self.parameters.target,
                chat_history_prompt=chat_history
            )
            
            # 获取即时记忆
            async_memory_wrapper = get_async_instant_memory(self.parameters.chat_id)
            instant_memory = await async_memory_wrapper.get_memory_with_fallback(self.parameters.target)
            
            # 构建记忆块
            memory_parts = []
            
            if running_memories:
                memory_parts.append("以下是当前在聊天中，你回忆起的记忆：")
                for memory in running_memories:
                    memory_parts.append(f"- {memory['content']}")
            
            if instant_memory:
                memory_parts.append(f"- {instant_memory}")
            
            memory_block = "\n".join(memory_parts) if memory_parts else ""
            
            return {"memory_block": memory_block}
            
        except Exception as e:
            logger.error(f"构建记忆块失败: {e}")
            return {"memory_block": ""}
    
    async def _build_relation_info(self) -> Dict[str, Any]:
        """构建关系信息"""
        try:
            relation_info = await Prompt.build_relation_info(self.parameters.chat_id, self.parameters.reply_to)
            return {"relation_info_block": relation_info}
        except Exception as e:
            logger.error(f"构建关系信息失败: {e}")
            return {"relation_info_block": ""}
    
    async def _build_tool_info(self) -> Dict[str, Any]:
        """构建工具信息"""
        if not global_config.tool.enable_tool:
            return {"tool_info_block": ""}
        
        try:
            from src.plugin_system.core.tool_use import ToolExecutor
            
            # 获取聊天历史
            chat_history = ""
            if self.parameters.message_list_before_now_long:
                recent_messages = self.parameters.message_list_before_now_long[-15:]
                chat_history = build_readable_messages(
                    recent_messages,
                    replace_bot_name=True,
                    timestamp_mode="normal",
                    truncate=True
                )
            
            # 创建工具执行器
            tool_executor = ToolExecutor(chat_id=self.parameters.chat_id)
            
            # 执行工具获取信息
            tool_results, _, _ = await tool_executor.execute_from_chat_message(
                sender=self.parameters.sender,
                target_message=self.parameters.target,
                chat_history=chat_history,
                return_details=False
            )
            
            # 构建工具信息块
            if tool_results:
                tool_info_parts = ["## 工具信息","以下是你通过工具获取到的实时信息："]
                for tool_result in tool_results:
                    tool_name = tool_result.get("tool_name", "unknown")
                    content = tool_result.get("content", "")
                    result_type = tool_result.get("type", "tool_result")
                    
                    tool_info_parts.append(f"- 【{tool_name}】{result_type}: {content}")
                
                tool_info_parts.append("以上是你获取到的实时信息，请在回复时参考这些信息。")
                tool_info_block = "\n".join(tool_info_parts)
            else:
                tool_info_block = ""
            
            return {"tool_info_block": tool_info_block}
            
        except Exception as e:
            logger.error(f"构建工具信息失败: {e}")
            return {"tool_info_block": ""}
    
    async def _build_knowledge_info(self) -> Dict[str, Any]:
        """构建知识信息"""
        if not global_config.lpmm_knowledge.enable:
            return {"knowledge_prompt": ""}
        
        try:
            from src.chat.knowledge.knowledge_lib import QAManager
            
            # 获取问题文本（当前消息）
            question = self.parameters.target or ""
            if not question:
                return {"knowledge_prompt": ""}
            
            # 创建QA管理器
            qa_manager = QAManager()
            
            # 搜索相关知识
            knowledge_results = await qa_manager.get_knowledge(
                question=question,
                chat_id=self.parameters.chat_id,
                max_results=5,
                min_similarity=0.5
            )
            
            # 构建知识块
            if knowledge_results and knowledge_results.get("knowledge_items"):
                knowledge_parts = ["## 知识库信息","以下是与你当前对话相关的知识信息："]
                
                for item in knowledge_results["knowledge_items"]:
                    content = item.get("content", "")
                    source = item.get("source", "")
                    relevance = item.get("relevance", 0.0)
                    
                    if content:
                        if source:
                            knowledge_parts.append(f"- [{relevance:.2f}] {content} (来源: {source})")
                        else:
                            knowledge_parts.append(f"- [{relevance:.2f}] {content}")
                
                if knowledge_results.get("summary"):
                    knowledge_parts.append(f"\n知识总结: {knowledge_results['summary']}")
                
                knowledge_prompt = "\n".join(knowledge_parts)
            else:
                knowledge_prompt = ""
            
            return {"knowledge_prompt": knowledge_prompt}
            
        except Exception as e:
            logger.error(f"构建知识信息失败: {e}")
            return {"knowledge_prompt": ""}
    
    async def _build_cross_context(self) -> Dict[str, Any]:
        """构建跨群上下文"""
        try:
            cross_context = await Prompt.build_cross_context(
                self.parameters.chat_id, self.parameters.prompt_mode, self.parameters.target_user_info
            )
            return {"cross_context_block": cross_context}
        except Exception as e:
            logger.error(f"构建跨群上下文失败: {e}")
            return {"cross_context_block": ""}
    
    async def _format_with_context(self, context_data: Dict[str, Any]) -> str:
        """使用上下文数据格式化模板"""
        if self.parameters.prompt_mode == "s4u":
            params = self._prepare_s4u_params(context_data)
        elif self.parameters.prompt_mode == "normal":
            params = self._prepare_normal_params(context_data)
        else:
            params = self._prepare_default_params(context_data)
        
        return await global_prompt_manager.format_prompt(self.name, **params) if self.name else self.format(**params)
    
    def _prepare_s4u_params(self, context_data: Dict[str, Any]) -> Dict[str, Any]:
        """准备S4U模式的参数"""
        return {
            **context_data,
            "expression_habits_block": context_data.get("expression_habits_block", ""),
            "tool_info_block": context_data.get("tool_info_block", ""),
            "knowledge_prompt": context_data.get("knowledge_prompt", ""),
            "memory_block": context_data.get("memory_block", ""),
            "relation_info_block": context_data.get("relation_info_block", ""),
            "extra_info_block": self.parameters.extra_info_block or context_data.get("extra_info_block", ""),
            "cross_context_block": context_data.get("cross_context_block", ""),
            "identity": self.parameters.identity_block or context_data.get("identity", ""),
            "action_descriptions": self.parameters.action_descriptions or context_data.get("action_descriptions", ""),
            "sender_name": self.parameters.sender,
            "mood_state": self.parameters.mood_prompt or context_data.get("mood_state", ""),
            "background_dialogue_prompt": context_data.get("background_dialogue_prompt", ""),
            "time_block": context_data.get("time_block", ""),
            "core_dialogue_prompt": context_data.get("core_dialogue_prompt", ""),
            "reply_target_block": context_data.get("reply_target_block", ""),
            "reply_style": global_config.personality.reply_style,
            "keywords_reaction_prompt": self.parameters.keywords_reaction_prompt or context_data.get("keywords_reaction_prompt", ""),
            "moderation_prompt": self.parameters.moderation_prompt_block or context_data.get("moderation_prompt", ""),
            "safety_guidelines_block": self.parameters.safety_guidelines_block or context_data.get("safety_guidelines_block", ""),
        }
    
    def _prepare_normal_params(self, context_data: Dict[str, Any]) -> Dict[str, Any]:
        """准备Normal模式的参数"""
        return {
            **context_data,
            "expression_habits_block": context_data.get("expression_habits_block", ""),
            "tool_info_block": context_data.get("tool_info_block", ""),
            "knowledge_prompt": context_data.get("knowledge_prompt", ""),
            "memory_block": context_data.get("memory_block", ""),
            "relation_info_block": context_data.get("relation_info_block", ""),
            "extra_info_block": self.parameters.extra_info_block or context_data.get("extra_info_block", ""),
            "cross_context_block": context_data.get("cross_context_block", ""),
            "identity": self.parameters.identity_block or context_data.get("identity", ""),
            "action_descriptions": self.parameters.action_descriptions or context_data.get("action_descriptions", ""),
            "schedule_block": self.parameters.schedule_block or context_data.get("schedule_block", ""),
            "time_block": context_data.get("time_block", ""),
            "chat_info": context_data.get("chat_info", ""),
            "reply_target_block": context_data.get("reply_target_block", ""),
            "config_expression_style": global_config.personality.reply_style,
            "mood_state": self.parameters.mood_prompt or context_data.get("mood_state", ""),
            "keywords_reaction_prompt": self.parameters.keywords_reaction_prompt or context_data.get("keywords_reaction_prompt", ""),
            "moderation_prompt": self.parameters.moderation_prompt_block or context_data.get("moderation_prompt", ""),
            "safety_guidelines_block": self.parameters.safety_guidelines_block or context_data.get("safety_guidelines_block", ""),
        }
    
    def _prepare_default_params(self, context_data: Dict[str, Any]) -> Dict[str, Any]:
        """准备默认模式的参数"""
        return {
            "expression_habits_block": context_data.get("expression_habits_block", ""),
            "relation_info_block": context_data.get("relation_info_block", ""),
            "chat_target": "",
            "time_block": context_data.get("time_block", ""),
            "chat_info": context_data.get("chat_info", ""),
            "identity": self.parameters.identity_block or context_data.get("identity", ""),
            "chat_target_2": "",
            "reply_target_block": context_data.get("reply_target_block", ""),
            "raw_reply": self.parameters.target,
            "reason": "",
            "mood_state": self.parameters.mood_prompt or context_data.get("mood_state", ""),
            "reply_style": global_config.personality.reply_style,
            "keywords_reaction_prompt": self.parameters.keywords_reaction_prompt or context_data.get("keywords_reaction_prompt", ""),
            "moderation_prompt": self.parameters.moderation_prompt_block or context_data.get("moderation_prompt", ""),
            "safety_guidelines_block": self.parameters.safety_guidelines_block or context_data.get("safety_guidelines_block", ""),
        }
    
    def format(self, *args, **kwargs) -> str:
        """格式化模板，支持位置参数和关键字参数"""
        try:
            # 先用位置参数格式化
            if args:
                formatted_args = {}
                for i in range(len(args)):
                    if i < len(self.args):
                        formatted_args[self.args[i]] = args[i]
                processed_template = self._processed_template.format(**formatted_args)
            else:
                processed_template = self._processed_template
            
            # 再用关键字参数格式化
            if kwargs:
                processed_template = processed_template.format(**kwargs)
            
            # 将临时标记还原为实际的花括号
            result = self._restore_escaped_braces(processed_template)
            return result
        except (IndexError, KeyError) as e:
            raise ValueError(f"格式化模板失败: {self.template}, args={args}, kwargs={kwargs} {str(e)}") from e
    
    def __str__(self) -> str:
        """返回格式化后的结果或原始模板"""
        return self._formatted_result if self._formatted_result else self.template
    
    def __repr__(self) -> str:
        """返回提示词的表示形式"""
        return f"Prompt(template='{self.template}', name='{self.name}')"

    # =============================================================================
    # PromptUtils功能迁移 - 静态工具方法
    # 这些方法原来在PromptUtils类中，现在作为Prompt类的静态方法
    # 解决循环导入问题
    # =============================================================================

    @staticmethod
    def parse_reply_target(target_message: str) -> Tuple[str, str]:
        """
        解析回复目标消息 - 统一实现

        Args:
            target_message: 目标消息，格式为 "发送者:消息内容" 或 "发送者：消息内容"

        Returns:
            Tuple[str, str]: (发送者名称, 消息内容)
        """
        sender = ""
        target = ""

        # 添加None检查，防止NoneType错误
        if target_message is None:
            return sender, target

        if ":" in target_message or "：" in target_message:
            # 使用正则表达式匹配中文或英文冒号
            parts = re.split(pattern=r"[:：]", string=target_message, maxsplit=1)
            if len(parts) == 2:
                sender = parts[0].strip()
                target = parts[1].strip()
        return sender, target

    @staticmethod
    async def build_relation_info(chat_id: str, reply_to: str) -> str:
        """
        构建关系信息 - 统一实现

        Args:
            chat_id: 聊天ID
            reply_to: 回复目标字符串

        Returns:
            str: 关系信息字符串
        """
        if not global_config.relationship.enable_relationship:
            return ""

        from src.person_info.relationship_fetcher import relationship_fetcher_manager

        relationship_fetcher = relationship_fetcher_manager.get_fetcher(chat_id)

        if not reply_to:
            return ""
        sender, text = Prompt.parse_reply_target(reply_to)
        if not sender or not text:
            return ""

        # 获取用户ID
        person_info_manager = get_person_info_manager()
        person_id = person_info_manager.get_person_id_by_person_name(sender)
        if not person_id:
            logger.warning(f"未找到用户 {sender} 的ID，跳过信息提取")
            return f"你完全不认识{sender}，不理解ta的相关信息。"

        return await relationship_fetcher.build_relation_info(person_id, points_num=5)

    @staticmethod
    async def build_cross_context(
        chat_id: str, prompt_mode: str, target_user_info: Optional[Dict[str, Any]]
    ) -> str:
        """
        构建跨群聊上下文 - 统一实现

        Args:
            chat_id: 聊天ID
            prompt_mode: 当前提示词模式
            target_user_info: 目标用户信息

        Returns:
            str: 跨群聊上下文字符串
        """
        if not global_config.cross_context.enable:
            return ""

        from src.plugin_system.apis import cross_context_api
        
        other_chat_raw_ids = cross_context_api.get_context_groups(chat_id)
        if not other_chat_raw_ids:
            return ""

        chat_stream = get_chat_manager().get_stream(chat_id)
        if not chat_stream:
            return ""

        if prompt_mode == "normal":
            return await cross_context_api.build_cross_context_normal(chat_stream, other_chat_raw_ids)
        elif prompt_mode == "s4u":
            return await cross_context_api.build_cross_context_s4u(chat_stream, other_chat_raw_ids, target_user_info)

        return ""

    @staticmethod
    def parse_reply_target_id(reply_to: str) -> str:
        """
        解析回复目标中的用户ID

        Args:
            reply_to: 回复目标字符串

        Returns:
            str: 用户ID
        """
        if not reply_to:
            return ""

        # 复用parse_reply_target方法的逻辑
        sender, _ = Prompt.parse_reply_target(reply_to)
        if not sender:
            return ""

        # 获取用户ID
        person_info_manager = get_person_info_manager()
        person_id = person_info_manager.get_person_id_by_person_name(sender)
        if person_id:
            user_id = person_info_manager.get_value_sync(person_id, "user_id")
            return str(user_id) if user_id else ""

        return ""


# 工厂函数
def create_prompt(
    template: str,
    name: Optional[str] = None,
    parameters: Optional[PromptParameters] = None,
    **kwargs
) -> Prompt:
    """快速创建Prompt实例的工厂函数"""
    if parameters is None:
        parameters = PromptParameters(**kwargs)
    return Prompt(template, name, parameters)


async def create_prompt_async(
    template: str,
    name: Optional[str] = None,
    parameters: Optional[PromptParameters] = None,
    **kwargs
) -> Prompt:
    """异步创建Prompt实例"""
    prompt = create_prompt(template, name, parameters, **kwargs)
    if global_prompt_manager._context._current_context:
        await global_prompt_manager._context.register_async(prompt)
    return prompt

