import asyncio
import functools
import itertools
from typing import Any, Dict, List
from datetime import datetime, timedelta
from exa_py import Exa
from asyncddgs import aDDGS
from tavily import TavilyClient
from .bing_search import BingSearch

from src.common.logger import get_logger
from typing import Tuple,Type
from src.plugin_system import (
    BasePlugin,
    register_plugin,
    BaseTool,
    ComponentInfo,
    ConfigField,
    llm_api,
    ToolParamType,
    PythonDependency
)
from src.plugin_system.apis import config_api  # 添加config_api导入
from src.common.cache_manager import tool_cache
import httpx
from bs4 import BeautifulSoup

logger = get_logger("web_surfing_tool")


class WebSurfingTool(BaseTool):
    name: str = "web_search"
    description: str = "用于执行网络搜索。当用户明确要求搜索，或者需要获取关于公司、产品、事件的最新信息、新闻或动态时，必须使用此工具"
    available_for_llm: bool = True
    parameters = [
        ("query", ToolParamType.STRING, "要搜索的关键词或问题。", True, None),
        ("num_results", ToolParamType.INTEGER, "期望每个搜索引擎返回的搜索结果数量，默认为5。", False, None),
        ("time_range", ToolParamType.STRING, "指定搜索的时间范围，可以是 'any', 'week', 'month'。默认为 'any'。", False, ["any", "week", "month"])
    ] # type: ignore

    def __init__(self, plugin_config=None):
        super().__init__(plugin_config)
        self.bing_search = BingSearch()
        
        # 初始化EXA API密钥轮询器
        self.exa_clients = []
        self.exa_key_cycle = None
        
        # 优先从主配置文件读取，如果没有则从插件配置文件读取
        EXA_API_KEYS = config_api.get_global_config("exa.api_keys", None)
        if EXA_API_KEYS is None:
            # 从插件配置文件读取
            EXA_API_KEYS = self.get_config("exa.api_keys", [])
        
        if isinstance(EXA_API_KEYS, list) and EXA_API_KEYS:
            valid_keys = [key.strip() for key in EXA_API_KEYS if isinstance(key, str) and key.strip() not in ("None", "")]
            if valid_keys:
                self.exa_clients = [Exa(api_key=key) for key in valid_keys]
                self.exa_key_cycle = itertools.cycle(self.exa_clients)
                logger.info(f"已配置 {len(valid_keys)} 个 Exa API 密钥")
            else:
                logger.warning("Exa API Keys 配置无效，Exa 搜索功能将不可用。")
        else:
            logger.warning("Exa API Keys 未配置，Exa 搜索功能将不可用。")

        # 初始化Tavily API密钥轮询器
        self.tavily_clients = []
        self.tavily_key_cycle = None
        
        # 优先从主配置文件读取，如果没有则从插件配置文件读取
        TAVILY_API_KEYS = config_api.get_global_config("tavily.api_keys", None)
        if TAVILY_API_KEYS is None:
            # 从插件配置文件读取
            TAVILY_API_KEYS = self.get_config("tavily.api_keys", [])
        
        if isinstance(TAVILY_API_KEYS, list) and TAVILY_API_KEYS:
            valid_keys = [key.strip() for key in TAVILY_API_KEYS if isinstance(key, str) and key.strip() not in ("None", "")]
            if valid_keys:
                self.tavily_clients = [TavilyClient(api_key=key) for key in valid_keys]
                self.tavily_key_cycle = itertools.cycle(self.tavily_clients)
                logger.info(f"已配置 {len(valid_keys)} 个 Tavily API 密钥")
            else:
                logger.warning("Tavily API Keys 配置无效，Tavily 搜索功能将不可用。")
        else:
            logger.warning("Tavily API Keys 未配置，Tavily 搜索功能将不可用。")

    async def execute(self, function_args: Dict[str, Any]) -> Dict[str, Any]:
        query = function_args.get("query")
        if not query:
            return {"error": "搜索查询不能为空。"}

        # 获取当前文件路径用于缓存键
        import os
        current_file_path = os.path.abspath(__file__)

        # 检查缓存
        query = function_args.get("query")
        cached_result = await tool_cache.get(self.name, function_args, current_file_path, semantic_query=query)
        if cached_result:
            logger.info(f"缓存命中: {self.name} -> {function_args}")
            return cached_result

        # 读取搜索配置
        enabled_engines = config_api.get_global_config("web_search.enabled_engines", ["ddg"])
        search_strategy = config_api.get_global_config("web_search.search_strategy", "single")
        
        logger.info(f"开始搜索，策略: {search_strategy}, 启用引擎: {enabled_engines}, 参数: '{function_args}'")

        # 根据策略执行搜索
        if search_strategy == "parallel":
            result = await self._execute_parallel_search(function_args, enabled_engines)
        elif search_strategy == "fallback":
            result = await self._execute_fallback_search(function_args, enabled_engines)
        else:  # single
            result = await self._execute_single_search(function_args, enabled_engines)
        
        # 保存到缓存
        if "error" not in result:
            query = function_args.get("query")
            await tool_cache.set(self.name, function_args, current_file_path, result, semantic_query=query)
            
        return result

    async def _execute_parallel_search(self, function_args: Dict[str, Any], enabled_engines: List[str]) -> Dict[str, Any]:
        """并行搜索策略：同时使用所有启用的搜索引擎"""
        search_tasks = []
        
        for engine in enabled_engines:
            custom_args = function_args.copy()
            custom_args["num_results"] = custom_args.get("num_results", 5)
            if engine == "exa" and self.exa_clients:
                # 使用参数中的数量，如果没有则默认5个
                search_tasks.append(self._search_exa(custom_args))
            elif engine == "tavily" and self.tavily_clients:
                search_tasks.append(self._search_tavily(custom_args))
            elif engine == "ddg":
                search_tasks.append(self._search_ddg(custom_args))
            elif engine == "bing":
                search_tasks.append(self._search_bing(custom_args))

        if not search_tasks:
            return {"error": "没有可用的搜索引擎。"}

        try:
            search_results_lists = await asyncio.gather(*search_tasks, return_exceptions=True)
            
            all_results = []
            for result in search_results_lists:
                if isinstance(result, list):
                    all_results.extend(result)
                elif isinstance(result, Exception):
                    logger.error(f"搜索时发生错误: {result}")

            # 去重并格式化
            unique_results = self._deduplicate_results(all_results)
            formatted_content = self._format_results(unique_results)
            
            return {
                "type": "web_search_result",
                "content": formatted_content,
            }

        except Exception as e:
            logger.error(f"执行并行网络搜索时发生异常: {e}", exc_info=True)
            return {"error": f"执行网络搜索时发生严重错误: {str(e)}"}

    async def _execute_fallback_search(self, function_args: Dict[str, Any], enabled_engines: List[str]) -> Dict[str, Any]:
        """回退搜索策略：按顺序尝试搜索引擎，失败则尝试下一个"""
        for engine in enabled_engines:
            try:
                custom_args = function_args.copy()
                custom_args["num_results"] = custom_args.get("num_results", 5)
                
                if engine == "exa" and self.exa_clients:
                    results = await self._search_exa(custom_args)
                elif engine == "tavily" and self.tavily_clients:
                    results = await self._search_tavily(custom_args)
                elif engine == "ddg":
                    results = await self._search_ddg(custom_args)
                elif engine == "bing":
                    results = await self._search_bing(custom_args)
                else:
                    continue
                
                if results:  # 如果有结果，直接返回
                    formatted_content = self._format_results(results)
                    return {
                        "type": "web_search_result",
                        "content": formatted_content,
                    }
                    
            except Exception as e:
                logger.warning(f"{engine} 搜索失败，尝试下一个引擎: {e}")
                continue
        
        return {"error": "所有搜索引擎都失败了。"}

    async def _execute_single_search(self, function_args: Dict[str, Any], enabled_engines: List[str]) -> Dict[str, Any]:
        """单一搜索策略：只使用第一个可用的搜索引擎"""
        for engine in enabled_engines:
            custom_args = function_args.copy()
            custom_args["num_results"] = custom_args.get("num_results", 5)
            
            try:
                if engine == "exa" and self.exa_clients:
                    results = await self._search_exa(custom_args)
                elif engine == "tavily" and self.tavily_clients:
                    results = await self._search_tavily(custom_args)
                elif engine == "ddg":
                    results = await self._search_ddg(custom_args)
                elif engine == "bing":
                    results = await self._search_bing(custom_args)
                else:
                    continue
                
                formatted_content = self._format_results(results)
                return {
                    "type": "web_search_result",
                    "content": formatted_content,
                }
                
            except Exception as e:
                logger.error(f"{engine} 搜索失败: {e}")
                return {"error": f"{engine} 搜索失败: {str(e)}"}
        
        return {"error": "没有可用的搜索引擎。"}

    def _deduplicate_results(self, results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        unique_urls = set()
        unique_results = []
        for res in results:
            if isinstance(res, dict) and res.get("url") and res["url"] not in unique_urls:
                unique_urls.add(res["url"])
                unique_results.append(res)
        return unique_results

    async def _search_exa(self, args: Dict[str, Any]) -> List[Dict[str, Any]]:
        query = args["query"]
        num_results = args.get("num_results", 3)
        time_range = args.get("time_range", "any")

        exa_args = {"num_results": num_results, "text": True, "highlights": True}
        if time_range != "any":
            today = datetime.now()
            start_date = today - timedelta(days=7 if time_range == "week" else 30)
            exa_args["start_published_date"] = start_date.strftime('%Y-%m-%d')

        try:
            if not self.exa_key_cycle:
                return []
            
            # 使用轮询机制获取下一个客户端
            exa_client = next(self.exa_key_cycle)
            loop = asyncio.get_running_loop()
            func = functools.partial(exa_client.search_and_contents, query, **exa_args)
            search_response = await loop.run_in_executor(None, func)
            
            return [
                {
                    "title": res.title,
                    "url": res.url,
                    "snippet": " ".join(getattr(res, 'highlights', [])) or (getattr(res, 'text', '')[:250] + '...'),
                    "provider": "Exa"
                }
                for res in search_response.results
            ]
        except Exception as e:
            logger.error(f"Exa 搜索失败: {e}")
            return []

    async def _search_tavily(self, args: Dict[str, Any]) -> List[Dict[str, Any]]:
        query = args["query"]
        num_results = args.get("num_results", 3)
        time_range = args.get("time_range", "any")

        try:
            if not self.tavily_key_cycle:
                return []
            
            # 使用轮询机制获取下一个客户端
            tavily_client = next(self.tavily_key_cycle)
            
            # 构建Tavily搜索参数
            search_params = {
                "query": query,
                "max_results": num_results,
                "search_depth": "basic",
                "include_answer": False,
                "include_raw_content": False
            }
            
            # 根据时间范围调整搜索参数
            if time_range == "week":
                search_params["days"] = 7
            elif time_range == "month":
                search_params["days"] = 30
            
            loop = asyncio.get_running_loop()
            func = functools.partial(tavily_client.search, **search_params)
            search_response = await loop.run_in_executor(None, func)
            
            results = []
            if search_response and "results" in search_response:
                for res in search_response["results"]:
                    results.append({
                        "title": res.get("title", "无标题"),
                        "url": res.get("url", ""),
                        "snippet": res.get("content", "")[:300] + "..." if res.get("content") else "无摘要",
                        "provider": "Tavily"
                    })
            
            return results
            
        except Exception as e:
            logger.error(f"Tavily 搜索失败: {e}")
            return []

    async def _search_ddg(self, args: Dict[str, Any]) -> List[Dict[str, Any]]:
        query = args["query"]
        num_results = args.get("num_results", 3)
        
        try:
            async with aDDGS() as ddgs:
                search_response = await ddgs.text(query, max_results=num_results)
            
            return [
                {
                    "title": r.get("title"),
                    "url": r.get("href"),
                    "snippet": r.get("body"),
                    "provider": "DuckDuckGo"
                }
                for r in search_response
            ]
        except Exception as e:
            logger.error(f"DuckDuckGo 搜索失败: {e}")
            return []

    async def _search_bing(self, args: Dict[str, Any]) -> List[Dict[str, Any]]:
        query = args["query"]
        num_results = args.get("num_results", 3)
        
        try:
            loop = asyncio.get_running_loop()
            func = functools.partial(self.bing_search.search, query, num_results=num_results)
            search_response = await loop.run_in_executor(None, func)
            if search_response:
                return [
                    {
                        "title": r.get("title"),
                        "url": r.get("url"),
                        "snippet": r.get("abstract"),
                        "provider": "Bing"
                    }
                    for r in search_response
                ]
        except Exception as e:
            logger.error(f"Bing 搜索失败: {e}")
            return []

    def _format_results(self, results: List[Dict[str, Any]]) -> str:
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
    
class URLParserTool(BaseTool):
    """
    一个用于解析和总结一个或多个网页URL内容的工具。
    """
    name: str = "parse_url"
    description: str = "当需要理解一个或多个特定网页链接的内容时，使用此工具。例如：'这些网页讲了什么？[https://example.com, https://example2.com]' 或 '帮我总结一下这些文章'"
    available_for_llm: bool = True
    parameters = [
        ("urls", ToolParamType.STRING, "要理解的网站", True, None),
    ]
    def __init__(self, plugin_config=None):
        super().__init__(plugin_config)
        
        # 初始化EXA API密钥轮询器
        self.exa_clients = []
        self.exa_key_cycle = None
        
        # 优先从主配置文件读取，如果没有则从插件配置文件读取
        EXA_API_KEYS = config_api.get_global_config("exa.api_keys", None)
        if EXA_API_KEYS is None:
            # 从插件配置文件读取
            EXA_API_KEYS = self.get_config("exa.api_keys", [])
        
        if isinstance(EXA_API_KEYS, list) and EXA_API_KEYS:
            valid_keys = [key.strip() for key in EXA_API_KEYS if isinstance(key, str) and key.strip() not in ("None", "")]
            if valid_keys:
                self.exa_clients = [Exa(api_key=key) for key in valid_keys]
                self.exa_key_cycle = itertools.cycle(self.exa_clients)
                logger.info(f"URL解析工具已配置 {len(valid_keys)} 个 Exa API 密钥")
            else:
                logger.warning("Exa API Keys 配置无效，URL解析功能将受限。")
        else:
            logger.warning("Exa API Keys 未配置，URL解析功能将受限。")
    async def _local_parse_and_summarize(self, url: str) -> Dict[str, Any]:
        """
        使用本地库(httpx, BeautifulSoup)解析URL，并调用LLM进行总结。
        """
        try:
            # 读取代理配置
            enable_proxy = self.get_config("proxy.enable_proxy", False)
            proxies = None
            
            if enable_proxy:
                socks5_proxy = self.get_config("proxy.socks5_proxy", None)
                http_proxy = self.get_config("proxy.http_proxy", None)
                https_proxy = self.get_config("proxy.https_proxy", None)
                
                # 优先使用SOCKS5代理（全协议代理）
                if socks5_proxy:
                    proxies = socks5_proxy
                    logger.info(f"使用SOCKS5代理: {socks5_proxy}")
                elif http_proxy or https_proxy:
                    proxies = {}
                    if http_proxy:
                        proxies["http://"] = http_proxy
                    if https_proxy:
                        proxies["https://"] = https_proxy
                    logger.info(f"使用HTTP/HTTPS代理配置: {proxies}")
            
            client_kwargs = {"timeout": 15.0, "follow_redirects": True}
            if proxies:
                client_kwargs["proxies"] = proxies
            
            async with httpx.AsyncClient(**client_kwargs) as client:
                response = await client.get(url)
                response.raise_for_status()

            soup = BeautifulSoup(response.text, "html.parser")
            
            title = soup.title.string if soup.title else "无标题"
            for script in soup(["script", "style"]):
                script.extract()
            text = soup.get_text(separator="\n", strip=True)

            if not text:
                return {"error": "无法从页面提取有效文本内容。"}

            summary_prompt = f"请根据以下网页内容，生成一段不超过300字的中文摘要，保留核心信息和关键点:\n\n---\n\n标题: {title}\n\n内容:\n{text[:4000]}\n\n---\n\n摘要:"


            text_model = str(self.get_config("models.text_model", "replyer_1"))
            models = llm_api.get_available_models()
            model_config = models.get(text_model)
            if not model_config:
                logger.error("未配置LLM模型")
                return {"error": "未配置LLM模型"}

            success, summary, reasoning, model_name = await llm_api.generate_with_model(
                    prompt=summary_prompt,
                    model_config=model_config,
                    request_type="story.generate",
                    temperature=0.3,
                    max_tokens=1000
                )

            if not success:
                logger.info(f"生成摘要失败: {summary}")
                return {"error": "发生ai错误"}

            logger.info(f"成功生成摘要内容：'{summary}'")

            return {
                "title": title,
                "url": url,
                "snippet": summary,
                "source": "local"
            }

        except httpx.HTTPStatusError as e:
            logger.warning(f"本地解析URL '{url}' 失败 (HTTP {e.response.status_code})")
            return {"error": f"请求失败，状态码: {e.response.status_code}"}
        except Exception as e:
            logger.error(f"本地解析或总结URL '{url}' 时发生未知异常: {e}", exc_info=True)
            return {"error": f"发生未知错误: {str(e)}"}

    async def execute(self, function_args: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行URL内容提取和总结。优先使用Exa，失败后尝试本地解析。
        """
        # 获取当前文件路径用于缓存键
        import os
        current_file_path = os.path.abspath(__file__)

        # 检查缓存
        cached_result = await tool_cache.get(self.name, function_args, current_file_path)
        if cached_result:
            logger.info(f"缓存命中: {self.name} -> {function_args}")
            return cached_result
            
        urls_input = function_args.get("urls")
        if not urls_input:
            return {"error": "URL列表不能为空。"}

        # 处理URL输入，确保是列表格式
        if isinstance(urls_input, str):
            # 如果是字符串，尝试解析为URL列表
            import re
            # 提取所有HTTP/HTTPS URL
            url_pattern = r'https?://[^\s\],]+'
            urls = re.findall(url_pattern, urls_input)
            if not urls:
                # 如果没有找到标准URL，将整个字符串作为单个URL
                if urls_input.strip().startswith(('http://', 'https://')):
                    urls = [urls_input.strip()]
                else:
                    return {"error": "提供的字符串中未找到有效的URL。"}
        elif isinstance(urls_input, list):
            urls = [url.strip() for url in urls_input if isinstance(url, str) and url.strip()]
        else:
            return {"error": "URL格式不正确，应为字符串或列表。"}

        # 验证URL格式
        valid_urls = []
        for url in urls:
            if url.startswith(('http://', 'https://')):
                valid_urls.append(url)
            else:
                logger.warning(f"跳过无效URL: {url}")
        
        if not valid_urls:
            return {"error": "未找到有效的URL。"}
        
        urls = valid_urls
        logger.info(f"准备解析 {len(urls)} 个URL: {urls}")

        successful_results = []
        error_messages = []
        urls_to_retry_locally = []
        
        # 步骤 1: 尝试使用 Exa API 进行解析
        contents_response = None
        if self.exa_key_cycle:
            logger.info(f"开始使用 Exa API 解析URL: {urls}")
            try:
                # 使用轮询机制获取下一个客户端
                exa_client = next(self.exa_key_cycle)
                loop = asyncio.get_running_loop()
                exa_params = {"text": True, "summary": True, "highlights": True}
                func = functools.partial(exa_client.get_contents, urls, **exa_params)
                contents_response = await loop.run_in_executor(None, func)
            except Exception as e:
                logger.error(f"执行 Exa URL解析时发生严重异常: {e}", exc_info=True)
                contents_response = None # 确保异常后为None

        # 步骤 2: 处理Exa的响应
        if contents_response and hasattr(contents_response, 'statuses'):
            results_map = {res.url: res for res in contents_response.results} if hasattr(contents_response, 'results') else {}
            if contents_response.statuses:
                for status in contents_response.statuses:
                    if status.status == 'success':
                        res = results_map.get(status.id)
                        if res:
                            summary = getattr(res, 'summary', '')
                            highlights = " ".join(getattr(res, 'highlights', []))
                            text_snippet = (getattr(res, 'text', '')[:300] + '...') if getattr(res, 'text', '') else ''
                            snippet = summary or highlights or text_snippet or '无摘要'
                            
                            successful_results.append({
                                "title": getattr(res, 'title', '无标题'),
                                "url": getattr(res, 'url', status.id),
                                "snippet": snippet,
                                "source": "exa"
                            })
                    else:
                        error_tag = getattr(status, 'error', '未知错误')
                        logger.warning(f"Exa解析URL '{status.id}' 失败: {error_tag}。准备本地重试。")
                        urls_to_retry_locally.append(status.id)
        else:
            # 如果Exa未配置、API调用失败或返回无效响应，则所有URL都进入本地重试
            urls_to_retry_locally.extend(url for url in urls if url not in [res['url'] for res in successful_results])


        # 步骤 3: 对失败的URL进行本地解析
        if urls_to_retry_locally:
            logger.info(f"开始本地解析以下URL: {urls_to_retry_locally}")
            local_tasks = [self._local_parse_and_summarize(url) for url in urls_to_retry_locally]
            local_results = await asyncio.gather(*local_tasks)
            
            for i, res in enumerate(local_results):
                url = urls_to_retry_locally[i]
                if "error" in res:
                    error_messages.append(f"URL: {url} - 解析失败: {res['error']}")
                else:
                    successful_results.append(res)

        if not successful_results:
            return {"error": "无法从所有给定的URL获取内容。", "details": error_messages}

        formatted_content = self._format_results(successful_results)
        
        result = {
            "type": "url_parse_result",
            "content": formatted_content,
            "errors": error_messages
        }
        
        # 保存到缓存
        import os
        current_file_path = os.path.abspath(__file__)
        if "error" not in result:
            await tool_cache.set(self.name, function_args, current_file_path, result)

        return result

    def _format_results(self, results: List[Dict[str, Any]]) -> str:
        """
        将成功解析的结果列表格式化为一段简洁的文本。
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

@register_plugin
class WEBSEARCHPLUGIN(BasePlugin):

    # 插件基本信息
    plugin_name: str = "web_search_tool"  # 内部标识符
    enable_plugin: bool = True
    dependencies: List[str] = []  # 插件依赖列表
    # Python包依赖列表 - 支持两种格式：
    # 方式1: 简单字符串列表（向后兼容）
    # python_dependencies: List[str] = ["asyncddgs", "exa_py", "httpx[socks]"]
    
    # 方式2: 详细的PythonDependency对象（推荐）
    python_dependencies: List[PythonDependency] = [
        PythonDependency(
            package_name="asyncddgs",
            description="异步DuckDuckGo搜索库",
            optional=False
        ),
        PythonDependency(
            package_name="exa_py",
            description="Exa搜索API客户端库",
            optional=True  # 如果没有API密钥，这个是可选的
        ),
        PythonDependency(
            package_name="tavily",
            install_name="tavily-python",  # 安装时使用这个名称
            description="Tavily搜索API客户端库",
            optional=True  # 如果没有API密钥，这个是可选的
        ),
        PythonDependency(
            package_name="httpx",
            version=">=0.20.0",
            install_name="httpx[socks]",  # 安装时使用这个名称（包含可选依赖）
            description="支持SOCKS代理的HTTP客户端库",
            optional=False
        )
    ]
    config_file_name: str = "config.toml"  # 配置文件名

    # 配置节描述
    config_section_descriptions = {"plugin": "插件基本信息", "proxy": "链接本地解析代理配置"}

    # 配置Schema定义
    # 注意：EXA配置和组件设置已迁移到主配置文件(bot_config.toml)的[exa]和[web_search]部分
    config_schema: dict = {
        "plugin": {
            "name": ConfigField(type=str, default="WEB_SEARCH_PLUGIN", description="插件名称"),
            "version": ConfigField(type=str, default="1.0.0", description="插件版本"),
            "enabled": ConfigField(type=bool, default=False, description="是否启用插件"),
        },
        "proxy": {
            "http_proxy": ConfigField(type=str, default=None, description="HTTP代理地址，格式如: http://proxy.example.com:8080"),
            "https_proxy": ConfigField(type=str, default=None, description="HTTPS代理地址，格式如: http://proxy.example.com:8080"),
            "socks5_proxy": ConfigField(type=str, default=None, description="SOCKS5代理地址，格式如: socks5://proxy.example.com:1080"),
            "enable_proxy": ConfigField(type=bool, default=False, description="是否启用代理")
        },
    }
    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        enable_tool =[]
        # 从主配置文件读取组件启用配置
        if config_api.get_global_config("web_search.enable_web_search_tool", True):
            enable_tool.append((WebSurfingTool.get_tool_info(), WebSurfingTool))
        if config_api.get_global_config("web_search.enable_url_tool", True):
            enable_tool.append((URLParserTool.get_tool_info(), URLParserTool))
        return enable_tool
