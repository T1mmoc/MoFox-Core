"""
该模块用于生成HTML格式的统计报告。
"""

import json
import os
from datetime import datetime, timedelta
from typing import Any

import aiofiles
from jinja2 import Environment, FileSystemLoader

from .statistic_keys import *  # noqa: F403


def format_online_time(online_seconds: int) -> str:
    """
    格式化在线时间。

    :param online_seconds: 在线时间（秒）。
    :return: 格式化后的在线时间字符串。
    """
    total_online_time = timedelta(seconds=online_seconds)
    days = total_online_time.days
    hours = total_online_time.seconds // 3600
    minutes = (total_online_time.seconds // 60) % 60
    seconds = total_online_time.seconds % 60
    if days > 0:
        return f"{days}天{hours}小时{minutes}分钟{seconds}秒"
    elif hours > 0:
        return f"{hours}小时{minutes}分钟{seconds}秒"
    else:
        return f"{minutes}分钟{seconds}秒"


class HTMLReportGenerator:
    """生成HTML统计报告"""

    def __init__(
        self,
        name_mapping: dict,
        stat_period: list,
        deploy_time: datetime,
    ):
        """
        初始化报告生成器。

        :param name_mapping: 聊天ID到名称的映射。
        :param stat_period: 统计时间段配置。
        :param deploy_time: 系统部署时间。
        """
        self.name_mapping = name_mapping
        self.stat_period = stat_period
        self.deploy_time = deploy_time
        # 初始化Jinja2环境
        template_dir = os.path.join(os.path.dirname(__file__), "templates")
        self.jinja_env = Environment(loader=FileSystemLoader(template_dir))

    def _format_stat_data_div(self, stat_data: dict[str, Any], div_id: str, start_time: datetime, now: datetime) -> str:
        """
        将单个时间段的统计数据格式化为HTML div块。

        :param stat_data: 统计数据。
        :param div_id: div的ID。
        :param start_time: 统计时间段的开始时间。
        :param now: 当前时间。
        :return: HTML字符串。
        """
        # 按模型分类统计
        model_rows = "\n".join(
            [
                f"<tr>"
                f"<td>{model_name}</td>"
                f"<td>{count}</td>"
                f"<td>{stat_data[AVG_TOK_BY_MODEL].get(model_name, 0)}</td>"
                f"<td>{stat_data[TOTAL_TOK_BY_MODEL].get(model_name, 0)}</td>"
                f"<td>{stat_data[TPS_BY_MODEL].get(model_name, 0):.2f}</td>"
                f"<td>{stat_data[COST_PER_KTOK_BY_MODEL].get(model_name, 0):.4f} ¥</td>"
                f"<td>{stat_data[COST_BY_MODEL].get(model_name, 0):.4f} ¥</td>"
                f"<td>{stat_data[AVG_TIME_COST_BY_MODEL].get(model_name, 0):.3f} 秒</td>"
                f"</tr>"
                for model_name, count in sorted(stat_data[REQ_CNT_BY_MODEL].items())
            ]
        )
        # 按供应商分类统计
        provider_rows = "\n".join(
            [
                f"<tr>"
                f"<td>{provider_name}</td>"
                f"<td>{count}</td>"
                f"<td>{stat_data[TOTAL_TOK_BY_PROVIDER].get(provider_name, 0)}</td>"
                f"<td>{stat_data[TPS_BY_PROVIDER].get(provider_name, 0):.2f}</td>"
                f"<td>{stat_data[COST_PER_KTOK_BY_PROVIDER].get(provider_name, 0):.4f} ¥</td>"
                f"<td>{stat_data[COST_BY_PROVIDER].get(provider_name, 0):.4f} ¥</td>"
                f"<td>{stat_data.get(AVG_TIME_COST_BY_PROVIDER, {}).get(provider_name, 0):.3f} 秒</td>"
                f"</tr>"
                for provider_name, count in sorted(stat_data[REQ_CNT_BY_PROVIDER].items())
            ]
        )
        # 按请求类型分类统计
        type_rows = "\n".join(
            [
                f"<tr>"
                f"<td>{req_type}</td>"
                f"<td>{count}</td>"
                f"<td>{stat_data[IN_TOK_BY_TYPE].get(req_type, 0)}</td>"
                f"<td>{stat_data[OUT_TOK_BY_TYPE].get(req_type, 0)}</td>"
                f"<td>{stat_data[TOTAL_TOK_BY_TYPE].get(req_type, 0)}</td>"
                f"<td>{stat_data[COST_BY_TYPE].get(req_type, 0):.4f} ¥</td>"
                f"<td>{stat_data[AVG_TIME_COST_BY_TYPE].get(req_type, 0):.3f} 秒</td>"
                f"<td>{stat_data[STD_TIME_COST_BY_TYPE].get(req_type, 0):.3f} 秒</td>"
                f"</tr>"
                for req_type, count in sorted(stat_data[REQ_CNT_BY_TYPE].items())
            ]
        )
        # 按模块分类统计
        module_rows = "\n".join(
            [
                f"<tr>"
                f"<td>{module_name}</td>"
                f"<td>{count}</td>"
                f"<td>{stat_data[IN_TOK_BY_MODULE].get(module_name, 0)}</td>"
                f"<td>{stat_data[OUT_TOK_BY_MODULE].get(module_name, 0)}</td>"
                f"<td>{stat_data[TOTAL_TOK_BY_MODULE].get(module_name, 0)}</td>"
                f"<td>{stat_data[COST_BY_MODULE].get(module_name, 0):.4f} ¥</td>"
                f"<td>{stat_data[AVG_TIME_COST_BY_MODULE].get(module_name, 0):.3f} 秒</td>"
                f"<td>{stat_data[STD_TIME_COST_BY_MODULE].get(module_name, 0):.3f} 秒</td>"
                f"</tr>"
                for module_name, count in sorted(stat_data[REQ_CNT_BY_MODULE].items())
            ]
        )
        # 聊天消息统计
        chat_rows = "\n".join(
            [
                f"<tr><td>{self.name_mapping.get(chat_id, ('未知', 0))[0]}</td><td>{count}</td></tr>"
                for chat_id, count in sorted(stat_data[MSG_CNT_BY_CHAT].items())
            ]
        )
        
        # 先计算基础数据
        total_tokens = sum(stat_data.get(TOTAL_TOK_BY_MODEL, {}).values())
        total_requests = stat_data.get(TOTAL_REQ_CNT, 0)
        total_cost = stat_data.get(TOTAL_COST, 0)
        total_messages = stat_data.get(TOTAL_MSG_CNT, 0)
        online_seconds = stat_data.get(ONLINE_TIME, 0)
        online_hours = online_seconds / 3600 if online_seconds > 0 else 0
        
        # 大模型相关效率指标
        avg_cost_per_req = (total_cost / total_requests) if total_requests > 0 else 0
        avg_cost_per_msg = (total_cost / total_messages) if total_messages > 0 else 0
        avg_tokens_per_msg = (total_tokens / total_messages) if total_messages > 0 else 0
        avg_tokens_per_req = (total_tokens / total_requests) if total_requests > 0 else 0
        msg_to_req_ratio = (total_messages / total_requests) if total_requests > 0 else 0
        cost_per_hour = (total_cost / online_hours) if online_hours > 0 else 0
        req_per_hour = (total_requests / online_hours) if online_hours > 0 else 0
        
        # Token效率 (输出/输入比率)
        total_in_tokens = sum(stat_data.get(IN_TOK_BY_MODEL, {}).values())
        total_out_tokens = sum(stat_data.get(OUT_TOK_BY_MODEL, {}).values())
        token_efficiency = (total_out_tokens / total_in_tokens) if total_in_tokens > 0 else 0
        
        # 生成效率指标表格数据
        efficiency_data = [
            ("💸 平均每条消息成本", f"{avg_cost_per_msg:.6f} ¥", "处理每条用户消息的平均AI成本"),
            ("🎯 平均每条消息Token", f"{avg_tokens_per_msg:.0f}", "每条消息平均消耗的Token数量"),
            ("📊 平均每次请求Token", f"{avg_tokens_per_req:.0f}", "每次AI请求平均消耗的Token数"),
            ("🔄 消息/请求比率", f"{msg_to_req_ratio:.2f}", "平均每个AI请求处理的消息数"),
            ("⚡ Token效率(输出/输入)", f"{token_efficiency:.3f}x", "输出Token与输入Token的比率"),
            ("💵 每小时运行成本", f"{cost_per_hour:.4f} ¥/h", "在线每小时的AI成本"),
            ("🚀 每小时请求数", f"{req_per_hour:.1f} 次/h", "在线每小时的AI请求次数"),
            ("💰 每千Token成本", f"{(total_cost / total_tokens * 1000) if total_tokens > 0 else 0:.4f} ¥", "平均每1000个Token的成本"),
            ("📈 Token/在线小时", f"{(total_tokens / online_hours) if online_hours > 0 else 0:.0f}", "每在线小时处理的Token数"),
            ("💬 消息/在线小时", f"{(total_messages / online_hours) if online_hours > 0 else 0:.1f}", "每在线小时处理的消息数"),
        ]
        
        efficiency_rows = "\n".join(
            [
                f"<tr><td style='font-weight: 500;'>{metric}</td><td style='color: #1976D2; font-weight: 600; font-size: 1.1em;'>{value}</td><td style='color: #546E7A;'>{desc}</td></tr>"
                for metric, value, desc in efficiency_data
            ]
        )
        
        # 计算活跃聊天数和最活跃聊天
        msg_by_chat = stat_data.get(MSG_CNT_BY_CHAT, {})
        active_chats = len(msg_by_chat)
        most_active_chat = ""
        if msg_by_chat:
            most_active_id = max(msg_by_chat, key=msg_by_chat.get)
            most_active_chat = self.name_mapping.get(most_active_id, (most_active_id, 0))[0]
            most_active_count = msg_by_chat[most_active_id]
            most_active_chat = f"{most_active_chat} ({most_active_count}条)"
        
        avg_msg_per_chat = (total_messages / active_chats) if active_chats > 0 else 0
        
        summary_cards = f"""
            <div class="summary-cards">
                <div class="card">
                    <h3>💰 总花费</h3>
                    <p>{total_cost:.4f} ¥</p>
                </div>
                <div class="card">
                    <h3>📞 AI请求数</h3>
                    <p>{total_requests:,}</p>
                </div>
                <div class="card">
                    <h3>🎯 总Token数</h3>
                    <p>{total_tokens:,}</p>
                </div>
                <div class="card">
                    <h3>💬 总消息数</h3>
                    <p>{total_messages:,}</p>
                </div>
                <div class="card">
                    <h3>⏱️ 在线时间</h3>
                    <p>{format_online_time(int(online_seconds))}</p>
                </div>
                <div class="card">
                    <h3>💸 每条消息成本</h3>
                    <p>{avg_cost_per_msg:.4f} ¥</p>
                </div>
                <div class="card">
                    <h3>📊 每请求Token</h3>
                    <p>{avg_tokens_per_req:.0f}</p>
                </div>
                <div class="card">
                    <h3>� 消息/请求比</h3>
                    <p>{msg_to_req_ratio:.2f}</p>
                </div>
                <div class="card">
                    <h3>⚡ Token效率</h3>
                    <p>{token_efficiency:.2f}x</p>
                </div>
                <div class="card">
                    <h3>💵 每小时成本</h3>
                    <p>{cost_per_hour:.4f} ¥</p>
                </div>
                <div class="card">
                    <h3>🚀 每小时请求</h3>
                    <p>{req_per_hour:.1f}</p>
                </div>
                <div class="card">
                    <h3>👥 活跃聊天数</h3>
                    <p>{active_chats}</p>
                </div>
                <div class="card">
                    <h3>🔥 最活跃聊天</h3>
                    <p style="font-size: 1.2em;">{most_active_chat if most_active_chat else "无"}</p>
                </div>
                <div class="card">
                    <h3>📈 平均消息/聊天</h3>
                    <p>{avg_msg_per_chat:.1f}</p>
                </div>
                <div class="card">
                    <h3>🎯 每消息Token</h3>
                    <p>{avg_tokens_per_msg:.0f}</p>
                </div>
            </div>
        """
        # 增加饼图和条形图
        static_charts = self._generate_static_charts_div(stat_data, div_id)
        template = self.jinja_env.get_template("tab_content.html")
        return template.render(
            div_id=div_id,
            start_time=start_time.strftime("%Y-%m-%d %H:%M:%S"),
            end_time=now.strftime("%Y-%m-%d %H:%M:%S"),
            summary_cards=summary_cards,
            static_charts=static_charts,
            model_rows=model_rows,
            provider_rows=provider_rows,
            module_rows=module_rows,
            type_rows=type_rows,
            chat_rows=chat_rows,
            efficiency_rows=efficiency_rows,
        )

    def _generate_chart_tab(self, chart_data: dict) -> str:
        """生成图表选项卡的HTML内容。"""
        template = self.jinja_env.get_template("charts_tab.html")
        return template.render()

    def _generate_static_charts_div(self, stat_data: dict[str, Any], div_id: str)-> str:
        """
        生成静态图表的HTML div。

        :param stat_data: 统计数据。
        :param div_id: The ID for the period, used to uniquely identify chart canvases.
        :return: 渲染后的HTML字符串。
        """
        template = self.jinja_env.get_template("static_charts.html")
        return template.render(period_id=div_id)

    async def generate_report(self, stat: dict[str, Any], chart_data: dict, now: datetime, output_path: str):
        """
        生成并写入完整的HTML报告文件。

        :param stat: 所有时间段的统计数据。
        :param chart_data: 用于图表的数据。
        :param now: 当前时间。
        :param output_path: 输出文件路径。
        """
        tab_list_html = [
            f'<button class="tab-link" onclick="showTab(event, \'{period[0]}\')">{period[2]}</button>'
            for period in self.stat_period
        ]
        tab_list_html.append('<button class="tab-link" onclick="showTab(event, \'charts\')">数据图表</button>')

        tab_content_html_list = [
            self._format_stat_data_div(stat[period[0]], period[0], now - period[1], now)
            for period in self.stat_period
            if period[0] != "all_time"
        ]
        tab_content_html_list.append(self._format_stat_data_div(stat["all_time"], "all_time", self.deploy_time, now))
        tab_content_html_list.append(self._generate_chart_tab(chart_data))

        static_chart_data = {}
        for period in self.stat_period:
            period_id = period[0]
            static_chart_data[period_id] = {
                "provider_cost_data": stat[period_id].get(PIE_CHART_COST_BY_PROVIDER, {}),
                "module_cost_data": stat[period_id].get(PIE_CHART_COST_BY_MODULE, {}),
                "model_cost_data": stat[period_id].get(BAR_CHART_COST_BY_MODEL, {}),
                "token_comparison_data": stat[period_id].get(BAR_CHART_TOKEN_COMPARISON, {}),
                "response_time_scatter_data": stat[period_id].get(SCATTER_CHART_RESPONSE_TIME, []),
                "model_efficiency_radar_data": stat[period_id].get(RADAR_CHART_MODEL_EFFICIENCY, {}),
                "provider_requests_data": stat[period_id].get(DOUGHNUT_CHART_PROVIDER_REQUESTS, {}),
                "avg_response_time_data": stat[period_id].get(BAR_CHART_AVG_RESPONSE_TIME, {}),
            }
        static_chart_data["all_time"] = {
            "provider_cost_data": stat["all_time"].get(PIE_CHART_COST_BY_PROVIDER, {}),
            "module_cost_data": stat["all_time"].get(PIE_CHART_COST_BY_MODULE, {}),
            "model_cost_data": stat["all_time"].get(BAR_CHART_COST_BY_MODEL, {}),
            "token_comparison_data": stat["all_time"].get(BAR_CHART_TOKEN_COMPARISON, {}),
            "response_time_scatter_data": stat["all_time"].get(SCATTER_CHART_RESPONSE_TIME, []),
            "model_efficiency_radar_data": stat["all_time"].get(RADAR_CHART_MODEL_EFFICIENCY, {}),
            "provider_requests_data": stat["all_time"].get(DOUGHNUT_CHART_PROVIDER_REQUESTS, {}),
            "avg_response_time_data": stat["all_time"].get(BAR_CHART_AVG_RESPONSE_TIME, {}),
        }

        # 渲染模板
        # 读取CSS和JS文件内容
        async with aiofiles.open(os.path.join(self.jinja_env.loader.searchpath[0], "report.css"), encoding="utf-8") as f:
            report_css = await f.read()
        async with aiofiles.open(os.path.join(self.jinja_env.loader.searchpath[0], "report.js"), encoding="utf-8") as f:
            report_js = await f.read()
        # 渲染模板（使用紧凑的JSON格式减少文件大小）
        template = self.jinja_env.get_template("report.html")
        rendered_html = template.render(
            report_title="MoFox-Bot运行统计报告",
            generation_time=now.strftime("%Y-%m-%d %H:%M:%S"),
            tab_list="\n".join(tab_list_html),
            tab_content="\n".join(tab_content_html_list),
            all_chart_data=json.dumps(chart_data, separators=(',', ':'), ensure_ascii=False),
            static_chart_data=json.dumps(static_chart_data, separators=(',', ':'), ensure_ascii=False),
            report_css=report_css,
            report_js=report_js,
        )

        async with aiofiles.open(output_path, "w", encoding="utf-8") as f:
            await f.write(rendered_html)
