"""
长期记忆层管理器 (Long-term Memory Manager)

负责管理长期记忆图：
- 短期记忆到长期记忆的转移
- 图操作语言的执行
- 激活度衰减优化（长期记忆衰减更慢）
"""

import asyncio
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from src.common.logger import get_logger
from src.memory_graph.manager import MemoryManager
from src.memory_graph.models import Memory, MemoryType, NodeType
from src.memory_graph.models import GraphOperation, GraphOperationType, ShortTermMemory

logger = get_logger(__name__)


class LongTermMemoryManager:
    """
    长期记忆层管理器

    基于现有的 MemoryManager，扩展支持：
    - 短期记忆的批量转移
    - 图操作语言的解析和执行
    - 优化的激活度衰减策略
    """

    def __init__(
        self,
        memory_manager: MemoryManager,
        batch_size: int = 10,
        search_top_k: int = 5,
        llm_temperature: float = 0.2,
        long_term_decay_factor: float = 0.95,
    ):
        """
        初始化长期记忆层管理器

        Args:
            memory_manager: 现有的 MemoryManager 实例
            batch_size: 批量处理的短期记忆数量
            search_top_k: 检索相似记忆的数量
            llm_temperature: LLM 决策的温度参数
            long_term_decay_factor: 长期记忆的衰减因子（比短期记忆慢）
        """
        self.memory_manager = memory_manager
        self.batch_size = batch_size
        self.search_top_k = search_top_k
        self.llm_temperature = llm_temperature
        self.long_term_decay_factor = long_term_decay_factor

        # 状态
        self._initialized = False

        logger.info(
            f"长期记忆管理器已创建 (batch_size={batch_size}, "
            f"search_top_k={search_top_k}, decay_factor={long_term_decay_factor:.2f})"
        )

    async def initialize(self) -> None:
        """初始化管理器"""
        if self._initialized:
            logger.warning("长期记忆管理器已经初始化")
            return

        try:
            logger.info("开始初始化长期记忆管理器...")

            # 确保底层 MemoryManager 已初始化
            if not self.memory_manager._initialized:
                await self.memory_manager.initialize()

            self._initialized = True
            logger.info("✅ 长期记忆管理器初始化完成")

        except Exception as e:
            logger.error(f"长期记忆管理器初始化失败: {e}", exc_info=True)
            raise

    async def transfer_from_short_term(
        self, short_term_memories: list[ShortTermMemory]
    ) -> dict[str, Any]:
        """
        将短期记忆批量转移到长期记忆

        流程：
        1. 分批处理短期记忆
        2. 对每条短期记忆，在长期记忆中检索相似记忆
        3. 将短期记忆和候选长期记忆发送给 LLM 决策
        4. 解析并执行图操作指令
        5. 保存更新

        Args:
            short_term_memories: 待转移的短期记忆列表

        Returns:
            转移结果统计
        """
        if not self._initialized:
            await self.initialize()

        try:
            logger.info(f"开始转移 {len(short_term_memories)} 条短期记忆到长期记忆...")

            result = {
                "processed_count": 0,
                "created_count": 0,
                "updated_count": 0,
                "merged_count": 0,
                "failed_count": 0,
                "transferred_memory_ids": [],
            }

            # 分批处理
            for batch_start in range(0, len(short_term_memories), self.batch_size):
                batch_end = min(batch_start + self.batch_size, len(short_term_memories))
                batch = short_term_memories[batch_start:batch_end]

                logger.info(
                    f"处理批次 {batch_start // self.batch_size + 1}/"
                    f"{(len(short_term_memories) - 1) // self.batch_size + 1} "
                    f"({len(batch)} 条记忆)"
                )

                # 处理当前批次
                batch_result = await self._process_batch(batch)

                # 汇总结果
                result["processed_count"] += batch_result["processed_count"]
                result["created_count"] += batch_result["created_count"]
                result["updated_count"] += batch_result["updated_count"]
                result["merged_count"] += batch_result["merged_count"]
                result["failed_count"] += batch_result["failed_count"]
                result["transferred_memory_ids"].extend(batch_result["transferred_memory_ids"])

                # 让出控制权
                await asyncio.sleep(0.01)

            logger.info(f"✅ 短期记忆转移完成: {result}")
            return result

        except Exception as e:
            logger.error(f"转移短期记忆失败: {e}", exc_info=True)
            return {"error": str(e), "processed_count": 0}

    async def _process_batch(self, batch: list[ShortTermMemory]) -> dict[str, Any]:
        """
        处理一批短期记忆

        Args:
            batch: 短期记忆批次

        Returns:
            批次处理结果
        """
        result = {
            "processed_count": 0,
            "created_count": 0,
            "updated_count": 0,
            "merged_count": 0,
            "failed_count": 0,
            "transferred_memory_ids": [],
        }

        for stm in batch:
            try:
                # 步骤1: 在长期记忆中检索相似记忆
                similar_memories = await self._search_similar_long_term_memories(stm)

                # 步骤2: LLM 决策如何更新图结构
                operations = await self._decide_graph_operations(stm, similar_memories)

                # 步骤3: 执行图操作
                success = await self._execute_graph_operations(operations, stm)

                if success:
                    result["processed_count"] += 1
                    result["transferred_memory_ids"].append(stm.id)

                    # 统计操作类型
                    for op in operations:
                        if op.operation_type == GraphOperationType.CREATE_MEMORY:
                            result["created_count"] += 1
                        elif op.operation_type == GraphOperationType.UPDATE_MEMORY:
                            result["updated_count"] += 1
                        elif op.operation_type == GraphOperationType.MERGE_MEMORIES:
                            result["merged_count"] += 1
                else:
                    result["failed_count"] += 1

            except Exception as e:
                logger.error(f"处理短期记忆 {stm.id} 失败: {e}", exc_info=True)
                result["failed_count"] += 1

        return result

    async def _search_similar_long_term_memories(
        self, stm: ShortTermMemory
    ) -> list[Memory]:
        """
        在长期记忆中检索与短期记忆相似的记忆

        优化：不仅检索内容相似的，还利用图结构获取上下文相关的记忆
        """
        try:
            from src.config.config import global_config

            # 检查是否启用了高级路径扩展算法
            use_path_expansion = getattr(global_config.memory, "enable_path_expansion", False)
            
            # 1. 检索记忆
            # 如果启用了路径扩展，search_memories 内部会自动使用 PathScoreExpansion
            # 我们只需要传入合适的 expand_depth
            expand_depth = getattr(global_config.memory, "path_expansion_max_hops", 2) if use_path_expansion else 0

            memories = await self.memory_manager.search_memories(
                query=stm.content,
                top_k=self.search_top_k,
                include_forgotten=False,
                use_multi_query=False,  # 不使用多查询，避免过度扩展
                expand_depth=expand_depth
            )

            # 2. 图结构扩展 (Graph Expansion)
            # 如果已经使用了高级路径扩展算法，就不需要再做简单的手动扩展了
            if use_path_expansion:
                logger.debug(f"已使用路径扩展算法检索到 {len(memories)} 条记忆")
                return memories

            # 如果未启用高级算法，使用简单的 1 跳邻居扩展作为保底
            expanded_memories = []
            seen_ids = {m.id for m in memories}
            
            for mem in memories:
                expanded_memories.append(mem)
                
                # 获取该记忆的直接关联记忆（1跳邻居）
                try:
                    # 利用 MemoryManager 的底层图遍历能力
                    related_ids = self.memory_manager._get_related_memories(mem.id, max_depth=1)
                    
                    # 限制每个记忆扩展的邻居数量，避免上下文爆炸
                    max_neighbors = 2
                    neighbor_count = 0
                    
                    for rid in related_ids:
                        if rid not in seen_ids:
                            related_mem = await self.memory_manager.get_memory(rid)
                            if related_mem:
                                expanded_memories.append(related_mem)
                                seen_ids.add(rid)
                                neighbor_count += 1
                                
                        if neighbor_count >= max_neighbors:
                            break
                            
                except Exception as e:
                    logger.warning(f"获取关联记忆失败: {e}")
                
                # 总数限制
                if len(expanded_memories) >= self.search_top_k * 2:
                    break

            logger.debug(f"为短期记忆 {stm.id} 找到 {len(expanded_memories)} 个长期记忆 (含简单图扩展)")
            return expanded_memories

        except Exception as e:
            logger.error(f"检索相似长期记忆失败: {e}", exc_info=True)
            return []

    async def _decide_graph_operations(
        self, stm: ShortTermMemory, similar_memories: list[Memory]
    ) -> list[GraphOperation]:
        """
        使用 LLM 决策如何更新图结构

        Args:
            stm: 短期记忆
            similar_memories: 相似的长期记忆列表

        Returns:
            图操作指令列表
        """
        try:
            from src.config.config import model_config
            from src.llm_models.utils_model import LLMRequest

            # 构建提示词
            prompt = self._build_graph_operation_prompt(stm, similar_memories)

            # 调用长期记忆构建模型
            llm = LLMRequest(
                model_set=model_config.model_task_config.memory_long_term_builder,
                request_type="long_term_memory.graph_operations",
            )

            response, _ = await llm.generate_response_async(
                prompt,
                temperature=self.llm_temperature,
                max_tokens=2000,
            )

            # 解析图操作指令
            operations = self._parse_graph_operations(response)

            logger.info(f"LLM 生成 {len(operations)} 个图操作指令")
            return operations

        except Exception as e:
            logger.error(f"LLM 决策图操作失败: {e}", exc_info=True)
            # 默认创建新记忆
            return [
                GraphOperation(
                    operation_type=GraphOperationType.CREATE_MEMORY,
                    parameters={
                        "subject": stm.subject or "未知",
                        "topic": stm.topic or stm.content[:50],
                        "object": stm.object,
                        "memory_type": stm.memory_type or "fact",
                        "importance": stm.importance,
                        "attributes": stm.attributes,
                    },
                    reason=f"LLM 决策失败，默认创建新记忆: {e}",
                    confidence=0.5,
                )
            ]

    def _build_graph_operation_prompt(
        self, stm: ShortTermMemory, similar_memories: list[Memory]
    ) -> str:
        """构建图操作的 LLM 提示词"""

        # 格式化短期记忆
        stm_desc = f"""
**待转移的短期记忆：**
- 内容: {stm.content}
- 主体: {stm.subject or '未指定'}
- 主题: {stm.topic or '未指定'}
- 客体: {stm.object or '未指定'}
- 类型: {stm.memory_type or '未指定'}
- 重要性: {stm.importance:.2f}
- 属性: {json.dumps(stm.attributes, ensure_ascii=False)}
"""

        # 格式化相似的长期记忆
        similar_desc = ""
        if similar_memories:
            similar_lines = []
            for i, mem in enumerate(similar_memories):
                subject_node = mem.get_subject_node()
                mem_text = mem.to_text()
                similar_lines.append(
                    f"{i + 1}. [ID: {mem.id}] {mem_text}\n"
                    f"   - 重要性: {mem.importance:.2f}\n"
                    f"   - 激活度: {mem.activation:.2f}\n"
                    f"   - 节点数: {len(mem.nodes)}"
                )
            similar_desc = "\n\n".join(similar_lines)
        else:
            similar_desc = "（未找到相似记忆）"

        prompt = f"""你是一个记忆图结构管理专家。现在需要将一条短期记忆转移到长期记忆图中。

{stm_desc}

**候选的相似长期记忆：**
{similar_desc}

**图操作语言说明：**

你可以使用以下操作指令来精确控制记忆图的更新：

1. **CREATE_MEMORY** - 创建新记忆
   参数: subject, topic, object, memory_type, importance, attributes
   *注意：target_id 请使用临时ID（如 "TEMP_MEM_1"），后续操作可引用此ID*

2. **UPDATE_MEMORY** - 更新现有记忆
   参数: memory_id, updated_fields (包含要更新的字段)

3. **MERGE_MEMORIES** - 合并多个记忆
   参数: source_memory_ids (要合并的记忆ID列表), merged_content, merged_importance

4. **CREATE_NODE** - 创建新节点
   参数: content, node_type, memory_id (所属记忆ID)
   *注意：target_id 请使用临时ID（如 "TEMP_NODE_1"），后续操作可引用此ID*

5. **UPDATE_NODE** - 更新节点
   参数: node_id, updated_content

6. **MERGE_NODES** - 合并节点
   参数: source_node_ids, merged_content

7. **CREATE_EDGE** - 创建边
   参数: source_node_id, target_node_id, relation, edge_type, importance

8. **UPDATE_EDGE** - 更新边
   参数: edge_id, updated_relation, updated_importance

9. **DELETE_EDGE** - 删除边
   参数: edge_id

**ID 引用规则（非常重要）：**
1. 对于**新创建**的对象（记忆、节点），请在 `target_id` 字段指定一个唯一的临时ID（例如 "TEMP_MEM_1", "TEMP_NODE_1"）。
2. 在后续的操作中（如 `CREATE_NODE` 需要 `memory_id`，或 `CREATE_EDGE` 需要 `source_node_id`），请直接使用这些临时ID。
3. 系统会自动将临时ID解析为真实的UUID。
4. **严禁**使用中文描述作为ID（如"新创建的记忆ID"），必须使用英文临时ID。

**任务要求：**
1. 分析短期记忆与候选长期记忆的关系
2. 决定最佳的图更新策略：
   - 如果没有相似记忆或差异较大 → CREATE_MEMORY
   - 如果有高度相似记忆 → UPDATE_MEMORY 或 MERGE_MEMORIES
   - 如果需要补充信息 → CREATE_NODE + CREATE_EDGE
3. 生成具体的图操作指令列表
4. 确保操作的逻辑性和连贯性

**输出格式（JSON数组）：**
```json
[
  {{
    "operation_type": "CREATE_MEMORY",
    "target_id": "TEMP_MEM_1",
    "parameters": {{
      "subject": "...",
      ...
    }},
    "reason": "创建新记忆",
    "confidence": 0.9
  }},
  {{
    "operation_type": "CREATE_NODE",
    "target_id": "TEMP_NODE_1",
    "parameters": {{
      "content": "...",
      "memory_id": "TEMP_MEM_1"
    }},
    "reason": "为新记忆添加节点",
    "confidence": 0.9
  }}
]
```

请输出JSON数组："""

        return prompt

    def _parse_graph_operations(self, response: str) -> list[GraphOperation]:
        """解析 LLM 生成的图操作指令"""
        try:
            # 提取 JSON
            json_match = re.search(r"```json\s*(.*?)\s*```", response, re.DOTALL)
            if json_match:
                json_str = json_match.group(1)
            else:
                json_str = response.strip()

            # 移除注释
            json_str = re.sub(r"//.*", "", json_str)
            json_str = re.sub(r"/\*.*?\*/", "", json_str, flags=re.DOTALL)

            # 解析
            data = json.loads(json_str)

            # 转换为 GraphOperation 对象
            operations = []
            for item in data:
                try:
                    op = GraphOperation(
                        operation_type=GraphOperationType(item["operation_type"]),
                        target_id=item.get("target_id"),
                        parameters=item.get("parameters", {}),
                        reason=item.get("reason", ""),
                        confidence=item.get("confidence", 1.0),
                    )
                    operations.append(op)
                except (KeyError, ValueError) as e:
                    logger.warning(f"解析图操作失败: {e}, 项目: {item}")
                    continue

            return operations

        except json.JSONDecodeError as e:
            logger.error(f"JSON 解析失败: {e}, 响应: {response[:200]}")
            return []

    async def _execute_graph_operations(
        self, operations: list[GraphOperation], source_stm: ShortTermMemory
    ) -> bool:
        """
        执行图操作指令

        Args:
            operations: 图操作指令列表
            source_stm: 源短期记忆

        Returns:
            是否执行成功
        """
        if not operations:
            logger.warning("没有图操作指令，跳过执行")
            return False

        try:
            success_count = 0
            temp_id_map: dict[str, str] = {}

            for op in operations:
                try:
                    if op.operation_type == GraphOperationType.CREATE_MEMORY:
                        await self._execute_create_memory(op, source_stm, temp_id_map)
                        success_count += 1

                    elif op.operation_type == GraphOperationType.UPDATE_MEMORY:
                        await self._execute_update_memory(op, temp_id_map)
                        success_count += 1

                    elif op.operation_type == GraphOperationType.MERGE_MEMORIES:
                        await self._execute_merge_memories(op, source_stm, temp_id_map)
                        success_count += 1

                    elif op.operation_type == GraphOperationType.CREATE_NODE:
                        await self._execute_create_node(op, temp_id_map)
                        success_count += 1

                    elif op.operation_type == GraphOperationType.UPDATE_NODE:
                        await self._execute_update_node(op, temp_id_map)
                        success_count += 1

                    elif op.operation_type == GraphOperationType.MERGE_NODES:
                        await self._execute_merge_nodes(op, temp_id_map)
                        success_count += 1

                    elif op.operation_type == GraphOperationType.CREATE_EDGE:
                        await self._execute_create_edge(op, temp_id_map)
                        success_count += 1

                    elif op.operation_type == GraphOperationType.UPDATE_EDGE:
                        await self._execute_update_edge(op, temp_id_map)
                        success_count += 1

                    elif op.operation_type == GraphOperationType.DELETE_EDGE:
                        await self._execute_delete_edge(op, temp_id_map)
                        success_count += 1

                    else:
                        logger.warning(f"未实现的操作类型: {op.operation_type}")

                except Exception as e:
                    logger.error(f"执行图操作失败: {op}, 错误: {e}", exc_info=True)

            logger.info(f"执行了 {success_count}/{len(operations)} 个图操作")
            return success_count > 0

        except Exception as e:
            logger.error(f"执行图操作失败: {e}", exc_info=True)
            return False

    @staticmethod
    def _is_placeholder_id(candidate: str | None) -> bool:
        if not candidate or not isinstance(candidate, str):
            return False
        lowered = candidate.strip().lower()
        return lowered.startswith(("new_", "temp_"))

    def _register_temp_id(
        self,
        placeholder: str | None,
        actual_id: str,
        temp_id_map: dict[str, str],
        force: bool = False,
    ) -> None:
        if not actual_id or not placeholder or not isinstance(placeholder, str):
            return
        if placeholder == actual_id:
            return
        if force or self._is_placeholder_id(placeholder):
            temp_id_map[placeholder] = actual_id

    def _resolve_id(self, raw_id: str | None, temp_id_map: dict[str, str]) -> str | None:
        if raw_id is None:
            return None
        return temp_id_map.get(raw_id, raw_id)

    def _resolve_value(self, value: Any, temp_id_map: dict[str, str]) -> Any:
        if isinstance(value, str):
            return self._resolve_id(value, temp_id_map)
        if isinstance(value, list):
            return [self._resolve_value(v, temp_id_map) for v in value]
        if isinstance(value, dict):
            return {k: self._resolve_value(v, temp_id_map) for k, v in value.items()}
        return value

    def _resolve_parameters(
        self, params: dict[str, Any], temp_id_map: dict[str, str]
    ) -> dict[str, Any]:
        return {k: self._resolve_value(v, temp_id_map) for k, v in params.items()}

    def _register_aliases_from_params(
        self,
        params: dict[str, Any],
        actual_id: str,
        temp_id_map: dict[str, str],
        *,
        extra_keywords: tuple[str, ...] = (),
        force: bool = False,
    ) -> None:
        alias_keywords = ("alias", "placeholder", "temp_id", "register_as") + tuple(
            extra_keywords
        )
        for key, value in params.items():
            if isinstance(value, str):
                lower_key = key.lower()
                if any(keyword in lower_key for keyword in alias_keywords):
                    self._register_temp_id(value, actual_id, temp_id_map, force=force)
            elif isinstance(value, list):
                lower_key = key.lower()
                if any(keyword in lower_key for keyword in alias_keywords):
                    for item in value:
                        if isinstance(item, str):
                            self._register_temp_id(item, actual_id, temp_id_map, force=force)
            elif isinstance(value, dict):
                self._register_aliases_from_params(
                    value,
                    actual_id,
                    temp_id_map,
                    extra_keywords=extra_keywords,
                    force=force,
                )

    async def _execute_create_memory(
        self,
        op: GraphOperation,
        source_stm: ShortTermMemory,
        temp_id_map: dict[str, str],
    ) -> None:
        """执行创建记忆操作"""
        params = self._resolve_parameters(op.parameters, temp_id_map)

        memory = await self.memory_manager.create_memory(
            subject=params.get("subject", source_stm.subject or "未知"),
            memory_type=params.get("memory_type", source_stm.memory_type or "fact"),
            topic=params.get("topic", source_stm.topic or source_stm.content[:50]),
            object=params.get("object", source_stm.object),
            attributes=params.get("attributes", source_stm.attributes),
            importance=params.get("importance", source_stm.importance),
        )

        if memory:
            # 标记为从短期记忆转移而来
            memory.metadata["transferred_from_stm"] = source_stm.id
            memory.metadata["transfer_time"] = datetime.now().isoformat()

            logger.info(f"✅ 创建长期记忆: {memory.id} (来自短期记忆 {source_stm.id})")
            self._register_temp_id(op.target_id, memory.id, temp_id_map)
            self._register_aliases_from_params(
                op.parameters,
                memory.id,
                temp_id_map,
                extra_keywords=("memory_id", "memory_alias", "memory_placeholder"),
                force=True,
            )
        else:
            logger.error(f"创建长期记忆失败: {op}")

    async def _execute_update_memory(
        self, op: GraphOperation, temp_id_map: dict[str, str]
    ) -> None:
        """执行更新记忆操作"""
        memory_id = self._resolve_id(op.target_id, temp_id_map)
        if not memory_id:
            logger.error("更新操作缺少目标记忆ID")
            return
            
        updates_raw = op.parameters.get("updated_fields", {})
        updates = (
            self._resolve_parameters(updates_raw, temp_id_map)
            if isinstance(updates_raw, dict)
            else updates_raw
        )

        success = await self.memory_manager.update_memory(memory_id, **updates)

        if success:
            logger.info(f"✅ 更新长期记忆: {memory_id}")
        else:
            logger.error(f"更新长期记忆失败: {memory_id}")

    async def _execute_merge_memories(
        self,
        op: GraphOperation,
        source_stm: ShortTermMemory,
        temp_id_map: dict[str, str],
    ) -> None:
        """执行合并记忆操作 (智能合并版)"""
        params = self._resolve_parameters(op.parameters, temp_id_map)
        source_ids = params.get("source_memory_ids", [])
        merged_content = params.get("merged_content", "")
        merged_importance = params.get("merged_importance", source_stm.importance)

        if not source_ids:
            logger.warning("合并操作缺少源记忆ID，跳过")
            return

        # 目标记忆（保留的那个）
        target_id = source_ids[0]
        
        # 待合并记忆（将被删除的）
        memories_to_merge = source_ids[1:]
        
        logger.info(f"开始智能合并记忆: {memories_to_merge} -> {target_id}")

        # 1. 调用 GraphStore 的合并功能（转移节点和边）
        merge_success = self.memory_manager.graph_store.merge_memories(target_id, memories_to_merge)

        if merge_success:
            # 2. 更新目标记忆的元数据
            await self.memory_manager.update_memory(
                target_id,
                metadata={
                    "merged_content": merged_content,
                    "merged_from": memories_to_merge,
                    "merged_from_stm": source_stm.id,
                    "merge_time": datetime.now().isoformat()
                },
                importance=merged_importance,
            )
            
            # 3. 异步保存
            asyncio.create_task(self.memory_manager._async_save_graph_store("合并记忆"))
            logger.info(f"✅ 合并记忆完成: {source_ids} -> {target_id}")
        else:
            logger.error(f"合并记忆失败: {source_ids}")

    async def _execute_create_node(
        self, op: GraphOperation, temp_id_map: dict[str, str]
    ) -> None:
        """执行创建节点操作"""
        params = self._resolve_parameters(op.parameters, temp_id_map)
        content = params.get("content")
        node_type = params.get("node_type", "OBJECT")
        memory_id = params.get("memory_id")
        
        if not content or not memory_id:
            logger.warning(f"创建节点失败: 缺少必要参数 (content={content}, memory_id={memory_id})")
            return

        import uuid
        node_id = str(uuid.uuid4())
        
        success = self.memory_manager.graph_store.add_node(
            node_id=node_id,
            content=content,
            node_type=node_type,
            memory_id=memory_id,
            metadata={"created_by": "long_term_manager"}
        )
        
        if success:
            # 尝试为新节点生成 embedding (异步)
            asyncio.create_task(self._generate_node_embedding(node_id, content))
            logger.info(f"✅ 创建节点: {content} ({node_type}) -> {memory_id}")
            self._register_temp_id(op.target_id, node_id, temp_id_map)
            self._register_aliases_from_params(
                op.parameters,
                node_id,
                temp_id_map,
                extra_keywords=("node_id", "node_alias", "node_placeholder"),
                force=True,
            )
        else:
            logger.error(f"创建节点失败: {op}")

    async def _execute_update_node(
        self, op: GraphOperation, temp_id_map: dict[str, str]
    ) -> None:
        """执行更新节点操作"""
        node_id = self._resolve_id(op.target_id, temp_id_map)
        params = self._resolve_parameters(op.parameters, temp_id_map)
        updated_content = params.get("updated_content")
        
        if not node_id:
            logger.warning("更新节点失败: 缺少 node_id")
            return
            
        success = self.memory_manager.graph_store.update_node(
            node_id=node_id,
            content=updated_content
        )
        
        if success:
            logger.info(f"✅ 更新节点: {node_id}")
        else:
            logger.error(f"更新节点失败: {node_id}")

    async def _execute_merge_nodes(
        self, op: GraphOperation, temp_id_map: dict[str, str]
    ) -> None:
        """执行合并节点操作"""
        params = self._resolve_parameters(op.parameters, temp_id_map)
        source_node_ids = params.get("source_node_ids", [])
        merged_content = params.get("merged_content")
        
        if not source_node_ids or len(source_node_ids) < 2:
            logger.warning("合并节点失败: 需要至少两个节点")
            return
            
        target_id = source_node_ids[0]
        sources = source_node_ids[1:]
        
        # 更新目标节点内容
        if merged_content:
            self.memory_manager.graph_store.update_node(target_id, content=merged_content)
            
        # 合并其他节点到目标节点
        for source_id in sources:
            self.memory_manager.graph_store.merge_nodes(source_id, target_id)
            
        logger.info(f"✅ 合并节点: {sources} -> {target_id}")

    async def _execute_create_edge(
        self, op: GraphOperation, temp_id_map: dict[str, str]
    ) -> None:
        """执行创建边操作"""
        params = self._resolve_parameters(op.parameters, temp_id_map)
        source_id = params.get("source_node_id")
        target_id = params.get("target_node_id")
        relation = params.get("relation", "related")
        edge_type = params.get("edge_type", "RELATION")
        importance = params.get("importance", 0.5)
        
        if not source_id or not target_id:
            logger.warning(f"创建边失败: 缺少节点ID ({source_id} -> {target_id})")
            return

        # 检查节点是否存在
        if not self.memory_manager.graph_store or not self.memory_manager.graph_store.graph.has_node(source_id):
            logger.warning(f"创建边失败: 源节点不存在 ({source_id})")
            return
        if not self.memory_manager.graph_store or not self.memory_manager.graph_store.graph.has_node(target_id):
            logger.warning(f"创建边失败: 目标节点不存在 ({target_id})")
            return
            
        edge_id = self.memory_manager.graph_store.add_edge(
            source_id=source_id,
            target_id=target_id,
            relation=relation,
            edge_type=edge_type,
            importance=importance,
            metadata={"created_by": "long_term_manager"}
        )
        
        if edge_id:
            logger.info(f"✅ 创建边: {source_id} -> {target_id} ({relation})")
        else:
            logger.error(f"创建边失败: {op}")

    async def _execute_update_edge(
        self, op: GraphOperation, temp_id_map: dict[str, str]
    ) -> None:
        """执行更新边操作"""
        edge_id = self._resolve_id(op.target_id, temp_id_map)
        params = self._resolve_parameters(op.parameters, temp_id_map)
        updated_relation = params.get("updated_relation")
        updated_importance = params.get("updated_importance")
        
        if not edge_id:
            logger.warning("更新边失败: 缺少 edge_id")
            return
            
        success = self.memory_manager.graph_store.update_edge(
            edge_id=edge_id,
            relation=updated_relation,
            importance=updated_importance
        )
        
        if success:
            logger.info(f"✅ 更新边: {edge_id}")
        else:
            logger.error(f"更新边失败: {edge_id}")

    async def _execute_delete_edge(
        self, op: GraphOperation, temp_id_map: dict[str, str]
    ) -> None:
        """执行删除边操作"""
        edge_id = self._resolve_id(op.target_id, temp_id_map)
        
        if not edge_id:
            logger.warning("删除边失败: 缺少 edge_id")
            return
            
        success = self.memory_manager.graph_store.remove_edge(edge_id)
        
        if success:
            logger.info(f"✅ 删除边: {edge_id}")
        else:
            logger.error(f"删除边失败: {edge_id}")

    async def _generate_node_embedding(self, node_id: str, content: str) -> None:
        """为新节点生成 embedding 并存入向量库"""
        try:
            if not self.memory_manager.vector_store or not self.memory_manager.embedding_generator:
                return
                
            embedding = await self.memory_manager.embedding_generator.generate(content)
            if embedding is not None:
                # 需要构造一个 MemoryNode 对象来调用 add_node
                from src.memory_graph.models import MemoryNode, NodeType
                node = MemoryNode(
                    id=node_id,
                    content=content,
                    node_type=NodeType.OBJECT, # 默认
                    embedding=embedding
                )
                await self.memory_manager.vector_store.add_node(node)
        except Exception as e:
            logger.warning(f"生成节点 embedding 失败: {e}")

    async def apply_long_term_decay(self) -> dict[str, Any]:
        """
        应用长期记忆的激活度衰减

        长期记忆的衰减比短期记忆慢，使用更高的衰减因子。

        Returns:
            衰减结果统计
        """
        if not self._initialized:
            await self.initialize()

        try:
            logger.info("开始应用长期记忆激活度衰减...")

            all_memories = self.memory_manager.graph_store.get_all_memories()
            decayed_count = 0

            for memory in all_memories:
                # 跳过已遗忘的记忆
                if memory.metadata.get("forgotten", False):
                    continue

                # 计算衰减
                activation_info = memory.metadata.get("activation", {})
                last_access = activation_info.get("last_access")

                if last_access:
                    try:
                        last_access_dt = datetime.fromisoformat(last_access)
                        days_passed = (datetime.now() - last_access_dt).days

                        if days_passed > 0:
                            # 使用长期记忆的衰减因子
                            base_activation = activation_info.get("level", memory.activation)
                            new_activation = base_activation * (self.long_term_decay_factor ** days_passed)

                            # 更新激活度
                            memory.activation = new_activation
                            activation_info["level"] = new_activation
                            memory.metadata["activation"] = activation_info

                            decayed_count += 1

                    except (ValueError, TypeError) as e:
                        logger.warning(f"解析时间失败: {e}")

            # 保存更新
            await self.memory_manager.persistence.save_graph_store(
                self.memory_manager.graph_store
            )

            logger.info(f"✅ 长期记忆衰减完成: {decayed_count} 条记忆已更新")
            return {"decayed_count": decayed_count, "total_memories": len(all_memories)}

        except Exception as e:
            logger.error(f"应用长期记忆衰减失败: {e}", exc_info=True)
            return {"error": str(e), "decayed_count": 0}

    def get_statistics(self) -> dict[str, Any]:
        """获取长期记忆层统计信息"""
        if not self._initialized or not self.memory_manager.graph_store:
            return {}

        stats = self.memory_manager.get_statistics()
        stats["decay_factor"] = self.long_term_decay_factor
        stats["batch_size"] = self.batch_size

        return stats

    async def shutdown(self) -> None:
        """关闭管理器"""
        if not self._initialized:
            return

        try:
            logger.info("正在关闭长期记忆管理器...")

            # 长期记忆的保存由 MemoryManager 负责

            self._initialized = False
            logger.info("✅ 长期记忆管理器已关闭")

        except Exception as e:
            logger.error(f"关闭长期记忆管理器失败: {e}", exc_info=True)


# 全局单例
_long_term_manager_instance: LongTermMemoryManager | None = None


def get_long_term_manager() -> LongTermMemoryManager:
    """获取长期记忆管理器单例（需要先初始化记忆图系统）"""
    global _long_term_manager_instance
    if _long_term_manager_instance is None:
        from src.memory_graph.manager_singleton import get_memory_manager

        memory_manager = get_memory_manager()
        if memory_manager is None:
            raise RuntimeError("记忆图系统未初始化，无法创建长期记忆管理器")
        _long_term_manager_instance = LongTermMemoryManager(memory_manager)
    return _long_term_manager_instance
