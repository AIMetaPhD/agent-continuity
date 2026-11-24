# Agent Continuity

本地优先的 Agent 工作台：管理版本化定义、项目记忆和可验证的工作交接，支持 Claude Code、Codex 与多设备同步。

每台设备运行一个本地网页应用。项目数据是本地 Git 仓库里的普通文件，源项目不被复制。换 Agent、换设备时，不需要手工搬运整段聊天上下文。

## 功能

- **项目**：登记真实本地目录；项目 ID 与设备路径分离。
- **Agent 版本**：导入或编写定义，保存 revision，查看 diff，激活，应用到 `CLAUDE.md`/`AGENTS.md` 的受管区段，并可恢复应用前内容。
- **记忆**：新增、搜索、修正、停用、显式恢复；保留来源与历史。恢复定义不会复活已撤销记忆。
- **接续**：记录目标、已完成、下一步、证据、base commit、未提交文件和记忆快照；从网页发起真实 Claude Code/Codex 任务并查看 session 与结果。
- **设备与同步**：每台设备保留本地副本，用普通 Git 推送/拉取；冲突显示双方内容，由你选择。不同步活跃 SQLite/WAL、整个 Agent home 或凭据。
- **证据**：saved / delivered / read / used 分层记录；读取证据带 session、revision、时间；没有标记就显示未知，不误报成功。
- **退出**：导出可读 JSON、备份数据仓库、只移除本产品写入的受管区段。

## 快速开始

要求：Python 3.11+、Git。Claude Code 和/或 Codex CLI 可选；要做真实接续才需要。

### Windows

双击 `scripts\start_windows.bat`。首次运行会创建 `.venv`、安装 Flask，然后打开浏览器。

### macOS

双击 `scripts/start_macos.command`；如果被系统拦截：

```bash
chmod +x scripts/start_macos.command
./scripts/start_macos.command
```

### Linux

```bash
chmod +x scripts/start_linux.sh
./scripts/start_linux.sh
```

服务只绑定 `127.0.0.1`，终端会打印真实地址。默认数据目录是 `~/.agent-continuity`，可用 `AGENT_CONTINUITY_HOME` 修改。

仓库内 `examples/demo-project` 是一个合成项目，可直接登记试用，不会碰你的真实工作。

## 五步开始使用

1. 打开终端显示的地址。
2. 点 **新建项目**，输入名称和已有的本地项目目录。
3. 打开 **Agent 版本**，写一版定义并保存；点 **应用到项目**，写入 `CLAUDE.md` 或 `AGENTS.md` 的受管区段。
4. 打开 **记忆**，加入下一个 Agent 需要的事实；当前、历史、撤销分开显示。
5. 打开 **接续**，填写目标/已完成/下一步/证据，创建交接；点 **Claude Code** 或 **Codex** 发起真实任务，页面显示 session、标记和输出。

## Claude Code 与 Codex

- Claude Code：运行 `claude -p ... --output-format json`。如果使用本机 MagPie 等网关，在 **设置** 填写 base URL、token 和模型；否则沿用 CLI 现有登录。
- Codex：运行 `codex exec ... --json`。如果 `CODEX_HOME` 不在环境变量里，可在 **设置** 填写。**模型**可选，会作为 `codex -m` 传入；**Windows 沙箱**可选，会覆盖 `windows.sandbox`（部分 Codex 版本在默认令牌下会拒绝访问项目目录）。提示词通过 stdin 传入，避免 npm 的 `codex.cmd` 包装脚本丢失长的多行参数。
- 任务会先读交接文件，读完后打印 `AC_READ <handoff-id> <revision>`，完成后打印 `AC_DONE <handoff-id>`。应用还会检查真实 CLI transcript 里是否用 Read/Bash 实际访问了交接快照，并记录带 session 和时间的 read 证据。`AC_DONE` 记为 **done reported**，不是独立验证的成功；请检查输出和项目 diff。两种证据都没有时，页面显示 unknown，而不是成功。

Claude 接续在**按项目隔离的接续工作区**里运行：根目录可配置（默认 `~/.agent-continuity/sessions`，Windows 上是 `%USERPROFILE%\.agent-continuity\sessions`），每个项目在其中有一个以项目 ID 命名的固定子目录，同一项目重复接续都回到这个目录。它是普通 Claude Code 项目，在 Claude Desktop 打开它就能看到该项目的历次 session 并继续对话。

Claude CLI 启动时带 `--add-dir <源项目绝对路径>`，因此能按绝对路径读写源项目；交接快照里同时复制了已应用的 Agent 定义和当前记忆，因为加入的目录不保证自动加载源项目自己的 `.claude/` 配置或 hooks。Codex 接续仍在源项目目录内运行。

工作区与源项目相同、或二者互相包含时，应用会在启动接续前直接报错。

任务结束后，session 在工作区里新建或修改的文件会按 SHA-256 **复制**（不删除工作区副本）回源项目。本应用此前回传过的文件，如果项目副本仍是上次写入的内容，恢复会话后再次回传会直接更新；如果你在项目里手工改过它，下次回传会列为冲突而不是覆盖你的修改。其他同名不同内容的文件同样不覆盖，列为冲突并留在工作区。符号链接、目录同名、路径越界和复制失败都会写进任务结果，不会静默丢弃。在 Claude Desktop 恢复会话后，可用交接详情或设置里的 **检查并回传** 再次同步，操作可重复，冲突文件保持不动。同一项目同时只允许一个接续或回传任务。

## 数据布局

```
~/.agent-continuity/
  device.json
  registry.json
  settings.json
  data/<project-id>/          # 每个项目一个 Git 仓库
    project.json
    definitions/<agent>/...
    memories/*.json
    handoffs/*.json
    receipts/<device-id>.jsonl
    jobs/*.json
    backups/...
```

源项目只被 `CLAUDE.md`/`AGENTS.md` 的受管区段修改；其他文件不复制、不改写。

## 同步

在 **设备与同步** 设置私有 Git remote，然后使用 **提交本地 / 推送 / 拉取**。数据变更会自动提交。拉取冲突会显示两边内容，由你选择保留本机或远端。源机离线时，目标机仍能读本地副本。

## 凭据

在 **设置** 中填写的 Claude 网关 token 只保存在本机 `~/.agent-continuity/settings.json`，不会进入 JSON 导出、项目备份、项目目录或数据 Git 仓库。如果不想保存 token，可以沿用 CLI 现有登录。

## 真实限制

- macOS 启动脚本与行为已提供，但本版本没有在物理 Mac 上验证；已验证路径是 Windows 与 WSL2/Linux。
- 真实接续需要可用的 Claude Code 或 Codex CLI；没有 CLI 时，存储与查看仍可用，但不能运行接续任务。
- Claude Code 腿可能走本机网关（如 MagPie）；网关实际路由到哪个模型由网关决定，应用只记录 session，不宣称具体模型。
- 不是实时协同编辑；不要同步活跃 SQLite/WAL 或凭据。
- 这是单用户本地网页，不是多租户服务器。
- 定义加载走宿主标准指令文件（`CLAUDE.md` / `AGENTS.md`）。应用记录“已应用”并在项目里启动新的 CLI 会话；不读取宿主私有加载日志，因此定义读取显示为 applied，而不是宿主确认的 read。

## 开发

```bash
python -m pytest -q
```

直接运行：

```bash
python app.py --port 8765 --open
```

## 依赖与许可证

运行时只依赖 Flask（BSD-3-Clause）。Agent Continuity 本身采用 MIT，见 `LICENSE`；没有复制上游源码。
