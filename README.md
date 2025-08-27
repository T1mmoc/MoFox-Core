<div align="center">

# 🌟 麦麦Fork！MoFox_Bot

<p>
  <strong>🚀 基于 MaiCore 的增强版智能体，提供更完善的功能和更好的使用体验</strong>
</p>

<div align="center">

[![Python](https://img.shields.io/badge/Python-3.10+-3776ab?logo=python&logoColor=white&style=for-the-badge)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-GPLv3-d73a49?logo=gnu&logoColor=white&style=for-the-badge)](https://github.com/MoFox-Studio/MoFox_Bot/blob/master/LICENSE)
[![Contributors](https://img.shields.io/badge/Contributors-Welcome-brightgreen?logo=github&logoColor=white&style=for-the-badge)](https://github.com/MoFox-Studio/MoFox_Bot/graphs/contributors)
[![Stars](https://img.shields.io/github/stars/MoFox-Studio/MoFox_Bot?style=for-the-badge&logo=star&logoColor=white&color=yellow&label=Stars)](https://github.com/MoFox-Studio/MoFox_Bot/stargazers)
[![Release](https://img.shields.io/github/v/release/MoFox-Studio/MoFox_Bot?style=for-the-badge&logo=github&logoColor=white&color=orange)](https://github.com/MoFox-Studio/MoFox_Bot/releases)
[![QQ](https://img.shields.io/badge/QQ-Bot-blue?style=for-the-badge&logo=tencentqq&logoColor=white)](https://github.com/NapNeko/NapCatQQ)

</div>

---

## 📖 项目介绍

**MoFox_Bot** 是基于 MaiCore 的增强版智能体，在保留原版 `0.10.0 snapshot.5` 所有功能的基础上，提供了更完善的功能、更好的稳定性和更丰富的使用体验。

> [!IMPORTANT]
> **请注意！** 这个版本的所有后续更新均为我们的第三方更新，不代表 MaiBot 官方立场

> [!WARNING]
> **迁移提醒！** 从官方版本到 MoFox_Bot 版本迁移暂时存在问题，因为数据库结构有改变

---

## ✨ 功能特性

<table>
<tr>
<td width="50%">

### 🔧 原版功能（全部保留）

- 🧠 **智能对话系统** - 基于 LLM 的自然语言交互，支持 normal 和 focus 统一化处理
- 🔌 **强大插件系统** - 全面重构的插件架构，支持完整的管理 API 和权限控制
- 💭 **实时思维系统** - 模拟人类思考过程
- 📚 **表达学习功能** - 学习群友的说话风格和表达方式
- 😊 **情感表达系统** - 情绪系统和表情包系统
- 🧠 **持久记忆系统** - 基于图的长期记忆存储
- 🎭 **动态人格系统** - 自适应的性格特征和表达方式
- 📊 **数据分析** - 内置数据统计和分析功能，更好了解麦麦状态

</td>
<td width="50%">

### 🚀 拓展功能

- 🔄 **数据库切换** - 支持 SQLite 与 MySQL 自由切换，采用 SQLAlchemy 2.0 重新构建
- 🛡️ **反注入集成** - 内置一整套回复前注入过滤系统，为人格保驾护航
- 🎥 **视频分析** - 支持多种视频识别模式，拓展原版视觉
- 😴 **苏醒系统** - 能够睡觉、失眠、被吵醒，更具乐趣
- 📅 **日程系统** - 让墨狐规划每一天
- 🧠 **拓展记忆系统** - 支持瞬时记忆等多种记忆
- 🎪 **完善的 Event** - 支持动态事件注册和处理器订阅，并实现了聚合结果管理
- 🔍 **内嵌魔改插件** - 内置联网搜索等诸多功能，等你来探索
- 🌟 **还有更多** - 请参阅详细修改 [commits](https://github.com/MoFox-Studio/MoFox_Bot/commits)

</td>
</tr>
</table>

---
## 🔧 系统要求

在开始使用之前，请确保你的系统满足以下要求：

<table>
<tr>
<td width="50%">

### 💻 基础要求

| 项目 | 要求 |
|------|------|
| 🖥️ **操作系统** | Windows 10/11, macOS 10.14+, Linux (Ubuntu 18.04+) |
| 🐍 **Python 版本** | Python 3.10 或更高版本 |
| 💾 **内存** | 建议 4GB 以上可用内存 |
| 💿 **存储空间** | 至少 2GB 可用空间 |

</td>

</tr>
</table>

<table>
<tr>
<td width="50%">

### 🛠️ 依赖服务

| 服务 | 描述 |
|------|------|
| 🤖 **QQ协议端** | [NapCat](https://github.com/NapNeko/NapCatQQ) 或其他兼容协议端 |
| 🗃️ **数据库** | SQLite (内置) 或 MySQL (可选) |
| 🔧 **管理工具** | chat2db (可选) |

</td>

</tr>
</table>
---

## 🏁 快速开始

### 📦 安装部署

```bash
# 克隆项目
git clone https://github.com/MoFox-Studio/MoFox_Bot.git
cd MoFox_Bot

# 安装依赖
pip install -r requirements.txt

# 配置机器人
cp config/bot_config.toml.example config/bot_config.toml
# 编辑配置文件...

# 启动机器人
python bot.py
```

### ⚙️ 配置说明

1. 📝 **编辑配置文件** - 修改 `config/bot_config.toml` 中的基本设置
2. 🤖 **配置协议端** - 设置 NapCat 或其他兼容的 QQ 协议端
3. 🗃️ **数据库配置** - 选择 SQLite 或 MySQL 作为数据存储
4. 🔌 **插件配置** - 在 `config/plugins/` 目录下配置所需插件

---


## 🙏 致谢

我们衷心感谢以下优秀的开源项目：

<div align="center">

| 项目 | 描述 | 贡献 |
|------|------|------|
| 🎯 [MaiM-with-u](https://github.com/MaiM-with-u/MaiBot) | 原版 MaiBot 项目 | 提供优秀的基础框架 |
| 🐱 [NapCat](https://github.com/NapNeko/NapCatQQ) | 基于 NTQQ 的 Bot 协议端 | 现代化的 QQ 协议实现 |
| 🌌 [Maizone](https://github.com/internetsb/Maizone) | 魔改空间插件 | 插件部分功能借鉴 |

</div>

---

## ⚠️ 注意事项

<div align="center">

> [!CAUTION]
> **重要提醒**
> 
> 使用本项目前必须阅读和同意 [📋 用户协议](EULA.md) 和 [🔒 隐私协议](PRIVACY.md)
> 
> 本应用生成内容来自人工智能模型，由 AI 生成，请仔细甄别，请勿用于违反法律的用途
> 
> AI 生成内容不代表本项目团队的观点和立场

</div>

---

## 📄 开源协议

<div align="center">

**本项目基于 [GPL-3.0](LICENSE) 协议开源**

[![GPL-3.0](https://img.shields.io/badge/License-GPL--3.0-blue.svg?style=for-the-badge&logo=gnu)](LICENSE)

```
Copyright © 2024 MoFox Studio
Licensed under the GNU General Public License v3.0
```

</div>

---

<div align="center">

**🌟 如果这个项目对你有帮助，请给我们一个 Star！**

**💬 有问题或建议？欢迎提 Issue 或 PR！**

Made with ❤️ by [MoFox Studio](https://github.com/MoFox-Studio)

</div>
