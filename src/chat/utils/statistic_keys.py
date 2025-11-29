"""
该模块用于存放统计数据相关的常量键名。
"""

# 统计数据的键
TOTAL_REQ_CNT = "total_requests"
TOTAL_COST = "total_cost"
REQ_CNT_BY_TYPE = "requests_by_type"
REQ_CNT_BY_USER = "requests_by_user"
REQ_CNT_BY_MODEL = "requests_by_model"
REQ_CNT_BY_MODULE = "requests_by_module"
IN_TOK_BY_TYPE = "in_tokens_by_type"
IN_TOK_BY_USER = "in_tokens_by_user"
IN_TOK_BY_MODEL = "in_tokens_by_model"
IN_TOK_BY_MODULE = "in_tokens_by_module"
OUT_TOK_BY_TYPE = "out_tokens_by_type"
OUT_TOK_BY_USER = "out_tokens_by_user"
OUT_TOK_BY_MODEL = "out_tokens_by_model"
OUT_TOK_BY_MODULE = "out_tokens_by_module"
TOTAL_TOK_BY_TYPE = "tokens_by_type"
TOTAL_TOK_BY_USER = "tokens_by_user"
TOTAL_TOK_BY_MODEL = "tokens_by_model"
TOTAL_TOK_BY_MODULE = "tokens_by_module"
COST_BY_TYPE = "costs_by_type"
COST_BY_USER = "costs_by_user"
COST_BY_MODEL = "costs_by_model"
COST_BY_MODULE = "costs_by_module"
ONLINE_TIME = "online_time"
TOTAL_MSG_CNT = "total_messages"
MSG_CNT_BY_CHAT = "messages_by_chat"
TIME_COST_BY_TYPE = "time_costs_by_type"
TIME_COST_BY_USER = "time_costs_by_user"
TIME_COST_BY_MODEL = "time_costs_by_model"
TIME_COST_BY_MODULE = "time_costs_by_module"
AVG_TIME_COST_BY_TYPE = "avg_time_costs_by_type"
AVG_TIME_COST_BY_USER = "avg_time_costs_by_user"
AVG_TIME_COST_BY_MODEL = "avg_time_costs_by_model"
AVG_TIME_COST_BY_MODULE = "avg_time_costs_by_module"
STD_TIME_COST_BY_TYPE = "std_time_costs_by_type"
STD_TIME_COST_BY_USER = "std_time_costs_by_user"
STD_TIME_COST_BY_MODEL = "std_time_costs_by_model"
STD_TIME_COST_BY_MODULE = "std_time_costs_by_module"

# 新增模型性能指标
TPS_BY_MODEL = "tps_by_model"  # Tokens Per Second
COST_PER_KTOK_BY_MODEL = "cost_per_ktok_by_model"
AVG_TOK_BY_MODEL = "avg_tok_by_model"

# 新增按供应商统计
REQ_CNT_BY_PROVIDER = "requests_by_provider"
COST_BY_PROVIDER = "costs_by_provider"
TOTAL_TOK_BY_PROVIDER = "tokens_by_provider"
TPS_BY_PROVIDER = "tps_by_provider"
COST_PER_KTOK_BY_PROVIDER = "cost_per_ktok_by_provider"
TIME_COST_BY_PROVIDER = "time_costs_by_provider"
AVG_TIME_COST_BY_PROVIDER = "avg_time_costs_by_provider"
STD_TIME_COST_BY_PROVIDER = "std_time_costs_by_provider"

# 新增饼图和条形图数据
PIE_CHART_COST_BY_PROVIDER = "pie_chart_cost_by_provider"
PIE_CHART_REQ_BY_PROVIDER = "pie_chart_req_by_provider"
PIE_CHART_COST_BY_MODULE = "pie_chart_cost_by_module"
BAR_CHART_COST_BY_MODEL = "bar_chart_cost_by_model"
BAR_CHART_REQ_BY_MODEL = "bar_chart_req_by_model"

# 新增更多图表数据
BAR_CHART_TOKEN_COMPARISON = "bar_chart_token_comparison"  # Token输入输出对比图
SCATTER_CHART_RESPONSE_TIME = "scatter_chart_response_time"  # 响应时间分布散点图
RADAR_CHART_MODEL_EFFICIENCY = "radar_chart_model_efficiency"  # 模型效率雷达图
HEATMAP_CHAT_ACTIVITY = "heatmap_chat_activity"  # 聊天活跃度热力图
DOUGHNUT_CHART_PROVIDER_REQUESTS = "doughnut_chart_provider_requests"  # 供应商请求占比环形图
LINE_CHART_COST_TREND = "line_chart_cost_trend"  # 成本趋势折线图
BAR_CHART_AVG_RESPONSE_TIME = "bar_chart_avg_response_time"  # 平均响应时间条形图

# 新增消息分析指标
MSG_CNT_BY_USER = "messages_by_user"  # 按用户的消息数
ACTIVE_CHATS_CNT = "active_chats_count"  # 活跃聊天数
MOST_ACTIVE_CHAT = "most_active_chat"  # 最活跃的聊天
AVG_MSG_PER_CHAT = "avg_messages_per_chat"  # 平均每个聊天的消息数

# 新增大模型效率指标
AVG_COST_PER_MSG = "avg_cost_per_message"  # 平均每条消息成本
AVG_TOKENS_PER_MSG = "avg_tokens_per_message"  # 平均每条消息Token数
AVG_TOKENS_PER_REQ = "avg_tokens_per_request"  # 平均每次请求Token数
MSG_TO_REQ_RATIO = "message_to_request_ratio"  # 消息/请求比率
COST_PER_ONLINE_HOUR = "cost_per_online_hour"  # 每小时在线成本
REQ_PER_ONLINE_HOUR = "requests_per_online_hour"  # 每小时请求数
TOKEN_EFFICIENCY = "token_efficiency"  # Token效率 (输出/输入比率)
