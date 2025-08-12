"""
MaiZone插件独立配置文件加载系统

这个模块提供了一个独立的配置文件加载系统，用于替代原本插件中的config加载系统。
它支持TOML格式的配置文件，具有配置验证、默认值处理、类型转换等功能。
"""

import os
import toml
import shutil
import datetime
from typing import Dict, Any, Union, List, Optional, Type
from dataclasses import dataclass, field
from pathlib import Path

from src.common.logger import get_logger

logger = get_logger("MaiZone.ConfigLoader")


@dataclass
class ConfigFieldSpec:
    """配置字段规格定义"""
    name: str
    type_hint: Type
    default: Any
    description: str = ""
    required: bool = False
    choices: Optional[List[Any]] = None
    min_value: Optional[Union[int, float]] = None
    max_value: Optional[Union[int, float]] = None
    
    def validate_value(self, value: Any) -> tuple[bool, str]:
        """验证配置值是否符合规格"""
        # 类型检查
        if not isinstance(value, self.type_hint):
            try:
                # 尝试类型转换
                if self.type_hint == bool and isinstance(value, str):
                    value = value.lower() in ('true', '1', 'yes', 'on')
                else:
                    value = self.type_hint(value)
            except (ValueError, TypeError):
                return False, f"类型错误: 期望 {self.type_hint.__name__}, 得到 {type(value).__name__}"
        
        # 选择项检查
        if self.choices and value not in self.choices:
            return False, f"值不在允许范围内: {self.choices}"
        
        # 数值范围检查
        if isinstance(value, (int, float)):
            if self.min_value is not None and value < self.min_value:
                return False, f"值小于最小值 {self.min_value}"
            if self.max_value is not None and value > self.max_value:
                return False, f"值大于最大值 {self.max_value}"
        
        return True, ""


@dataclass 
class ConfigSectionSpec:
    """配置节规格定义"""
    name: str
    description: str = ""
    fields: Dict[str, ConfigFieldSpec] = field(default_factory=dict)
    
    def add_field(self, field_spec: ConfigFieldSpec):
        """添加字段规格"""
        self.fields[field_spec.name] = field_spec
    
    def validate_section(self, section_data: Dict[str, Any]) -> tuple[bool, List[str]]:
        """验证配置节数据"""
        errors = []
        
        # 检查必需字段
        for field_name, field_spec in self.fields.items():
            if field_spec.required and field_name not in section_data:
                errors.append(f"缺少必需字段: {field_name}")
        
        # 验证每个字段
        for field_name, value in section_data.items():
            if field_name in self.fields:
                field_spec = self.fields[field_name]
                is_valid, error_msg = field_spec.validate_value(value)
                if not is_valid:
                    errors.append(f"{field_name}: {error_msg}")
            else:
                logger.warning(f"未知配置字段: {self.name}.{field_name}")
        
        return len(errors) == 0, errors


