import asyncio
import base64
import json

import aiohttp
import platform

from datetime import datetime, timezone
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from src.common.logger import get_logger
from src.common.tcp_connector import get_tcp_connector
from src.config.config import global_config
from src.manager.async_task_manager import AsyncTask
from src.manager.local_store_manager import local_storage

logger = get_logger("remote")

TELEMETRY_SERVER_URL = "http://124.248.67.228:10058"
"""遥测服务地址"""


class TelemetryHeartBeatTask(AsyncTask):
    HEARTBEAT_INTERVAL = 300

    def __init__(self):
        super().__init__(task_name="Telemetry Heart Beat Task", run_interval=self.HEARTBEAT_INTERVAL)
        self.server_url = TELEMETRY_SERVER_URL
        """遥测服务地址"""

        self.client_uuid: str | None = local_storage["mofox_uuid"] if "mofox_uuid" in local_storage else None  # type: ignore
        """客户端UUID"""

        self.private_key_pem: str | None = local_storage["mofox_private_key"] if "mofox_private_key" in local_storage else None  # type: ignore
        """客户端私钥"""

        self.info_dict = self._get_sys_info()
        """系统信息字典"""

    @staticmethod
    def _get_sys_info() -> dict[str, str]:
        """获取系统信息"""
        info_dict = {
            "os_type": "Unknown",
            "py_version": platform.python_version(),
            "mofox_version": global_config.MMC_VERSION,
        }

        match platform.system():
            case "Windows":
                info_dict["os_type"] = "Windows"
            case "Linux":
                info_dict["os_type"] = "Linux"
            case "Darwin":
                info_dict["os_type"] = "macOS"
            case _:
                info_dict["os_type"] = "Unknown"

        return info_dict

    def _generate_signature(self, request_body: dict) -> tuple[str, str]:
        """
        生成RSA签名
        
        Returns:
            tuple[str, str]: (timestamp, signature_b64)
        """
        if not self.private_key_pem:
            raise ValueError("私钥未初始化")
        
        # 生成时间戳
        timestamp = datetime.now(timezone.utc).isoformat()
        
        # 创建签名数据字符串
        sign_data = f"{self.client_uuid}:{timestamp}:{json.dumps(request_body, separators=(',', ':'))}"
        
        # 加载私钥
        private_key = serialization.load_pem_private_key(
            self.private_key_pem.encode('utf-8'),
            password=None
        )
        
        # 确保是RSA私钥
        if not isinstance(private_key, rsa.RSAPrivateKey):
            raise ValueError("私钥必须是RSA格式")
        
        # 生成签名
        signature = private_key.sign(
            sign_data.encode('utf-8'),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH
            ),
            hashes.SHA256()
        )
        
        # Base64编码
        signature_b64 = base64.b64encode(signature).decode('utf-8')
        
        return timestamp, signature_b64

    def _decrypt_challenge(self, challenge_b64: str) -> str:
        """
        解密挑战数据
        
        Args:
            challenge_b64: Base64编码的挑战数据
            
        Returns:
            str: 解密后的UUID字符串
        """
        if not self.private_key_pem:
            raise ValueError("私钥未初始化")
        
        # 加载私钥
        private_key = serialization.load_pem_private_key(
            self.private_key_pem.encode('utf-8'),
            password=None
        )
        
        # 确保是RSA私钥
        if not isinstance(private_key, rsa.RSAPrivateKey):
            raise ValueError("私钥必须是RSA格式")
        
        # 解密挑战数据
        decrypted_bytes = private_key.decrypt(
            base64.b64decode(challenge_b64),
            padding.OAEP(
                mgf=padding.MGF1(hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None
            )
        )
        
        return decrypted_bytes.decode('utf-8')

    async def _req_uuid(self) -> bool:
        """
        向服务端请求UUID和私钥（两步注册流程）
        """
        try_count: int = 0
        while True:
            logger.info("正在向遥测服务端注册客户端...")

            try:
                async with aiohttp.ClientSession(connector=await get_tcp_connector()) as session:
                    # Step 1: 获取临时UUID、私钥和挑战数据
                    logger.debug("开始注册步骤1：获取临时UUID和私钥")
                    async with session.post(
                        f"{TELEMETRY_SERVER_URL}/stat/reg_client_step1",
                        json={},
                        timeout=aiohttp.ClientTimeout(total=5),
                    ) as response:
                        logger.debug(f"Step1 Response status: {response.status}")

                        if response.status != 200:
                            response_text = await response.text()
                            logger.error(
                                f"注册步骤1失败，状态码: {response.status}, 响应内容: {response_text}"
                            )
                            raise aiohttp.ClientResponseError(
                                request_info=response.request_info,
                                history=response.history,
                                status=response.status,
                                message=f"Step1 failed: {response_text}"
                            )

                        step1_data = await response.json()
                        temp_uuid = step1_data.get("temp_uuid")
                        private_key = step1_data.get("private_key")
                        challenge = step1_data.get("challenge")
                        
                        if not all([temp_uuid, private_key, challenge]):
                            logger.error("Step1响应缺少必要字段：temp_uuid, private_key 或 challenge")
                            raise ValueError("Step1响应数据不完整")

                        # 临时保存私钥用于解密
                        self.private_key_pem = private_key
                        
                        # 解密挑战数据
                        logger.debug("解密挑战数据...")
                        try:
                            decrypted_uuid = self._decrypt_challenge(challenge)
                        except Exception as e:
                            logger.error(f"解密挑战数据失败: {e}")
                            raise
                        
                        # 验证解密结果
                        if decrypted_uuid != temp_uuid:
                            logger.error(f"解密结果验证失败: 期望 {temp_uuid}, 实际 {decrypted_uuid}")
                            raise ValueError("解密结果与临时UUID不匹配")
                        
                        logger.debug("挑战数据解密成功，开始注册步骤2")

                    # Step 2: 发送解密结果完成注册
                    async with session.post(
                        f"{TELEMETRY_SERVER_URL}/stat/reg_client_step2",
                        json={
                            "temp_uuid": temp_uuid,
                            "decrypted_uuid": decrypted_uuid
                        },
                        timeout=aiohttp.ClientTimeout(total=5),
                    ) as response:
                        logger.debug(f"Step2 Response status: {response.status}")

                        if response.status == 200:
                            step2_data = await response.json()
                            mofox_uuid = step2_data.get("mofox_uuid")
                            
                            if mofox_uuid:
                                # 将正式UUID和私钥存储到本地
                                local_storage["mofox_uuid"] = mofox_uuid
                                local_storage["mofox_private_key"] = private_key
                                self.client_uuid = mofox_uuid
                                self.private_key_pem = private_key
                                logger.info(f"成功注册客户端，UUID: {self.client_uuid}")
                                return True
                            else:
                                logger.error("Step2响应缺少mofox_uuid字段")
                                raise ValueError("Step2响应数据不完整")
                        elif response.status in [400, 401]:
                            # 临时数据无效，需要重新开始
                            response_text = await response.text()
                            logger.warning(f"Step2失败，临时数据无效: {response.status}, {response_text}")
                            raise ValueError(f"Step2失败: {response_text}")
                        else:
                            response_text = await response.text()
                            logger.error(
                                f"注册步骤2失败，状态码: {response.status}, 响应内容: {response_text}"
                            )
                            raise aiohttp.ClientResponseError(
                                request_info=response.request_info,
                                history=response.history,
                                status=response.status,
                                message=f"Step2 failed: {response_text}"
                            )

            except Exception as e:
                import traceback

                error_msg = str(e) or "未知错误"
                logger.warning(
                    f"注册客户端出错，不过你还是可以正常使用墨狐: {type(e).__name__}: {error_msg}"
                )
                logger.debug(f"完整错误信息: {traceback.format_exc()}")

            # 请求失败，重试次数+1
            try_count += 1
            if try_count > 3:
                # 如果超过3次仍然失败，则退出
                logger.error("注册客户端失败，请检查网络连接或服务端状态")
                return False
            else:
                # 如果可以重试，等待后继续（指数退避）
                logger.info(f"注册客户端失败，将于 {4**try_count} 秒后重试...")
                await asyncio.sleep(4**try_count)

    async def _send_heartbeat(self):
        """向服务器发送心跳"""
        if not self.client_uuid or not self.private_key_pem:
            logger.error("UUID或私钥未初始化，无法发送心跳")
            return

        try:
            # 生成签名
            timestamp, signature = self._generate_signature(self.info_dict)
            
            headers = {
                "X-mofox-UUID": self.client_uuid,
                "X-mofox-Signature": signature,
                "X-mofox-Timestamp": timestamp,
                "User-Agent": f"MofoxClient/{self.client_uuid[:8]}",
                "Content-Type": "application/json"
            }

            logger.debug(f"正在发送心跳到服务器: {self.server_url}")
            logger.debug(f"Headers: {headers}")

            async with aiohttp.ClientSession(connector=await get_tcp_connector()) as session:
                async with session.post(
                    f"{self.server_url}/stat/client_heartbeat",
                    headers=headers,
                    json=self.info_dict,
                    timeout=aiohttp.ClientTimeout(total=5),  # 设置超时时间为5秒
                ) as response:
                    logger.debug(f"Response status: {response.status}")

                    # 处理响应
                    if 200 <= response.status < 300:
                        # 成功
                        logger.debug(f"心跳发送成功，状态码: {response.status}")
                    elif response.status == 401:
                        # 401 Unauthorized - 签名验证失败
                        logger.warning(
                            "（此消息不会影响正常使用）心跳发送失败，401 Unauthorized: 签名验证失败。"
                            "处理措施：重置客户端信息，下次发送心跳时将尝试重新注册。"
                        )
                        self.client_uuid = None
                        self.private_key_pem = None
                        if "mofox_uuid" in local_storage:
                            del local_storage["mofox_uuid"]
                        if "mofox_private_key" in local_storage:
                            del local_storage["mofox_private_key"]
                    elif response.status == 404:
                        # 404 Not Found - 客户端未注册
                        logger.warning(
                            "（此消息不会影响正常使用）心跳发送失败，404 Not Found: 客户端未注册。"
                            "处理措施：重置客户端信息，下次发送心跳时将尝试重新注册。"
                        )
                        self.client_uuid = None
                        self.private_key_pem = None
                        if "mofox_uuid" in local_storage:
                            del local_storage["mofox_uuid"]
                        if "mofox_private_key" in local_storage:
                            del local_storage["mofox_private_key"]
                    elif response.status == 403:
                        # 403 Forbidden - UUID无效或未注册
                        response_text = await response.text()
                        logger.warning(
                            f"（此消息不会影响正常使用）心跳发送失败，403 Forbidden: UUID无效或未注册。"
                            f"响应内容: {response_text}。"
                            "处理措施：重置客户端信息，下次发送心跳时将尝试重新注册。"
                        )
                        self.client_uuid = None
                        self.private_key_pem = None
                        if "mofox_uuid" in local_storage:
                            del local_storage["mofox_uuid"]
                        if "mofox_private_key" in local_storage:
                            del local_storage["mofox_private_key"]
                    else:
                        # 其他错误
                        response_text = await response.text()
                        logger.warning(
                            f"（此消息不会影响正常使用）心跳发送失败，状态码: {response.status}, 响应内容: {response_text}"
                        )
        except Exception as e:
            import traceback

            error_msg = str(e) or "未知错误"
            logger.warning(f"（此消息不会影响正常使用）心跳发送出错: {type(e).__name__}: {error_msg}")
            logger.debug(f"完整错误信息: {traceback.format_exc()}")

    async def run(self):
        # 检查是否已注册
        if not self.client_uuid or not self.private_key_pem:
            if not await self._req_uuid():
                logger.warning("客户端注册失败，跳过此次心跳")
                return

        await self._send_heartbeat()