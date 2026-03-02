# CodexTools MCP

CodexTools 是一个基于 Python 的 MCP 工具服务，提供 UTF-8 文本读写、限量读取、批量搜索、批量替换、目录操作、命令执行、图片绘制和提示音能力。

## 策略约束

- `tools/call` 放行前会先确保规则文件存在：Claude 模型使用 `CLAUDE.MD`，Codex/其他模型使用 `AGENTS.MD`。
- 规则文件默认写入当前工作区根目录（优先 `initialize.rootUri/workspaceFolders`，其次 `CODEXTOOLS_WORKSPACE_ROOT`，再兜底服务启动工作目录）。
- 若目标规则文件为空则直接创建；若已有内容则仅在缺失时追加，并避免重复追加。

## 启动

```powershell
python -u -X utf8 D:/python/CodexTools/server.py
```

依赖安装：

- 默认会在首次调用相关工具时自动尝试 `pip` 安装缺失依赖（使用当前 Python 解释器执行 `python -m pip install ...`）。
- 也可以提前手动安装，避免首次调用等待：

```powershell
python -m pip install pillow matplotlib playwright
```

- `img_draw` 依赖 `pillow`（缺失时自动安装）
- `ui_line_chart` 依赖 `matplotlib`（缺失时自动安装）
- 浏览器调试工具依赖 `playwright`（缺失时自动安装包；浏览器二进制仍需按提示执行 `playwright install`）
- `ui_dialog_input` 使用 `tkinter`（Python 标准库，需系统图形界面可用）

## 用户输入交互（ui_dialog_input）

默认是手动交互模式（不会自动提交），会尝试置顶并抢焦点，适合真实用户输入场景。

手动交互示例：

```json
{
  "title": "请输入审批意见",
  "prompt": "请填写说明后点击按钮",
  "button1_label": "提交",
  "button2_label": "取消"
}
```

自动化测试示例（仅测试时使用）：

```json
{
  "title": "自动化测试",
  "prompt": "该窗口将自动提交",
  "default": "test",
  "auto_submit_after_ms": 200,
  "auto_button": "button1"
}
```

## 计划确认弹窗（继续/修改）

当计划未确认时，写操作闸门会优先弹出计划确认窗口（`ui_plan_confirm`）：
- 点击“继续”：立即确认当前计划并继续执行。
- 点击“修改计划”：阻断写操作，按提示直接编辑当前工作区根目录下的 `<工作区名>-plan.md`，修改后再发送“继续”并调用 `plan_confirm_continue`。

该弹窗默认手动交互，不自动提交。

## MCP 配置（JSON）

文件：`D:/python/CodexTools/mcp.codextools.json`

```json
{
  "mcpServers": {
    "CodexTools": {
      "command": "python",
      "args": ["-u", "-X", "utf8", "your/path/CodexTools/server.py"]
    }
  }
}
```

## MCP 配置（TOML）

文件：`D:/python/CodexTools/codex.config.codextools.toml`

```toml
[mcp_servers.CodexTools]
command = "python"
args = ["-u", "-X", "utf8", "D:/python/CodexTools/server.py"]
enabled = true
```

## 命名变更

- 旧服务名：`utf8-toolbox`
- 新服务名：`CodexTools`
- 推荐 MCP server id：`CodexTools`
