import base64
import json
import os
import random
import time
import datetime
import traceback
from typing import List, Dict, Any, Optional
from pathlib import Path

import httpx
import requests
import asyncio
import bs4
import json5

from src.chat.utils.utils_image import get_image_manager
from src.common.logger import get_logger
from src.plugin_system.apis import llm_api, config_api, emoji_api

# 获取日志记录器
logger = get_logger('MaiZone-Utils')


class CookieManager:
    """Cookie管理类 - 负责处理QQ空间的认证Cookie"""
    
    @staticmethod
    def get_cookie_file_path(uin: str) -> str:
        """获取Cookie文件路径"""
        # .parents 向上追溯四级目录，到达 mmc 根目录
        base_path = Path(__file__).resolve().parents[5]
        return str(base_path / 'src' / 'plugins'/ 'built_in' / 'Maizone' / f"cookies-{uin}.json")

    @staticmethod
    def parse_cookie_string(cookie_str: str) -> Dict[str, str]:
        """解析Cookie字符串为字典"""
        cookies: Dict[str, str] = {}
        if not cookie_str:
            return cookies
            
        for pair in cookie_str.split("; "):
            if not pair or "=" not in pair:
                continue
            key, value = pair.split("=", 1)
            cookies[key.strip()] = value.strip()
        return cookies

    @staticmethod
    def extract_uin_from_cookie(cookie_str: str) -> str:
        """从Cookie中提取用户UIN"""
        for item in cookie_str.split("; "):
            if item.startswith("uin=") or item.startswith("o_uin="):
                _, value = item.split("=", 1)
                return value.lstrip("o")
        raise ValueError("无法从Cookie字符串中提取UIN")

    @staticmethod
    async def fetch_cookies(domain: str, port: str, host: str) -> Dict[str, Any]:
        """从NapCat获取Cookie"""
        url = f"http://{host}:{port}/get_cookies?domain={domain}"
        try:
            async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
                
                if data.get("status") != "ok" or "cookies" not in data.get("data", {}):
                    raise RuntimeError(f"获取Cookie失败: {data}")
                    
                return data["data"]
        except Exception as e:
            logger.error(f"从NapCat获取Cookie失败: {str(e)}")
            raise

    @staticmethod
    async def renew_cookies(port: str, host: str) -> bool:
        """更新Cookie文件"""
        try:
            domain = "user.qzone.qq.com"
            cookie_data = await CookieManager.fetch_cookies(domain, port, host)
            cookie_str = cookie_data["cookies"]
            parsed_cookies = CookieManager.parse_cookie_string(cookie_str)
            uin = CookieManager.extract_uin_from_cookie(cookie_str)
            
            file_path = CookieManager.get_cookie_file_path(uin)

            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(parsed_cookies, f, indent=4, ensure_ascii=False)
                
            logger.info(f"Cookie已更新并保存至: {file_path}")
            return True
            
        except Exception as e:
            logger.error(f"更新Cookie失败: {str(e)}")
            return False

    @staticmethod
    def load_cookies(qq_account: str) -> Optional[Dict[str, str]]:
        """加载Cookie文件"""
        cookie_file = CookieManager.get_cookie_file_path(qq_account)
        
        if os.path.exists(cookie_file):
            try:
                with open(cookie_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"加载Cookie文件失败: {str(e)}")
                return None
        else:
            logger.warning(f"Cookie文件不存在: {cookie_file}")
            return None


