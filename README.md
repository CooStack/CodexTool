# CodexTools MCP

CodexTools 是一个本地运行的 MCP 服务，重点解决三类问题：

- 让 AI 稳定地读写项目文件、搜索代码、生成补丁、执行必要命令
- 提供桌面 GUI、浏览器接管、OCR、截图、人工确认等高级交互能力
- 支持多 Agent 协作，包括规划、分工、审查和可视化面板

如果你只关心“它能做什么、怎么装、为什么值得用”，看这份 README 就够了。

## 它能做什么

CodexTools 提供一组面向真实开发场景的 MCP 工具：

- 文件与代码操作
  - 读取、搜索、批量读取、替换、补丁修改、目录管理
- 本地执行
  - 运行命令、批量执行、调试、基准测试
- 图形与交互
  - 弹窗、进度条、图表、提示音
- 浏览器能力
  - 可见浏览器控制、页面读取、输入、点击、搜索、附着到现有浏览器
- 桌面能力
  - OCR、截图、坐标点击、人工接管提示、桌面级 computer-use
- 多 Agent 协作
  - 角色分工、任务编排、聊天式活动流、Dashboard GUI、草稿与交接文档
- 自带项目技能
  - `codextoolSkill`：角色化多 Agent 工作流
  - `minecraft-modding-skill`：Minecraft Mod 开发专项工作流

## 它的优势

- 本地优先
  - 服务直接运行在你的机器上，适合真实项目目录、真实浏览器、真实 GUI 操作。
- 工具面完整
  - 不只是读文件和跑命令，还覆盖了 GUI、浏览器、OCR、多 Agent 这些高价值能力。
- 对工程任务更友好
  - 更强调补丁式改动、结构化输出、面板可视化和持久化协作产物。
- 可直接扩展
  - 仓库内已经带了 skills，安装后可以直接复用，不需要再单独找模板。
- 一键安装
  - 现在可以直接运行 `install.bat` 或 `install.py`，自动装依赖、写配置、复制所选 skills。

## 安装

### 环境要求

- Windows
- Python 3.10+

### 方案 1：安装 MCP + Skills

安装器会安装 `CodexTools MCP`，并让你选择这两个 skill：

- `codextoolSkill`
- `minecraft-modding-skill`

运行：

```bat
install.bat
```

或：

```powershell
python install.py
```

### 方案 2：只安装 MCP

运行：

```powershell
python install.py --skills none
```

### MCP 接入方式

安装完成后，重启你的 Codex 或支持 MCP 的客户端，确认已经启用：

- MCP Server 名称：`CodexTools`

项目内会生成配置样例：

- [mcp.codextools.json](/D:/python/CodexTools/mcp.codextools.json)
- [codex.config.codextools.toml](/D:/python/CodexTools/codex.config.codextools.toml)

服务启动入口：

- [server.py](/D:/python/CodexTools/server.py)

## 推荐使用方式

如果你要体验多 Agent 面板或聊天式协作流，优先安装：

- `codextoolSkill`

如果你主要做 Minecraft Mod、Forge、Fabric、NeoForge 或相关 JVM 项目，建议同时安装：

- `minecraft-modding-skill`

## 仓库内重要文件

- 服务入口: [server.py](/D:/python/CodexTools/server.py)
- 一键安装脚本: [install.py](/D:/python/CodexTools/install.py)
- Windows 启动脚本: [install.bat](/D:/python/CodexTools/install.bat)
- 多 Agent skill: [SKILL.md](/D:/python/CodexTools/.codex/skills/codextoolSkill/SKILL.md)
- Minecraft skill: [SKILL.md](/D:/python/CodexTools/.codex/skills/minecraft-modding-skill/SKILL.md)

## 常见问题

### 1. 安装器会不会覆盖我现有的 Codex 配置？

不会整份覆盖。安装器只会维护一段带标记的 `CodexTools` 配置块。

### 2. skills 会装到哪里？

会复制到这两个目录，便于不同代理环境直接发现：

- `~/.codex/skills`
- `~/.agents/skills`

### 3. Playwright 浏览器安装失败怎么办？

先完成主安装，再手动执行：

```powershell
D:/python/CodexTools/.venv/Scripts/python.exe -m playwright install chromium
```

### 4. 如何确认安装成功？

看这三项：

1. `.venv` 已创建
2. `~/.codex/config.toml` 中存在 `CodexTools` 配置
3. 客户端重启后能看到 `CodexTools` MCP 服务
