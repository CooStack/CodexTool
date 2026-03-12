# CodexTools MCP

CodexTools 是一个基于 Python 的 MCP 工具服务，提供 UTF-8 文本读写、限量读取、批量搜索、批量替换、目录操作、命令执行、图片绘制和提示音能力。

## 策略约束

- `tools/call` 放行前会先确保规则文件存在：Claude 模型使用 `CLAUDE.MD`，Codex/其他模型使用 `AGENTS.MD`。
- 规则文件默认写入当前工作区根目录（优先 `initialize.rootUri/workspaceFolders`，其次 `CODEXTOOLS_WORKSPACE_ROOT`，再兜底服务启动工作目录）。
- 若目标规则文件为空则写入当前版本受管规则块；若已有内容则只维护 `codextools:auto-agent-rules` 的 `start -> end` 受管区间：缺失时追加，发现无版本或旧版本时仅替换该块，避免覆盖其他 MCP 或用户自定义内容。

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

## 浏览器调试器（更像真人操作）

参考 `chrome-devtools-mcp` 的使用体验，浏览器调试工具现在默认更偏“真人可见操作”：

- `browser_session_start` 默认 `headless=false`，会直接打开可见浏览器窗口。
- 默认 `bring_to_front=true`，在关键操作前尽量把页面切到前台，方便用户观察。
- 默认 `human_like=true`，`browser_click` / `browser_type` 会使用更像真人的鼠标移动、点击停顿和键盘逐字输入。
- `chromium` 默认 `prefer_real_chrome=true`，会优先尝试启动本机 `chrome` 渠道；如果失败会自动回退到 Playwright 自带浏览器。
- 默认 `slow_mo_ms=120`、`typing_delay_ms=90`、`pre_action_delay_ms=120`、`post_action_delay_ms=180`，避免操作过于“秒点秒填”。
- 新增 `browser_frame_list`，并让 `browser_click` / `browser_type` / `browser_eval` / `browser_snapshot` 支持 `frame_index`、`frame_name`、`frame_url_contains`，方便调试 `iframe` 内的页面模块。
- 新增 `browser_session_attach`，可通过 CDP 附着到用户已经打开的 Chromium/Chrome/Edge 浏览器；停止会话时只“脱离”不会关闭用户真实浏览器。
- 新增 `browser_handoff_start` / `browser_read_active`：可优先尝试 CDP 接管当前浏览器，失败时自动回退到桌面 OCR 读取与接手模式。
- 新增 `browser_press_key`、`browser_search_visible`、`browser_ask_visible_ai`：支持可见浏览器搜索、在 AI 网站里尝试自动提问，并在需要登录时切换为人工接管。

示例：

```json
{
  "browser": "chromium",
  "url": "https://example.com",
  "human_like": true,
  "bring_to_front": true
}
```

如果需要直接接手用户已经打开的 Chrome / Edge，可先用远程调试端口启动浏览器，例如：

```powershell
chrome.exe --remote-debugging-port=9222
```

然后调用 `browser_handoff_start`（`mode=auto`）或 `browser_session_attach`。如果当前浏览器没有开启 CDP，工具会自动回退到桌面 OCR 接手模式。

如果需要回到纯自动化速度，也可以显式传参覆盖：

```json
{
  "headless": true,
  "human_like": false,
  "slow_mo_ms": 0,
  "typing_delay_ms": 0
}
```

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

`ui_plan_confirm` 可用于可选的计划复核流程，不再作为写操作的硬性前置闸门：
- 点击“继续”：记录当前计划已确认，可继续执行。
- 点击“修改计划”：按提示编辑当前工作区根目录下的 `<工作区名>-plan.md`，然后可再次复核。

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
