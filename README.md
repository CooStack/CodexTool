# CodexTools MCP

CodexTools 是一个基于 Python 的 MCP 工具服务，提供 UTF-8 文本读写、限量读取、批量搜索、批量替换、目录操作、命令执行、图片绘制和提示音能力。

## 当前目录与迁移说明

本项目已按你的要求迁移到根目录路径：`D:/python/CodexTools`。

旧路径（已移除）：`D:/python/CodexTools/utf8_tools_mcp/`

当前核心文件：

- `D:/python/CodexTools/server.py`
- `D:/python/CodexTools/AGENT_RULES.md`
- `D:/python/CodexTools/examples/`
- `D:/python/CodexTools/codex.config.codextools.toml`
- `D:/python/CodexTools/mcp.codextools.json`

## 工具列表

- `fs_read_text`
- `fs_read_texts`
- `fs_write_text`
- `fs_replace_text`
- `fs_replace_regex`
- `fs_patch_lines`
- `fs_list`
- `fs_list_files`
- `fs_search_text`
- `fs_stat`
- `fs_delete`
- `fs_move`
- `fs_move_file`
- `fs_copy_file`
- `fs_create`
- `plan_create`
- `plan_update`
- `plan_view`
- `plan_list`
- `plan_archive`
- `change_begin`
- `change_set_active`
- `change_get`
- `change_list`
- `change_commit`
- `change_rollback`
- `proc_run`
- `img_draw`
- `sound_beep`

## 计划可视化与变更回滚

典型流程：

1. `plan_create` 创建任务步骤。
2. 执行过程中用 `plan_update` 更新步骤状态，用 `plan_view`/`plan_list` 查看进度可视化。
3. 需要可撤回时先调用 `change_begin`，后续文件修改会自动记录快照。
4. 用 `change_get`/`change_list` 查看本次改动。
5. 确认后 `change_commit`，或用 `change_rollback` 一键回滚。

## 启动

```powershell
python -u -X utf8 D:/python/CodexTools/server.py
```

可选依赖（仅 `img_draw` 需要）：

```powershell
python -m pip install pillow
```

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
