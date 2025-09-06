"""
Bing search engine implementation
"""
import asyncio
import functools
import random
import traceback
from typing import Dict, List, Any
import requests
from bs4 import BeautifulSoup

from src.common.logger import get_logger
from .base import BaseSearchEngine

logger = get_logger("bing_engine")

ABSTRACT_MAX_LENGTH = 300  # abstract max length

user_agents = [
    # Edge浏览器
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 Edg/122.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36 Edg/121.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 Edg/122.0.0.0",
    # Chrome浏览器
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    # Firefox浏览器
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
]

# 请求头信息
HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
    "Cache-Control": "max-age=0",
    "Connection": "keep-alive",
    "Host": "www.bing.com",
    "Referer": "https://www.bing.com/",
    "Sec-Ch-Ua": '"Chromium";v="122", "Microsoft Edge";v="122", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 Edg/122.0.0.0",
}

bing_search_url = "https://www.bing.com/search?q="


class BingSearchEngine(BaseSearchEngine):
    """
    Bing搜索引擎实现
    """
    
    def __init__(self):
        self.session = requests.Session()
        self.session.headers = HEADERS
    
    def is_available(self) -> bool:
        """检查Bing搜索引擎是否可用"""
        return True  # Bing是免费搜索引擎，总是可用
    
    async def search(self, args: Dict[str, Any]) -> List[Dict[str, Any]]:
        """执行Bing搜索"""
        query = args["query"]
        num_results = args.get("num_results", 3)
        time_range = args.get("time_range", "any")
        
        try:
            loop = asyncio.get_running_loop()
            func = functools.partial(self._search_sync, query, num_results, time_range)
            search_response = await loop.run_in_executor(None, func)
            return search_response
        except Exception as e:
            logger.error(f"Bing 搜索失败: {e}")
            return []
    
    def _search_sync(self, keyword: str, num_results: int, time_range: str) -> List[Dict[str, Any]]:
        """同步执行Bing搜索"""
        if not keyword:
            return []

        list_result = []
        
        # 构建搜索URL
        search_url = bing_search_url + keyword
        
        # 如果指定了时间范围，添加时间过滤参数
        if time_range == "week":
            search_url += "&qft=+filterui:date-range-7"
        elif time_range == "month":
            search_url += "&qft=+filterui:date-range-30"

        try:
            data = self._parse_html(search_url)
            if data:
                list_result.extend(data)
                logger.debug(f"Bing搜索 [{keyword}] 找到 {len(data)} 个结果")

        except Exception as e:
            logger.error(f"Bing搜索解析失败: {e}")
            return []

        logger.debug(f"Bing搜索 [{keyword}] 完成，总共 {len(list_result)} 个结果")
        return list_result[:num_results] if len(list_result) > num_results else list_result

    def _parse_html(self, url: str) -> List[Dict[str, Any]]:
        """解析处理结果"""
        try:
            logger.debug(f"访问Bing搜索URL: {url}")

            # 设置必要的Cookie
            cookies = {
                "SRCHHPGUSR": "SRCHLANG=zh-Hans",  # 设置默认搜索语言为中文
                "SRCHD": "AF=NOFORM",
                "SRCHUID": "V=2&GUID=1A4D4F1C8844493F9A2E3DB0D1BC806C",
                "_SS": "SID=0D89D9A3C95C60B62E7AC80CC85461B3",
                "_EDGE_S": "ui=zh-cn",  # 设置界面语言为中文
                "_EDGE_V": "1",
            }

            # 为每次请求随机选择不同的用户代理，降低被屏蔽风险
            headers = HEADERS.copy()
            headers["User-Agent"] = random.choice(user_agents)

            # 创建新的session
            session = requests.Session()
            session.headers.update(headers)
            session.cookies.update(cookies)

            # 发送请求
            try:
                res = session.get(url=url, timeout=(3.05, 6), verify=True, allow_redirects=True)
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                logger.warning(f"第一次请求超时，正在重试: {str(e)}")
                try:
                    res = session.get(url=url, timeout=(5, 10), verify=False)
                except Exception as e2:
                    logger.error(f"第二次请求也失败: {str(e2)}")
                    return []

            res.encoding = "utf-8"

            # 检查响应状态
            if res.status_code == 403:
                logger.error("被禁止访问 (403 Forbidden)，可能是IP被限制")
                return []

            if res.status_code != 200:
                logger.error(f"必应搜索请求失败，状态码: {res.status_code}")
                return []

            # 检查是否被重定向到登录页面或验证页面
            if "login.live.com" in res.url or "login.microsoftonline.com" in res.url:
                logger.error("被重定向到登录页面，可能需要登录")
                return []

            if "https://www.bing.com/ck/a" in res.url:
                logger.error("被重定向到验证页面，可能被识别为机器人")
                return []

            # 解析HTML
            try:
                root = BeautifulSoup(res.text, "lxml")
            except Exception:
                try:
                    root = BeautifulSoup(res.text, "html.parser")
                except Exception as e:
                    logger.error(f"HTML解析失败: {str(e)}")
                    return []

            list_data = []

            # 尝试提取搜索结果
            # 方法1: 查找标准的搜索结果容器
            results = root.select("ol#b_results li.b_algo")
            
            if results:
                for _rank, result in enumerate(results, 1):
                    # 提取标题和链接
                    title_link = result.select_one("h2 a")
                    if not title_link:
                        continue
                    
                    title = title_link.get_text().strip()
                    url = title_link.get("href", "")
                    
                    # 提取摘要
                    abstract = ""
                    abstract_elem = result.select_one("div.b_caption p")
                    if abstract_elem:
                        abstract = abstract_elem.get_text().strip()
                    
                    # 限制摘要长度
                    if ABSTRACT_MAX_LENGTH and len(abstract) > ABSTRACT_MAX_LENGTH:
                        abstract = abstract[:ABSTRACT_MAX_LENGTH] + "..."
                    
                    list_data.append({
                        "title": title,
                        "url": url,
                        "snippet": abstract,
                        "provider": "Bing"
                    })
                    
                    if len(list_data) >= 10:  # 限制结果数量
                        break

            # 方法2: 如果标准方法没找到结果，使用备用方法
            if not list_data:
                # 查找所有可能的搜索结果链接
                all_links = root.find_all("a")
                
                for link in all_links:
                    href = link.get("href", "")
                    text = link.get_text().strip()
                    
                    # 过滤有效的搜索结果链接
                    if (href and text and len(text) > 10 
                        and not href.startswith("javascript:")
                        and not href.startswith("#")
                        and "http" in href
                        and not any(x in href for x in [
                            "bing.com/search", "bing.com/images", "bing.com/videos",
                            "bing.com/maps", "bing.com/news", "login", "account",
                            "microsoft", "javascript"
                        ])):
                        
                        # 尝试获取摘要
                        abstract = ""
                        parent = link.parent
                        if parent and parent.get_text():
                            full_text = parent.get_text().strip()
                            if len(full_text) > len(text):
                                abstract = full_text.replace(text, "", 1).strip()
                        
                        # 限制摘要长度
                        if ABSTRACT_MAX_LENGTH and len(abstract) > ABSTRACT_MAX_LENGTH:
                            abstract = abstract[:ABSTRACT_MAX_LENGTH] + "..."
                        
                        list_data.append({
                            "title": text,
                            "url": href,
                            "snippet": abstract,
                            "provider": "Bing"
                        })
                        
                        if len(list_data) >= 10:
                            break

            logger.debug(f"从Bing解析到 {len(list_data)} 个搜索结果")
            return list_data

        except Exception as e:
            logger.error(f"解析Bing页面时出错: {str(e)}")
            logger.debug(traceback.format_exc())
            return []