class MaiZoneConfigLoader:
    """MaiZone插件独立配置加载器"""
    
    def __init__(self, plugin_dir: str, config_filename: str = "config.toml"):
        """
        初始化配置加载器
        
        Args:
            plugin_dir: 插件目录路径
            config_filename: 配置文件名
        """
        self.plugin_dir = Path(plugin_dir)
        self.config_filename = config_filename
        self.config_file_path = self.plugin_dir / config_filename
        self.config_data: Dict[str, Any] = {}
        self.config_specs: Dict[str, ConfigSectionSpec] = {}
        self.config_version = "2.0.0"
        
        # 初始化配置规格
        self._init_config_specs()
    
    def _init_config_specs(self):
        """初始化配置规格定义"""
        # 插件基础配置节
        plugin_section = ConfigSectionSpec("plugin", "插件基础配置")
        plugin_section.add_field(ConfigFieldSpec("enable", bool, True, "是否启用插件"))
        plugin_section.add_field(ConfigFieldSpec("config_version", str, "2.0.0", "配置文件版本"))
        plugin_section.add_field(ConfigFieldSpec("http_port", str, "3000", "NapCat HTTP服务器端口号"))
        plugin_section.add_field(ConfigFieldSpec("http_host", str, "127.0.0.1", "NapCat HTTP服务器地址"))
        self.config_specs["plugin"] = plugin_section
        
        # 模型相关配置节
        models_section = ConfigSectionSpec("models", "模型相关配置")
        models_section.add_field(ConfigFieldSpec("text_model", str, "replyer_1", "生成文本的模型名称"))
        models_section.add_field(ConfigFieldSpec("siliconflow_apikey", str, "", "硅基流动AI生图API密钥"))
        self.config_specs["models"] = models_section
        
        # 发送说说配置节
        send_section = ConfigSectionSpec("send", "发送说说配置")
        send_section.add_field(ConfigFieldSpec("permission", list, ["2488036428"], "发送权限QQ号列表"))
        send_section.add_field(ConfigFieldSpec("permission_type", str, "whitelist", "权限类型", 
                                              choices=["whitelist", "blacklist"]))
        send_section.add_field(ConfigFieldSpec("enable_image", bool, False, "是否启用说说配图"))
        send_section.add_field(ConfigFieldSpec("enable_ai_image", bool, False, "是否启用AI生成配图"))
        send_section.add_field(ConfigFieldSpec("enable_reply", bool, True, "生成完成时是否发出回复"))
        send_section.add_field(ConfigFieldSpec("ai_image_number", int, 1, "AI生成图片数量", 
                                              min_value=1, max_value=4))
        send_section.add_field(ConfigFieldSpec("image_directory", str, "./plugins/built_in/Maizone/images", 
                                              "图片存储目录"))
        self.config_specs["send"] = send_section
        
        # 阅读说说配置节
        read_section = ConfigSectionSpec("read", "阅读说说配置")
        read_section.add_field(ConfigFieldSpec("permission", list, [], "阅读权限QQ号列表"))
        read_section.add_field(ConfigFieldSpec("permission_type", str, "blacklist", "权限类型",
                                              choices=["whitelist", "blacklist"]))
        read_section.add_field(ConfigFieldSpec("read_number", int, 5, "一次读取的说说数量", 
                                              min_value=1, max_value=20))
        read_section.add_field(ConfigFieldSpec("like_possibility", float, 1.0, "点赞概率", 
                                              min_value=0.0, max_value=1.0))
        read_section.add_field(ConfigFieldSpec("comment_possibility", float, 0.3, "评论概率", 
                                              min_value=0.0, max_value=1.0))
        self.config_specs["read"] = read_section
        
        # 自动监控配置节
        monitor_section = ConfigSectionSpec("monitor", "自动监控配置")
        monitor_section.add_field(ConfigFieldSpec("enable_auto_monitor", bool, False, "是否启用自动监控好友说说"))
        monitor_section.add_field(ConfigFieldSpec("interval_minutes", int, 10, "监控间隔时间(分钟)", 
                                                 min_value=1, max_value=1440))
        self.config_specs["monitor"] = monitor_section
        
        # 定时发送配置节
        schedule_section = ConfigSectionSpec("schedule", "定时发送配置")
        schedule_section.add_field(ConfigFieldSpec("enable_schedule", bool, False, "是否启用定时发送说说"))
        schedule_section.add_field(ConfigFieldSpec("schedules", dict,
            {"08:00": "早安", "22:00": "晚安"},
            "定时发送任务列表, 格式为 {\"时间\": \"主题\"}"))
        self.config_specs["schedule"] = schedule_section
    
    def load_config(self) -> bool:
        """
        加载配置文件
        
        Returns:
            bool: 是否成功加载
        """
        try:
            # 如果配置文件不存在，生成默认配置
            if not self.config_file_path.exists():
                logger.info(f"配置文件不存在，生成默认配置: {self.config_file_path}")
                self._generate_default_config()
            
            # 加载配置文件
            with open(self.config_file_path, 'r', encoding='utf-8') as f:
                self.config_data = toml.load(f)
            
            logger.info(f"成功加载配置文件: {self.config_file_path}")
            
            # 验证配置
            self._validate_config()
            
            # 检查版本并迁移
            self._check_and_migrate_config()
            
            return True
            
        except Exception as e:
            logger.error(f"加载配置文件失败: {e}")
            return False
    
    def _generate_default_config(self):
        """生成默认配置文件"""
        try:
            # 确保插件目录存在
            self.plugin_dir.mkdir(parents=True, exist_ok=True)
            
            # 生成默认配置数据
            default_config = {}
            for section_name, section_spec in self.config_specs.items():
                section_data = {}
                for field_name, field_spec in section_spec.fields.items():
                    section_data[field_name] = field_spec.default
                default_config[section_name] = section_data
            
            # 保存到文件
            self._save_config_to_file(default_config)
            self.config_data = default_config
            
            logger.info(f"默认配置文件已生成: {self.config_file_path}")
            
        except Exception as e:
            logger.error(f"生成默认配置文件失败: {e}")
    
    def _save_config_to_file(self, config_data: Dict[str, Any]):
        """保存配置到文件（带注释）"""
        toml_content = f"# MaiZone插件配置文件\n"
        toml_content += f"# 让你的麦麦发QQ空间说说、评论、点赞，支持AI配图、定时发送和自动监控功能\n"
        toml_content += f"# 配置版本: {self.config_version}\n\n"
        
        for section_name, section_spec in self.config_specs.items():
            if section_name not in config_data:
                continue
                
            # 添加节描述
            toml_content += f"# {section_spec.description}\n"
            toml_content += f"[{section_name}]\n\n"
            
            section_data = config_data[section_name]
            for field_name, field_spec in section_spec.fields.items():
                if field_name not in section_data:
                    continue
                    
                # 添加字段描述
                toml_content += f"# {field_spec.description}\n"
                if field_spec.choices:
                    toml_content += f"# 可选值: {', '.join(map(str, field_spec.choices))}\n"
                if field_spec.min_value is not None or field_spec.max_value is not None:
                    range_str = f"# 范围: "
                    if field_spec.min_value is not None:
                        range_str += f"最小值 {field_spec.min_value}"
                    if field_spec.max_value is not None:
                        if field_spec.min_value is not None:
                            range_str += f", 最大值 {field_spec.max_value}"
                        else:
                            range_str += f"最大值 {field_spec.max_value}"
                    toml_content += range_str + "\n"
                
                # 添加字段值
                value = section_data[field_name]
                if isinstance(value, str):
                    toml_content += f'{field_name} = "{value}"\n'
                elif isinstance(value, bool):
                    toml_content += f"{field_name} = {str(value).lower()}\n"
                elif isinstance(value, list):
                    # 格式化列表
                    if all(isinstance(item, str) for item in value):
                        formatted_list = "[" + ", ".join(f'"{item}"' for item in value) + "]"
                    elif all(isinstance(item, dict) for item in value):
                        # 处理字典列表（如schedules）
                        # 使用 TOML 内联表格式
                        formatted_items = []
                        for item in value:
                            # TOML 内联表中的字符串需要转义
                            item_str = ", ".join([f'{k} = "{str(v)}"' for k, v in item.items()])
                            formatted_items.append(f"{{ {item_str} }}")
                        formatted_list = "[\n    " + ",\n    ".join(formatted_items) + "\n]"
                    else:
                        formatted_list = str(value)
                    toml_content += f"{field_name} = {formatted_list}\n"
                else:
                    toml_content += f"{field_name} = {value}\n"
                
                toml_content += "\n"
            
            toml_content += "\n"
        
        # 写入文件
        with open(self.config_file_path, 'w', encoding='utf-8') as f:
            f.write(toml_content)
    
    def _validate_config(self) -> bool:
        """验证配置数据"""
        all_valid = True
        
        for section_name, section_spec in self.config_specs.items():
            if section_name not in self.config_data:
                logger.warning(f"配置文件缺少节: {section_name}")
                continue
            
            section_data = self.config_data[section_name]
            is_valid, errors = section_spec.validate_section(section_data)
            
            if not is_valid:
                logger.error(f"配置节 {section_name} 验证失败:")
                for error in errors:
                    logger.error(f"  - {error}")
                all_valid = False
        
        return all_valid
    
    def _check_and_migrate_config(self):
        """检查配置版本并进行迁移"""
        current_version = self.get_config("plugin.config_version", "1.0.0")
        
        if current_version != self.config_version:
            logger.info(f"检测到配置版本变更: {current_version} -> {self.config_version}")
            
            # 备份旧配置
            self._backup_config()
            
            # 迁移配置
            self._migrate_config(current_version, self.config_version)
            
            # 更新版本号
            self.config_data["plugin"]["config_version"] = self.config_version
            
            # 保存迁移后的配置
            self._save_config_to_file(self.config_data)
            
            logger.info(f"配置已迁移到版本 {self.config_version}")
    
    def _backup_config(self):
        """备份当前配置文件"""
        if self.config_file_path.exists():
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = self.config_file_path.with_suffix(f".backup_{timestamp}.toml")
            shutil.copy2(self.config_file_path, backup_path)
            logger.info(f"配置文件已备份到: {backup_path}")
    
    def _migrate_config(self, from_version: str, to_version: str):
        """迁移配置数据"""
        # 创建新的配置结构
        new_config = {}
        
        for section_name, section_spec in self.config_specs.items():
            new_section = {}
            
            # 复制现有配置值
            if section_name in self.config_data:
                old_section = self.config_data[section_name]
                for field_name, field_spec in section_spec.fields.items():
                    if field_name in old_section:
                        new_section[field_name] = old_section[field_name]
                    else:
                        new_section[field_name] = field_spec.default
                        logger.info(f"添加新配置项: {section_name}.{field_name} = {field_spec.default}")
            else:
                # 新增节，使用默认值
                for field_name, field_spec in section_spec.fields.items():
                    new_section[field_name] = field_spec.default
                logger.info(f"添加新配置节: {section_name}")
            
            new_config[section_name] = new_section
        
        self.config_data = new_config
    
    def get_config(self, key: str, default: Any = None) -> Any:
        """
        获取配置值，支持嵌套键访问
        
        Args:
            key: 配置键名，支持嵌套访问如 "section.field"
            default: 默认值
            
        Returns:
            Any: 配置值或默认值
        """
        keys = key.split('.')
        current = self.config_data
        
        for k in keys:
            if isinstance(current, dict) and k in current:
                current = current[k]
            else:
                return default
        
        return current
    
    def set_config(self, key: str, value: Any) -> bool:
        """
        设置配置值
        
        Args:
            key: 配置键名
            value: 配置值
            
        Returns:
            bool: 是否设置成功
        """
        try:
            keys = key.split('.')
            if len(keys) != 2:
                logger.error(f"配置键格式错误: {key}，应为 'section.field' 格式")
                return False
            
            section_name, field_name = keys
            
            # 检查节是否存在
            if section_name not in self.config_specs:
                logger.error(f"未知配置节: {section_name}")
                return False
            
            # 检查字段是否存在
            if field_name not in self.config_specs[section_name].fields:
                logger.error(f"未知配置字段: {key}")
                return False
            
            # 验证值
            field_spec = self.config_specs[section_name].fields[field_name]
            is_valid, error_msg = field_spec.validate_value(value)
            if not is_valid:
                logger.error(f"配置值验证失败 {key}: {error_msg}")
                return False
            
            # 设置值
            if section_name not in self.config_data:
                self.config_data[section_name] = {}
            
            self.config_data[section_name][field_name] = value
            logger.debug(f"设置配置: {key} = {value}")
            
            return True
            
        except Exception as e:
            logger.error(f"设置配置失败 {key}: {e}")
            return False
    
    def save_config(self) -> bool:
        """
        保存当前配置到文件
        
        Returns:
            bool: 是否保存成功
        """
        try:
            self._save_config_to_file(self.config_data)
            logger.info(f"配置已保存到: {self.config_file_path}")
            return True
        except Exception as e:
            logger.error(f"保存配置失败: {e}")
            return False
    
    def reload_config(self) -> bool:
        """
        重新加载配置文件
        
        Returns:
            bool: 是否重新加载成功
        """
        return self.load_config()
    
    def get_config_info(self) -> Dict[str, Any]:
        """
        获取配置信息
        
        Returns:
            Dict[str, Any]: 配置信息
        """
        return {
            "config_file": str(self.config_file_path),
            "config_version": self.config_version,
            "sections": list(self.config_specs.keys()),
            "loaded": bool(self.config_data)
        }
