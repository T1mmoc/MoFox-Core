"""
Formatters for web search results
"""
from typing import List, Dict, Any


def format_search_results(results: List[Dict[str, Any]]) -> str:
    """
    格式化搜索结果为字符串
    """
    if not results:
        return "没有找到相关的网络信息。"

    formatted_string = "根据网络搜索结果：\n\n"
    for i, res in enumerate(results, 1):
        title = res.get("title", '无标题')
        url = res.get("url", '#')
        snippet = res.get("snippet", '无摘要')
        provider = res.get("provider", "未知来源")
        
        formatted_string += f"{i}. **{title}** (来自: {provider})\n"
        formatted_string += f"   - 摘要: {snippet}\n"
        formatted_string += f"   - 来源: {url}\n\n"
        
    return formatted_string


def format_url_parse_results(results: List[Dict[str, Any]]) -> str:
    """
    将成功解析的URL结果列表格式化为一段简洁的文本。
    """
    formatted_parts = []
    for res in results:
        title = res.get('title', '无标题')
        url = res.get('url', '#')
        snippet = res.get('snippet', '无摘要')
        source = res.get('source', '未知')

        formatted_string = f"**{title}**\n"
        formatted_string += f"**内容摘要**:\n{snippet}\n"
        formatted_string += f"**来源**: {url} (由 {source} 解析)\n"
        formatted_parts.append(formatted_string)

    return "\n---\n".join(formatted_parts)


def deduplicate_results(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    根据URL去重搜索结果
    """
    unique_urls = set()
    unique_results = []
    for res in results:
        if isinstance(res, dict) and res.get("url") and res["url"] not in unique_urls:
            unique_urls.add(res["url"])
            unique_results.append(res)
    return unique_results