class QZoneAPI:
    """QQ空间API类 - 封装QQ空间的核心操作"""
    
    # QQ空间API地址常量
    UPLOAD_IMAGE_URL = "https://up.qzone.qq.com/cgi-bin/upload/cgi_upload_image"
    EMOTION_PUBLISH_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6"
    DOLIKE_URL = "https://user.qzone.qq.com/proxy/domain/w.qzone.qq.com/cgi-bin/likes/internal_dolike_app"
    COMMENT_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_re_feeds"
    LIST_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6"
    ZONE_LIST_URL = "https://user.qzone.qq.com/proxy/domain/ic2.qzone.qq.com/cgi-bin/feeds/feeds3_html_more"

    def __init__(self, cookies_dict: Optional[Dict[str, str]] = None):
        """初始化QZone API"""
        self.cookies = cookies_dict or {}
        self.gtk2 = ''
        self.uin = 0
        self.qzonetoken = ''
        
        # 生成gtk2
        p_skey = self.cookies.get('p_skey') or self.cookies.get('p_skey'.upper())
        if p_skey:
            self.gtk2 = self._generate_gtk(p_skey)

        # 提取UIN
        uin_raw = self.cookies.get('uin') or self.cookies.get('o_uin') or self.cookies.get('p_uin')
        if isinstance(uin_raw, str) and uin_raw:
            uin_str = uin_raw.lstrip('o')
            try:
                self.uin = int(uin_str)
            except Exception:
                logger.error(f"UIN格式错误: {uin_raw}")

    def _generate_gtk(self, skey: str) -> str:
        """生成GTK令牌"""
        hash_val = 5381
        for i in range(len(skey)):
            hash_val += (hash_val << 5) + ord(skey[i])
        return str(hash_val & 2147483647)

    async def _do_request(
        self,
        method: str,
        url: str,
        params: Optional[Dict] = None,
        data: Optional[Dict] = None,
        headers: Optional[Dict] = None,
        timeout: int = 10
    ) -> requests.Response:
        """执行HTTP请求"""
        try:
            return requests.request(
                method=method,
                url=url,
                params=params or {},
                data=data or {},
                headers=headers or {},
                cookies=self.cookies,
                timeout=timeout
            )
        except Exception as e:
            logger.error(f"HTTP请求失败: {str(e)}")
            raise

    async def validate_token(self, retry: int = 3) -> bool:
        """验证Token有效性"""
        # 简单验证 - 检查必要的Cookie是否存在
        required_cookies = ['p_skey', 'uin']
        for cookie in required_cookies:
            if cookie not in self.cookies and cookie.upper() not in self.cookies:
                logger.error(f"缺少必要的Cookie: {cookie}")
                return False
        return True

    def _image_to_base64(self, image: bytes) -> str:
        """将图片转换为Base64"""
        pic_base64 = base64.b64encode(image)
        return str(pic_base64)[2:-1]

    async def _get_image_base64_by_url(self, url: str) -> str:
        """通过URL获取图片的Base64编码"""
        try:
            res = await self._do_request("GET", url, timeout=60)
            image_data = res.content
            base64_str = base64.b64encode(image_data).decode('utf-8')
            return base64_str
        except Exception as e:
            logger.error(f"获取图片Base64失败: {str(e)}")
            raise

    async def upload_image(self, image: bytes) -> Dict[str, Any]:
        """上传图片到QQ空间"""
        try:
            res = await self._do_request(
                method="POST",
                url=self.UPLOAD_IMAGE_URL,
                data={
                    "filename": "filename",
                    "zzpanelkey": "",
                    "uploadtype": "1",
                    "albumtype": "7",
                    "exttype": "0",
                    "skey": self.cookies["skey"],
                    "zzpaneluin": self.uin,
                    "p_uin": self.uin,
                    "uin": self.uin,
                    "p_skey": self.cookies['p_skey'],
                    "output_type": "json",
                    "qzonetoken": "",
                    "refer": "shuoshuo",
                    "charset": "utf-8",
                    "output_charset": "utf-8",
                    "upload_hd": "1",
                    "hd_width": "2048",
                    "hd_height": "10000",
                    "hd_quality": "96",
                    "backUrls": "http://upbak.photo.qzone.qq.com/cgi-bin/upload/cgi_upload_image,http://119.147.64.75/cgi-bin/upload/cgi_upload_image",
                    "url": "https://up.qzone.qq.com/cgi-bin/upload/cgi_upload_image?g_tk=" + self.gtk2,
                    "base64": "1",
                    "picfile": self._image_to_base64(image),
                },
                headers={
                    'referer': 'https://user.qzone.qq.com/' + str(self.uin),
                    'origin': 'https://user.qzone.qq.com'
                },
                timeout=60
            )
            
            if res.status_code == 200:
                # 解析返回的JSON数据
                response_text = res.text
                json_start = response_text.find('{')
                json_end = response_text.rfind('}') + 1
                json_str = response_text[json_start:json_end]
                return eval(json_str)  # 使用eval解析，因为可能不是标准JSON
            else:
                raise Exception(f"上传图片失败，状态码: {res.status_code}")
                
        except Exception as e:
            logger.error(f"上传图片异常: {str(e)}")
            raise

    def _get_picbo_and_richval(self, upload_result: Dict[str, Any]) -> tuple[str, str]:
        """从上传结果中提取picbo和richval"""
        try:
            if upload_result.get('ret') != 0:
                raise Exception("上传图片失败")
                
            picbo_spt = upload_result['data']['url'].split('&bo=')
            if len(picbo_spt) < 2:
                raise Exception("解析图片URL失败")
            picbo = picbo_spt[1]

            data = upload_result['data']
            richval = f",{data['albumid']},{data['lloc']},{data['sloc']},{data['type']},{data['height']},{data['width']},,{data['height']},{data['width']}"

            return picbo, richval
            
        except Exception as e:
            logger.error(f"提取图片信息失败: {str(e)}")
            raise

    async def publish_emotion(self, content: str, images: Optional[List[bytes]] = None) -> str:
        """发布说说"""
        if images is None:
            images = []

        try:
            post_data = {
                "syn_tweet_verson": "1",
                "paramstr": "1",
                "who": "1",
                "con": content,
                "feedversion": "1",
                "ver": "1",
                "ugc_right": "1",
                "to_sign": "0",
                "hostuin": self.uin,
                "code_version": "1",
                "format": "json",
                "qzreferrer": "https://user.qzone.qq.com/" + str(self.uin)
            }

            # 处理图片
            if len(images) > 0:
                pic_bos = []
                richvals = []
                
                for img in images:
                    upload_result = await self.upload_image(img)
                    picbo, richval = self._get_picbo_and_richval(upload_result)
                    pic_bos.append(picbo)
                    richvals.append(richval)

                post_data['pic_bo'] = ','.join(pic_bos)
                post_data['richtype'] = '1'
                post_data['richval'] = '\t'.join(richvals)

            res = await self._do_request(
                method="POST",
                url=self.EMOTION_PUBLISH_URL,
                params={'g_tk': self.gtk2, 'uin': self.uin},
                data=post_data,
                headers={
                    'referer': 'https://user.qzone.qq.com/' + str(self.uin),
                    'origin': 'https://user.qzone.qq.com'
                }
            )
            
            if res.status_code == 200:
                result = res.json()
                return result.get('tid', '')
            else:
                raise Exception(f"发表说说失败，状态码: {res.status_code}")
                
        except Exception as e:
            logger.error(f"发表说说异常: {str(e)}")
            raise

    async def like_feed(self, fid: str, target_qq: str) -> bool:
        """点赞说说"""
        try:
            post_data = {
                'qzreferrer': f'https://user.qzone.qq.com/{self.uin}',
                'opuin': self.uin,
                'unikey': f'http://user.qzone.qq.com/{target_qq}/mood/{fid}',
                'curkey': f'http://user.qzone.qq.com/{target_qq}/mood/{fid}',
                'appid': 311,
                'from': 1,
                'typeid': 0,
                'abstime': int(time.time()),
                'fid': fid,
                'active': 0,
                'format': 'json',
                'fupdate': 1,
            }
            
            res = await self._do_request(
                method="POST",
                url=self.DOLIKE_URL,
                params={'g_tk': self.gtk2},
                data=post_data,
                headers={
                    'referer': 'https://user.qzone.qq.com/' + str(self.uin),
                    'origin': 'https://user.qzone.qq.com'
                }
            )
            
            return res.status_code == 200
            
        except Exception as e:
            logger.error(f"点赞说说异常: {str(e)}")
            return False

    async def comment_feed(self, fid: str, target_qq: str, content: str) -> bool:
        """评论说说"""
        try:
            post_data = {
                "topicId": f'{target_qq}_{fid}__1',
                "uin": self.uin,
                "hostUin": target_qq,
                "feedsType": 100,
                "inCharset": "utf-8",
                "outCharset": "utf-8",
                "plat": "qzone",
                "source": "ic",
                "platformid": 52,
                "format": "fs",
                "ref": "feeds",
                "content": content,
            }
            
            res = await self._do_request(
                method="POST",
                url=self.COMMENT_URL,
                params={"g_tk": self.gtk2},
                data=post_data,
                headers={
                    'referer': 'https://user.qzone.qq.com/' + str(self.uin),
                    'origin': 'https://user.qzone.qq.com'
                }
            )
            
            return res.status_code == 200
            
        except Exception as e:
            logger.error(f"评论说说异常: {str(e)}")
            return False

    async def get_feed_list(self, target_qq: str, num: int) -> List[Dict[str, Any]]:
        """获取指定用户的说说列表"""
        try:
            logger.info(f'获取用户 {target_qq} 的说说列表')
            
            res = await self._do_request(
                method="GET",
                url=self.LIST_URL,
                params={
                    'g_tk': self.gtk2,
                    "uin": target_qq,
                    "ftype": 0,
                    "sort": 0,
                    "pos": 0,
                    "num": num,
                    "replynum": 100,
                    "callback": "_preloadCallback",
                    "code_version": 1,
                    "format": "jsonp",
                    "need_comment": 1,
                    "need_private_comment": 1
                },
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
                    "Referer": f"https://user.qzone.qq.com/{target_qq}",
                    "Host": "user.qzone.qq.com",
                    "Connection": "keep-alive"
                }
            )

            if res.status_code != 200:
                raise Exception(f"访问失败，状态码: {res.status_code}")

            # 解析JSONP响应
            data = res.text
            if data.startswith('_preloadCallback(') and data.endswith(');'):
                json_str = data[len('_preloadCallback('):-2]
            else:
                json_str = data

            json_data = json.loads(json_str)
            
            if json_data.get('code') != 0:
                return [{"error": json_data.get('message', '未知错误')}]

            # 解析说说列表
            return await self._parse_feed_list(json_data, target_qq)
            
        except Exception as e:
            logger.error(f"获取说说列表失败: {str(e)}")
            return [{"error": f'获取说说列表失败: {str(e)}'}]

    async def _parse_feed_list(self, json_data: Dict[str, Any], target_qq: str) -> List[Dict[str, Any]]:
        """解析说说列表数据"""
        try:
            feeds_list = []
            login_info = json_data.get('logininfo', {})
            uin_nickname = login_info.get('name', '')

            for msg in json_data.get("msglist", []):
                # 检查是否已经评论过
                is_commented = False
                commentlist = msg.get("commentlist", [])
                
                if isinstance(commentlist, list):
                    for comment in commentlist:
                        if comment.get("name") == uin_nickname:
                            logger.info('已评论过此说说，跳过')
                            is_commented = True
                            break

                if not is_commented:
                    # 解析说说信息
                    feed_info = await self._parse_single_feed(msg)
                    if feed_info:
                        feeds_list.append(feed_info)

            if len(feeds_list) == 0:
                return [{"error": '你已经看过所有说说了，没有必要再看一遍'}]
                
            return feeds_list
            
        except Exception as e:
            logger.error(f"解析说说列表失败: {str(e)}")
            return [{"error": f'解析说说列表失败: {str(e)}'}]

    async def _parse_single_feed(self, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """解析单条说说信息"""
        try:
            # 基本信息
            timestamp = msg.get("created_time", "")
            created_time = "unknown"
            if timestamp:
                time_tuple = time.localtime(timestamp)
                created_time = time.strftime('%Y-%m-%d %H:%M:%S', time_tuple)
                
            tid = msg.get("tid", "")
            content = msg.get("content", "")
            
            logger.debug(f"正在解析说说: {content[:20]}...")

            # 解析图片
            images = []
            if 'pic' in msg:
                for pic in msg['pic']:
                    url = pic.get('url1') or pic.get('pic_id') or pic.get('smallurl')
                    if url:
                        try:
                            image_base64 = await self._get_image_base64_by_url(url)
                            image_manager = get_image_manager()
                            image_description = await image_manager.get_image_description(image_base64)
                            images.append(image_description)
                        except Exception as e:
                            logger.warning(f"处理图片失败: {str(e)}")

            # 解析视频
            videos = []
            if 'video' in msg:
                for video in msg['video']:
                    # 视频缩略图
                    video_image_url = video.get('url1') or video.get('pic_url')
                    if video_image_url:
                        try:
                            image_base64 = await self._get_image_base64_by_url(video_image_url)
                            image_manager = get_image_manager()
                            image_description = await image_manager.get_image_description(image_base64)
                            images.append(f"视频缩略图: {image_description}")
                        except Exception as e:
                            logger.warning(f"处理视频缩略图失败: {str(e)}")
                    
                    # 视频URL
                    url = video.get('url3')
                    if url:
                        videos.append(url)

            # 解析转发内容
            rt_con = ""
            if "rt_con" in msg:
                rt_con_data = msg.get("rt_con")
                if isinstance(rt_con_data, dict):
                    rt_con = rt_con_data.get("content", "")

            return {
                "tid": tid,
                "created_time": created_time,
                "content": content,
                "images": images,
                "videos": videos,
                "rt_con": rt_con
            }
            
        except Exception as e:
            logger.error(f"解析单条说说失败: {str(e)}")
            return None

    async def get_monitor_feed_list(self, num: int) -> List[Dict[str, Any]]:
        """获取监控用的说说列表（所有好友的最新动态）"""
        try:
            res = await self._do_request(
                method="GET",
                url=self.ZONE_LIST_URL,
                params={
                    "uin": self.uin,
                    "scope": 0,
                    "view": 1,
                    "filter": "all",
                    "flag": 1,
                    "applist": "all",
                    "pagenum": 1,
                    "count": num,
                    "aisortEndTime": 0,
                    "aisortOffset": 0,
                    "aisortBeginTime": 0,
                    "begintime": 0,
                    "format": "json",
                    "g_tk": self.gtk2,
                    "useutf8": 1,
                    "outputhtmlfeed": 1
                },
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
                    "Referer": f"https://user.qzone.qq.com/{self.uin}",
                    "Host": "user.qzone.qq.com",
                    "Connection": "keep-alive"
                }
            )

            if res.status_code != 200:
                raise Exception(f"访问失败，状态码: {res.status_code}")

            # 解析响应数据
            data = res.text
            if data.startswith('_Callback(') and data.endswith(');'):
                data = data[len('_Callback('):-2]
                
            data = data.replace('undefined', 'null')
            
            try:
                json_data = json5.loads(data)
                if json_data and isinstance(json_data, dict):
                    feeds_data = json_data.get('data', {}).get('data', [])
                else:
                    feeds_data = []
            except Exception as e:
                logger.error(f"解析JSON数据失败: {str(e)}")
                return []

            # 解析说说列表
            return await self._parse_monitor_feeds(feeds_data)
            
        except Exception as e:
            logger.error(f"获取监控说说列表失败: {str(e)}")
            return []

    async def _parse_monitor_feeds(self, feeds_data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """解析监控说说数据"""
        try:
            feeds_list = []
            current_uin = str(self.uin)

            for feed in feeds_data:
                if not feed:
                    continue
                    
                # 过滤广告和非说说内容
                appid = str(feed.get('appid', ''))
                if appid != '311':
                    continue
                    
                target_qq = feed.get('uin', '')
                tid = feed.get('key', '')
                
                if not target_qq or not tid:
                    continue
                    
                # 过滤自己的说说
                if target_qq == current_uin:
                    continue

                # 解析HTML内容
                html_content = feed.get('html', '')
                if not html_content:
                    continue

                feed_info = await self._parse_monitor_html(html_content, target_qq, tid)
                if feed_info:
                    feeds_list.append(feed_info)

            logger.info(f"成功解析 {len(feeds_list)} 条未读说说")
            return feeds_list
            
        except Exception as e:
            logger.error(f"解析监控说说数据失败: {str(e)}")
            return []

    async def _parse_monitor_html(self, html_content: str, target_qq: str, tid: str) -> Optional[Dict[str, Any]]:
        """解析监控说说的HTML内容"""
        try:
            soup = bs4.BeautifulSoup(html_content, 'html.parser')
            
            # 检查是否已经点赞（判断是否已读）
            like_btn = soup.find('a', class_='qz_like_btn_v3')
            if not like_btn:
                like_btn = soup.find('a', attrs={'data-islike': True})

            if isinstance(like_btn, bs4.element.Tag):
                data_islike = like_btn.get('data-islike')
                if data_islike == '1':  # 已点赞，跳过
                    return None

            # 提取文字内容
            text_div = soup.find('div', class_='f-info')
            text = text_div.get_text(strip=True) if text_div else ""

            # 提取转发内容
            rt_con = ""
            txt_box = soup.select_one('div.txt-box')
            if txt_box:
                rt_con = txt_box.get_text(strip=True)
                if '：' in rt_con:
                    rt_con = rt_con.split('：', 1)[1].strip()

            # 提取图片
            images = []
            img_box = soup.find('div', class_='img-box')
            if isinstance(img_box, bs4.element.Tag):
                for img in img_box.find_all('img'):
                    src = img.get('src') if isinstance(img, bs4.element.Tag) else None
                    if src and isinstance(src, str) and not src.startswith('http://qzonestyle.gtimg.cn'):
                        try:
                            image_base64 = await self._get_image_base64_by_url(src)
                            image_manager = get_image_manager()
                            description = await image_manager.get_image_description(image_base64)
                            images.append(description)
                        except Exception as e:
                            logger.warning(f"处理图片失败: {str(e)}")

            # 视频缩略图
            img_tag = soup.select_one('div.video-img img')
            if isinstance(img_tag, bs4.element.Tag):
                src = img_tag.get('src')
                if src and isinstance(src, str):
                    try:
                        image_base64 = await self._get_image_base64_by_url(src)
                        image_manager = get_image_manager()
                        description = await image_manager.get_image_description(image_base64)
                        images.append(f"视频缩略图: {description}")
                    except Exception as e:
                        logger.warning(f"处理视频缩略图失败: {str(e)}")

            # 视频URL
            videos = []
            video_div = soup.select_one('div.img-box.f-video-wrap.play')
            if video_div and 'url3' in video_div.attrs:
                videos.append(video_div['url3'])

            return {
                'target_qq': target_qq,
                'tid': tid,
                'content': text,
                'images': images,
                'videos': videos,
                'rt_con': rt_con,
            }
            
        except Exception as e:
            logger.error(f"解析监控HTML失败: {str(e)}")
            return None


class QZoneManager:
    """QQ空间管理器 - 高级封装类"""
    
    def __init__(self, port: str, host: str):
        """初始化QZone管理器"""
        self.port = port
        self.host = host
        self.cookie_manager = CookieManager()

    async def _get_qzone_api(self, qq_account: str) -> Optional[QZoneAPI]:
        """获取QZone API实例"""
        try:
            # 更新Cookie
            await self.cookie_manager.renew_cookies(self.port, self.host)
            
            # 加载Cookie
            cookies = self.cookie_manager.load_cookies(qq_account)
            if not cookies:
                logger.error("无法加载Cookie")
                return None

            # 创建API实例
            qzone_api = QZoneAPI(cookies)
            
            # 验证Token
            if not await qzone_api.validate_token():
                logger.error("Token验证失败")
                return None
                
            return qzone_api
            
        except Exception as e:
            logger.error(f"获取QZone API失败: {str(e)}")
            return None

    async def send_feed(self, message: str, image_directory: str, qq_account: str, enable_image: bool) -> bool:
        """发送说说"""
        try:
            # 获取API实例
            qzone_api = await self._get_qzone_api(qq_account)
            if not qzone_api:
                return False

            # 处理图片
            images = []
            if enable_image:
                images = await self._load_images(image_directory, message)

            # 发送说说
            tid = await qzone_api.publish_emotion(message, images)
            if tid:
                logger.info(f"成功发送说说，TID: {tid}")
                return True
            else:
                logger.error("发送说说失败")
                return False
                
        except Exception as e:
            logger.error(f"发送说说异常: {str(e)}")
            return False

    async def _load_images(self, image_directory: str, message: str) -> List[bytes]:
        """加载图片文件"""
        images = []
        
        try:
            if os.path.exists(image_directory):
                # 获取所有未处理的图片文件
                all_files = [f for f in os.listdir(image_directory)
                           if os.path.isfile(os.path.join(image_directory, f))]
                unprocessed_files = [f for f in all_files if not f.startswith("done_")]
                unprocessed_files_sorted = sorted(unprocessed_files)

                for image_file in unprocessed_files_sorted:
                    full_path = os.path.join(image_directory, image_file)
                    try:
                        with open(full_path, "rb") as img:
                            images.append(img.read())

                        # 重命名已处理的文件
                        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                        new_filename = f"done_{timestamp}_{image_file}"
                        new_path = os.path.join(image_directory, new_filename)
                        os.rename(full_path, new_path)
                        
                    except Exception as e:
                        logger.warning(f"处理图片文件 {image_file} 失败: {str(e)}")

            # 如果没有图片文件，尝试获取表情包
            if not images:
                image = await emoji_api.get_by_description(message)
                if image:
                    image_base64, description, scene = image
                    image_data = base64.b64decode(image_base64)
                    images.append(image_data)
                    
        except Exception as e:
            logger.error(f"加载图片失败: {str(e)}")
            
        return images

    async def read_feed(self, qq_account: str, target_qq: str, num: int) -> List[Dict[str, Any]]:
        """读取指定用户的说说"""
        try:
            # 获取API实例
            qzone_api = await self._get_qzone_api(qq_account)
            if not qzone_api:
                return [{"error": "无法获取QZone API"}]

            # 获取说说列表
            feeds_list = await qzone_api.get_feed_list(target_qq, num)
            return feeds_list
            
        except Exception as e:
            logger.error(f"读取说说失败: {str(e)}")
            return [{"error": f"读取说说失败: {str(e)}"}]

    async def monitor_read_feed(self, qq_account: str, num: int) -> List[Dict[str, Any]]:
        """监控读取所有好友的说说"""
        try:
            # 获取API实例
            qzone_api = await self._get_qzone_api(qq_account)
            if not qzone_api:
                return []

            # 获取监控说说列表
            feeds_list = await qzone_api.get_monitor_feed_list(num)
            return feeds_list
            
        except Exception as e:
            logger.error(f"监控读取说说失败: {str(e)}")
            return []

    async def like_feed(self, qq_account: str, target_qq: str, fid: str) -> bool:
        """点赞说说"""
        try:
            # 获取API实例
            qzone_api = await self._get_qzone_api(qq_account)
            if not qzone_api:
                return False

            # 点赞说说
            success = await qzone_api.like_feed(fid, target_qq)
            return success
            
        except Exception as e:
            logger.error(f"点赞说说失败: {str(e)}")
            return False

    async def comment_feed(self, qq_account: str, target_qq: str, fid: str, content: str) -> bool:
        """评论说说"""
        try:
            # 获取API实例
            qzone_api = await self._get_qzone_api(qq_account)
            if not qzone_api:
                return False

            # 评论说说
            success = await qzone_api.comment_feed(fid, target_qq, content)
            return success
            
        except Exception as e:
            logger.error(f"评论说说失败: {str(e)}")
            return False


# ===== 辅助功能函数 =====

async def generate_image_by_sf(api_key: str, story: str, image_dir: str, batch_size: int = 1) -> bool:
    """使用硅基流动API生成图片"""
    try:
        logger.info(f"正在生成图片，保存路径: {image_dir}")
        
        # 获取模型配置
        models = llm_api.get_available_models()
        prompt_model = "replyer_1"
        model_config = models.get(prompt_model)
        
        if not model_config:
            logger.error('配置模型失败')
            return False

        # 生成图片提示词
        bot_personality = config_api.get_global_config("personality.personality_core", "一个机器人")
        bot_details = config_api.get_global_config("identity.identity_detail", "未知")
        
        success, prompt, reasoning, model_name = await llm_api.generate_with_model(
            prompt=f"""
            请根据以下QQ空间说说内容配图，并构建生成配图的风格和prompt。
            说说主人信息：'{bot_personality},{str(bot_details)}'。
            说说内容:'{story}'。 
            请注意：仅回复用于生成图片的prompt，不要有其他的任何正文以外的冗余输出""",
            model_config=model_config,
            request_type="story.generate",
            temperature=0.3,
            max_tokens=1000
        )
        
        if not success:
            logger.error('生成说说配图prompt失败')
            return False
            
        logger.info(f'即将生成说说配图：{prompt}')

        # 调用硅基流动API
        sf_url = "https://api.siliconflow.cn/v1/images/generations"
        sf_headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        sf_data = {
            "model": "Kwai-Kolors/Kolors",
            "prompt": prompt,
            "negative_prompt": "lowres, bad anatomy, bad hands, text, error, cropped, worst quality, low quality, normal quality, jpeg artifacts, signature, watermark, username, blurry",
            "image_size": "1024x1024",
            "batch_size": batch_size,
            "seed": random.randint(1, 9999999999),
            "num_inference_steps": 20,
            "guidance_scale": 7.5,
        }
        
        res = requests.post(sf_url, headers=sf_headers, json=sf_data)
        
        if res.status_code != 200:
            logger.error(f'生成图片出错，错误码: {res.status_code}')
            return False
            
        json_data = res.json()
        image_urls = [img["url"] for img in json_data["images"]]
        
        # 确保目录存在
        Path(image_dir).mkdir(parents=True, exist_ok=True)
        
        # 下载并保存图片
        for i, img_url in enumerate(image_urls):
            try:
                img_response = requests.get(img_url)
                filename = f"sf_{i}_{int(time.time())}.png"
                save_path = Path(image_dir) / filename
                
                with open(save_path, "wb") as f:
                    f.write(img_response.content)
                    
                logger.info(f"图片已保存至: {save_path}")
                
            except Exception as e:
                logger.error(f"下载图片失败: {str(e)}")
                return False
                
        return True
        
    except Exception as e:
        logger.error(f"生成图片失败: {str(e)}")
        return False


async def get_send_history(qq_account: str) -> str:
    """获取发送历史记录"""
    try:
        cookie_manager = CookieManager()
        cookies = cookie_manager.load_cookies(qq_account)
        
        if not cookies:
            return ""

        qzone_api = QZoneAPI(cookies)
        
        if not await qzone_api.validate_token():
            logger.error("Token验证失败")
            return ""
            
        feeds_list = await qzone_api.get_feed_list(target_qq=qq_account, num=5)
        
        if not isinstance(feeds_list, list) or len(feeds_list) == 0:
            return ""

        history_lines = ["==================="]
        
        for feed in feeds_list:
            if not isinstance(feed, dict):
                continue
                
            created_time = feed.get("created_time", "")
            content = feed.get("content", "")
            images = feed.get("images", [])
            rt_con = feed.get("rt_con", "")
            
            if not rt_con:
                history_lines.append(
                    f"\n时间：'{created_time}'\n说说内容：'{content}'\n图片：'{images}'\n==================="
                )
            else:
                history_lines.append(
                    f"\n时间: '{created_time}'\n转发了一条说说，内容为: '{rt_con}'\n图片: '{images}'\n对该说说的评论为: '{content}'\n==================="
                )
                
        return "".join(history_lines)
        
    except Exception as e:
        logger.error(f"获取发送历史失败: {str(e)}")
        return ""