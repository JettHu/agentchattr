# Multi-Project 支持 V1：每个项目启动一个独立服务实例

> 日期：2026-05-27
>
> 状态：替代原 single-server `/p/<project_id>` 方案的 V1 实施计划
>
> 目标：用最小侵入方式支持多个项目并行使用 agentchattr，同时保持项目间数据、agent runtime、MCP 连接和 artifact 完全隔离。

---

## 1. 背景

agentchattr 当前是单实例模型：一个 web server、一个 MCP server 组合、一份 `data/`、一组 queue 文件、一组全局 runtime state。`app.py`、`mcp_bridge.py`、`agents.py` 都围绕进程内全局对象工作：

- `MessageStore / RuleStore / JobStore / SessionStore / RuntimeRegistry / AgentTrigger` 在 `configure()` 时一次性绑定。
- WebSocket client 集合是全局的。
- MCP cursors、roles、presence、last_read 也是全局的。
- wrapper 的 tmux session 名称只按 agent 名派生。

原计划尝试在一个 server 内用 `/p/<project_id>/` 承载多个 project。这会要求把大量全局状态改成 `ProjectContext`，并给所有 REST route、WebSocket、MCP tool、broadcast、queue、uploads、artifacts 做 project-aware 分区。实现范围大，且任何漏改都会造成跨项目串台。

本计划把 V1 调整为：

```text
one project = one agentchattr service instance
```

也就是每个项目启动一套独立端口：

```text
Project A:
  web:      8300
  mcp http:8200
  mcp sse: 8201
  data:     /A/.agentchattr/data
  uploads:  /A/.agentchattr/uploads
  artifacts:/A/.agentchattr/artifacts

Project B:
  web:      8301
  mcp http:8202
  mcp sse: 8203
  data:     /B/.agentchattr/data
  uploads:  /B/.agentchattr/uploads
  artifacts:/B/.agentchattr/artifacts
```

UI 不再通过 `/p/<project_id>/` 区分项目，而是通过不同 localhost 端口区分：

```text
http://localhost:8300/
http://localhost:8301/
```

---

## 1.5 前置依赖与环境状态

本计划在编写时，main 分支的 working tree 上有一组**未提交的 artifact pattern 改动**，Phase 1 的多处描述（`_resolve_artifact_root`、`.agentchattr/artifacts/session-<id>/...`、`AGENTCHATTR_ARTIFACT_ROOT`、`session_engine` 的 `artifact_root` 注入、`mcp_bridge` 的 LONG OUTPUT prompt、`/api/artifact` endpoint）都建立在这组改动之上。

涉及文件：

```text
app.py
mcp_bridge.py
session_engine.py
session_store.py
static/chat.js
static/index.html
static/sessions.css
static/sessions.js
session_templates/code-review-strict.json        (新增)
session_templates/debate-strict.json             (新增)
session_templates/design-critique-strict.json    (新增)
session_templates/plan-review.json               (新增)
session_templates/planning-strict.json           (新增)
```

实施 Phase 1 前必须二选一：

1. **推荐**：先把这组 artifact pattern 改动合并到 main，多 project 工作在干净的 baseline 上展开。
2. 把多 project 改动和 artifact pattern 改动合并到同一个 PR，但要清楚 PR 的 diff 由两条独立特性组成。

如果新会话签出 main 后 `grep _resolve_artifact_root app.py` 没有结果，说明 working tree 已经被重置或还未合并 —— 此时**不要**怀疑本计划与代码不一致，应先把上面这组 diff 找回来。

---

## 2. 为什么这样调整

### 2.1 与当前架构天然匹配

当前代码已经通过进程内全局对象表达“一个服务实例只有一个聊天室/runtime”。如果每个项目启动独立服务实例，现有全局对象仍然是正确抽象：

- `store` 只属于当前 project。
- `registry` 只包含当前 project 的 agent。
- `ws_clients` 全部属于当前 project。
- MCP cursors/roles/presence 不需要再按 project_id 分桶。
- `AgentTrigger` 继续写当前实例自己的 queue 文件。

这避免了把整个 `app.py` 和 `mcp_bridge.py` 改造成 multi-tenant server。

### 2.2 明显降低串台风险

single-server 方案中，任何一个 route、broadcast、MCP tool 或 queue path 漏掉 project 维度，都会让 A 项目的消息、状态、artifact 或 agent trigger 泄漏到 B 项目。

multi-service 方案把隔离边界放在进程、端口和目录上：

- A 的 browser 只连 A 的 web port。
- A 的 agent 只连 A 的 MCP port。
- A 的 queue 只在 A 的 data dir。
- A 的 artifact 只在 A 的 `.agentchattr/artifacts`。

隔离主要由 OS 进程和文件路径提供，代码层面的分区压力小很多。

### 2.3 可分阶段独立交付

原 single-server 方案必须改完 server route、WebSocket、MCP bridge、frontend 才能真正避免串台。multi-service V1 可以先交付可用闭环：

1. `--project` 解析和端口分配。
2. server/wrapper 使用同一组 env overrides。
3. agent cwd、data_dir、uploads、artifacts 指向 project 目录。
4. tmux session 名称带上 project/port，支持多项目同时运行。

不需要先完成 `/p/<project_id>/` UI 和 per-route dependency 注入。

### 2.4 保持旧行为稳定

不传 `--project` 时继续使用当前行为：

```text
web:      8300
mcp http:8200
mcp sse: 8201
data:     ./data
cwd:      config.toml 中的 agents.*.cwd
```

显式 `--project` 才启用项目级目录和端口分配。

---

## 3. V1 用户体验

### 3.1 默认项目，完全兼容旧行为

```sh
sh macos-linux/start_claude.sh
```

行为保持不变：使用 `config.toml` 里的默认 cwd、默认端口和 repo 内 `./data`。

### 3.2 显式项目，自动分配端口

```sh
cd ~/work
sh ~/workspace/agentchattr/macos-linux/start_claude.sh --project ./api-server
```

launcher 会：

1. 在 `cd` 进 agentchattr repo 前记录用户原始 `$PWD`。
2. 将 `./api-server` 解析成绝对路径 `~/work/api-server`。
3. 创建目录：

   ```text
   ~/work/api-server/.agentchattr/data/
   ~/work/api-server/.agentchattr/uploads/
   ~/work/api-server/.agentchattr/artifacts/
   ```

4. 给该 project 分配端口，例如：

   ```text
   web:      8301
   mcp http:8202
   mcp sse: 8203
   ```

5. 启动该 project 的 server。
6. 启动 wrapper，并让 agent 的 cwd 是 `~/work/api-server`。
7. 打印可访问地址。V1 不自动打开浏览器，避免跨平台 launcher 复杂度；用户按打印出来的 URL 打开即可：

   ```text
   agentchattr project: api-server
   web UI: http://127.0.0.1:8301/
   ```

### 3.3 手动指定端口

保留现有 override 风格：

```sh
sh macos-linux/start_codex.sh \
  --project ~/work/frontend \
  --port 8302 \
  --mcp-http-port 8204 \
  --mcp-sse-port 8205
```

如果用户手动指定端口，launcher 不再自动分配这组端口，但仍会把 project 的 data/upload/cwd/artifact 绑定好。

---

## 4. 端口与项目注册

### 4.1 新增本地 registry

新增：

```text
data/project_instances.json
```

这是唯一 repo-local 的跨项目索引文件，只记录 project 到端口的映射，不存项目消息内容。

registry 的作用域是当前 agentchattr checkout。多 clone agentchattr 时，每个 clone 各自维护自己的 `data/project_instances.json`；启动时仍必须用真实端口占用状态校验，不能只相信 registry 记录。

建议结构：

```json
{
  "projects": {
    "/Users/me/work/api-server": {
      "project_id": "api-server",
      "web_port": 8301,
      "mcp_http_port": 8202,
      "mcp_sse_port": 8203,
      "data_dir": "/Users/me/work/api-server/.agentchattr/data",
      "upload_dir": "/Users/me/work/api-server/.agentchattr/uploads",
      "artifact_root": "/Users/me/work/api-server/.agentchattr/artifacts",
      "updated_at": 1770000000
    }
  }
}
```

### 4.2 project_id 规则

- 默认 `project_id = basename(abs_project_path)`。
- `--project-name <name>` 只影响显示名、tmux session 前缀和 registry 记录。
- V1 不需要因为 basename 冲突而拒绝启动，因为隔离边界是绝对路径和端口，不是 URL path。
- 如果两个项目 basename 相同，自动在显示名后追加短 hash 也可以接受，例如 `api-server-a1b2c3`。

### 4.3 端口分配规则

默认从以下范围找空闲端口组：

```text
web:      8300-8399
mcp http:8200, 8202, 8204, ...
mcp sse: 8201, 8203, 8205, ...
```

分配原则：

1. 同一个 `abs_project_path` 重复启动时复用已有端口。
2. 如果 registry 里的端口被当前 project 的 server 占用，wrapper 直接连接。
3. 如果端口被无关进程占用，给该 project 分配新的空闲端口组并更新 registry。
4. 如果用户显式传了端口但端口被占用，直接报错，不静默换端口。

并发要求：

- `scripts/resolve_project_instance.py` 必须在“读 registry -> 检查端口 -> 分配端口 -> 写 registry”的完整 read-modify-write 区间持有文件锁。
- Unix 用 `fcntl.flock`；Windows 用 `msvcrt.locking` 或等价 lockfile 机制。
- 写 JSON 时仍使用临时文件 + atomic replace，防止半写文件；但 atomic replace 不能替代文件锁，因为它不能避免 lost update 和重复端口分配。

---

## 5. 实施计划

### Phase 1：project env 和端口编排

目标：`--project` 可以启动独立服务实例，数据和 agent cwd 都落到 project 目录。

推荐执行顺序：

1. 先实现 `scripts/resolve_project_instance.py`
   - 只用标准库。
   - 支持输入 project path / project name / 手动端口。
   - 完成 project path 解析、目录创建、端口探测、registry 文件锁、registry 更新。
   - 输出格式先固定下来，供 shell 和 bat launcher 消费。
2. 扩展 `config_loader.py`
   - 增加 project/artifact/env overrides。
   - 修正 override 类型，确保 `PROJECT_ID` / `PROJECT_NAME` 不会被当成路径解析。
   - `AGENTCHATTR_PROJECT` 存在时覆盖所有 agent cwd。
3. 扩展 `run.py`
   - argparse 接受 project 相关 flags。
   - 确保 `apply_cli_overrides()` 在 `load_config()` 前生效。
   - 增加 `/api/instance` 需要的数据来源。
4. 扩展 `app.py`
   - `_resolve_artifact_root()` 优先读 explicit artifact root。
   - 增加 `GET /api/instance`，返回 project path、project_id、web/MCP ports、data_dir、artifact_root。
5. 扩展 `wrapper.py`
   - argparse 接受 project 相关 flags，避免透传给 agent CLI。
   - 保持 wrapper/server 使用同一组 data_dir 和 ports。
   - 修正 `project_dir` 计算，确保 MCP 注入、provider launch、agent cwd 都指向 project path。
   - tmux session name 纳入 project/port 维度。
   - 不需要改 `mcp_bridge.py` 和 `registry.py`。V1 一个 server 进程只服务一个 project，MCP cursors、roles、presence 仍然是该进程内的正确单一真相，不需要加 project 维度。
6. 改 launcher
   - 先改 `macos-linux/start.sh` 和一个代表性 agent launcher（建议 `start_claude.sh`）跑通。
   - 验证后再批量同步到其余 `macos-linux/*.sh` 和 `windows/*.bat`。
   - server spawn 必须显式传 CLI flags，不能依赖 env 继承。
7. 最后处理文案和 UI 提示
   - launcher 打印 project、cwd、web URL、MCP ports。
   - `/api/instance` 可供前端 header 后续展示；Phase 1 只要 endpoint 可用即可。

改动：

1. 新增 `scripts/resolve_project_instance.py`
   - 输入：`--project`、`--project-name`、可选 `--port`、`--mcp-http-port`、`--mcp-sse-port`
   - 必须 stdlib-only，因为 launcher 会在 venv bootstrap 之前或之外调用它。允许使用 `argparse`、`json`、`socket`、`pathlib`、`fcntl`、`msvcrt`、`os`、`tempfile` 等标准库；不要 import `requirements.txt` 里的第三方包。
   - 持有 registry 文件锁完成端口分配和 registry 写入，避免两个 launcher 同时启动时分配到同一组端口。
   - 输出 shell/bat 可消费的 env：

     ```text
     AGENTCHATTR_PROJECT=/abs/project
     AGENTCHATTR_PROJECT_ID=api-server
     AGENTCHATTR_DATA_DIR=/abs/project/.agentchattr/data
     AGENTCHATTR_UPLOAD_DIR=/abs/project/.agentchattr/uploads
     AGENTCHATTR_ARTIFACT_ROOT=/abs/project/.agentchattr/artifacts
     AGENTCHATTR_PORT=8301
     AGENTCHATTR_MCP_HTTP_PORT=8202
     AGENTCHATTR_MCP_SSE_PORT=8203
     ```

2. `config_loader.py`
   - 现有 `_ENV_OVERRIDES` 表已经有 5 项，tuple 结构是 `(env_var, section, key, is_int)`：

     ```python
     _ENV_OVERRIDES = [
         ("AGENTCHATTR_DATA_DIR",      "server", "data_dir",   False),
         ("AGENTCHATTR_PORT",          "server", "port",       True),
         ("AGENTCHATTR_MCP_HTTP_PORT", "mcp",    "http_port",  True),
         ("AGENTCHATTR_MCP_SSE_PORT",  "mcp",    "sse_port",   True),
         ("AGENTCHATTR_UPLOAD_DIR",    "images", "upload_dir", False),
     ]
     ```

     `CLI_OVERRIDE_FLAGS` 是 `(--flag, env_var)`，已对应上面 5 个 env。

   - 新增 env/CLI overrides（注意 `is_int=False`，全是字符串）：

     ```text
     AGENTCHATTR_PROJECT         → (project, path,         False)
     AGENTCHATTR_PROJECT_NAME    → (project, name,         False)
     AGENTCHATTR_PROJECT_ID      → (project, id,           False)
     AGENTCHATTR_ARTIFACT_ROOT   → (server,  artifact_root, False)
     ```

     section/key 名按 config.toml 约定可自行调整；以上是建议命名，目标是让 `cfg["project"]["path"]` / `cfg["server"]["artifact_root"]` 之类的访问形式自然存在。

   - `AGENTCHATTR_PROJECT_NAME / PROJECT_ID` 是显示名/标识，不能按 path 类型解析（不要 `Path(...)` 也不要绝对路径化）。
   - 如果 `AGENTCHATTR_PROJECT` 存在，把所有有 `cwd` 的 agent cwd 覆盖为该绝对路径。这一步在 `_apply_env_overrides` 之后单独执行（因为它操作的是 `agents.*.cwd` 这种动态条目，不是固定 section/key）。

3. `app.py`
   - `_resolve_artifact_root()` 优先使用 `AGENTCHATTR_ARTIFACT_ROOT` 或 config 中的 artifact root。
   - `/api/platform` 或 `/api/settings` 可以附带当前 project_id/web_port，供 UI 显示。

4. `run.py`
   - argparse 增加 `--project`、`--project-name`、`--artifact-root`，避免直接运行时报 unknown argument。
   - 继续通过 `apply_cli_overrides()` 写入 env。

5. `wrapper.py`
   - argparse 增加 `--project`、`--project-name`、`--artifact-root`，避免参数被透传给 agent CLI。
   - `data_dir` 继续从 config 的 `server.data_dir` 读取；launcher 已经设置为 project data dir。
   - `project_dir` 从被覆盖后的 agent cwd 得到。
   - tmux session 名称改为带 project/port 前缀：

     ```text
     agentchattr-8301-claude
     ```

     或：

     ```text
     agentchattr-api-server-claude
     ```

6. `macos-linux/*.sh` 和 `windows/*.bat`
   - 在 `cd "$(dirname "$0")/.."` 前保存原始 cwd。
   - 解析 `--project`、`--project-name` 和端口参数。
   - 调用 `scripts/resolve_project_instance.py`。
   - `is_server_running()` 检查当前 project 的 web port，而不是硬编码 8300。
   - spawn server 时传同一组 CLI 参数，不依赖新终端透明继承 env。当前 launcher 会用 `osascript` / `gnome-terminal` / `xterm` 拉起新终端，跨平台 env 继承行为不应作为正确性前提。
   - `start.sh` 也必须支持 `--project`、`--project-name`、`--port`、`--mcp-http-port`、`--mcp-sse-port`、`--artifact-root`，这样 agent launcher 在需要拉起 server 时可以把完整参数拼到 `start.sh` 或 `run.py` 命令行里。
   - wrapper 启动时也传同一组 env/CLI 参数。
   - 启动完成后打印当前 project 的 web URL；V1 不要求自动打开浏览器。

验收：

1. 不传 `--project`，旧行为不变。
2. `--project ./api-server` 后，agent tmux cwd 是 `api-server`。
3. `api-server/.agentchattr/data` 下出现 queue、cursors、roles 等文件。
4. `api-server/.agentchattr/artifacts` 被 server 创建，artifact preview 能读回。
5. 同时启动两个 basename 不同的 project，分别使用不同 web/MCP 端口。
6. `tmux ls` 中两个 project 的 `claude` session 不互相 kill。
7. 两个 launcher 并发启动不同 project 时，不会拿到同一组端口，`data/project_instances.json` 不会丢记录。
8. 当 agent launcher 自动 spawn server 时，server 的 `/api/instance` 返回的 project path 和端口必须与 wrapper 使用的 project path 和端口一致，不能回落到默认 8300/8200/8201。

Phase 1 done criteria：

- 所有默认启动脚本仍能按旧方式启动，不要求用户传任何新参数。
- 至少一个 macOS/Linux agent launcher 和一个 Windows launcher 完成 `--project` 路径解析、端口传递、server spawn、wrapper 启动闭环；随后批量同步到全部 launcher。
- `run.py --project ... --port ... --mcp-http-port ... --mcp-sse-port ...` 可以直接启动正确实例。
- `wrapper.py <agent> --project ... --port ... --mcp-http-port ... --mcp-sse-port ...` 不会把 project flags 透传给 agent CLI。
- `/api/instance` 可以作为验证单一真相：launcher 打印的 project/ports、wrapper 使用的 project/ports、server 返回的 project/ports 必须一致。
- 不要求解决 home-scoped settings_file agent 并行问题；但如果启动这类 agent，输出里应有明确 caveat 或文档可查。
- 不要求前端 header 展示 project；那属于 Phase 2。

### Phase 2：UI 项目信息与可观测性

目标：用户能清楚知道当前浏览器 tab 属于哪个 project。

推荐执行顺序：

1. 先稳定 `GET /api/instance`
   - 如果 Phase 1 已经实现 endpoint，这一步只补齐字段和测试。
   - endpoint 返回值必须来自当前进程实际 config/env，而不是只读 registry 缓存。
   - 字段至少包含 `project_id`、`project_path`、`web_port`、`mcp_http_port`、`mcp_sse_port`、`data_dir`、`upload_dir`、`artifact_root`。
2. 前端读取实例信息
   - 在现有前端初始化流程里请求 `/api/instance`。
   - 请求失败时不阻塞旧 UI，显示默认/unknown 即可。
   - 不引入 `/p/<project_id>/` 路由，也不改 WebSocket URL。
3. UI header 展示 project 信息
   - 顶部展示 project id 和端口。
   - cwd/project path 可以放在 tooltip、title 或紧凑 secondary text，避免挤占主聊天区域。
   - 默认实例也要显示，避免用户误以为只有非默认 project 才有 project identity。
   - `document.title` 也带 project_id 前缀，例如 `[api-server] agentchattr`，方便多个 project tab 折叠后区分。
4. launcher 输出保持与 UI 一致
   - launcher 打印的 project/path/ports 必须和 `/api/instance` 一致。
   - 若 `/api/instance` 不可达，launcher 仍打印本地解析结果和错误提示，方便排查。

改动：

1. `static/index.html` 顶部显示：

   ```text
   project: api-server
   port: 8301
   cwd: /Users/me/work/api-server
   ```

2. `app.py` 增加只读 endpoint：

   ```text
   GET /api/instance
   ```

   返回：

   ```json
   {
     "project_id": "api-server",
     "project_path": "/Users/me/work/api-server",
     "web_port": 8301,
     "mcp_http_port": 8202,
     "mcp_sse_port": 8203,
     "data_dir": ".../.agentchattr/data",
     "artifact_root": ".../.agentchattr/artifacts"
   }
   ```

3. launcher 启动后打印当前实例信息。

验收：

1. 打开两个不同端口，header 能区分 project。
2. `/api/instance` 返回的 project_path 与 agent cwd 一致。
3. 默认实例 `http://127.0.0.1:8300/` 也显示 instance 信息，不破坏旧聊天功能。
4. 非默认实例刷新页面后仍显示正确 project 信息。
5. `/api/instance` 信息与 launcher 输出一致。

Phase 2 done criteria：

- 前端没有引入 project path routing；仍然只按当前 origin 访问 REST 和 WebSocket。
- UI 能清楚区分两个同时打开的 project tab。
- `/api/instance` 失败不会让聊天主界面不可用。
- 默认实例和非默认实例都通过同一套 UI 展示逻辑。
- 不要求实现 project list、stop、forget 等管理能力；那属于 Phase 3。

### Phase 3：管理命令和清理

目标：让多实例使用更顺手，但不影响 V1 的核心隔离。

推荐执行顺序：

1. 先实现只读 list 能力
   - 从 `data/project_instances.json` 读取已知 project。
   - 对每条记录先探测 web port 是否仍在监听，再调用该端口的 `/api/instance` 验证返回的 `project_path` 与 registry 记录一致。
   - 端口不监听时标为 `stale`；端口监听但 `/api/instance` 不存在或 `project_path` 不匹配时标为 `port-conflict`；两项都通过才标为 `running`。
   - 输出 project path、project_id、web URL、MCP ports、data_dir、last updated、status。
2. 再实现 forget 清理能力
   - 只删除 registry 记录，不删除 project 下的 `.agentchattr/` 数据。
   - 支持按 `--project` 或绝对 path 清理。
   - 如果目标 project 当前 server 仍在运行，默认拒绝 forget，除非显式 `--force`。
3. 最后考虑 stop 能力
   - V1 可以先不做 stop，因为当前 server 没有进程管理 registry。
   - 如果要做 stop，需要明确记录 pid 或通过端口定位进程；跨平台实现必须谨慎，避免误杀无关进程。
   - 推荐先交付 `list` / `forget`，把 `stop_project.sh` 作为后续可选项。
4. 同步 shell/bat 包装命令
   - 如果 Python 子命令已足够清晰，shell/bat wrapper 只做薄封装。
   - 不要增加第二套独立逻辑，避免 registry 解析和端口判断分叉。

可选新增：

```sh
sh macos-linux/list_projects.sh
sh macos-linux/stop_project.sh --project ./api-server
```

或者提供 Python 子命令：

```sh
python scripts/resolve_project_instance.py list
python scripts/resolve_project_instance.py forget --project ./api-server
```

验收：

1. 能列出 registry 中记录的 project、端口和最近启动时间。
2. 能清理不存在项目或已废弃项目的 registry 记录。
3. `list` 能标出 running / stale / port-conflict 这类状态。
4. `forget` 不删除任何 project 数据文件，只更新 registry。
5. 多 clone agentchattr 时，每个 checkout 的 list/forget 只影响自己的 `data/project_instances.json`。

Phase 3 done criteria：

- `python scripts/resolve_project_instance.py list` 或等价命令可以作为主要管理入口。
- stale registry 记录可以安全清理，不影响正在运行的 project。
- 管理命令不破坏 Phase 1 的启动路径；启动不依赖用户先运行 list/cleanup。
- 如果没有实现 stop，要在文档和 CLI help 中明确说明 stop 尚不可用。
- 如果实现 stop，必须有防误杀校验：端口、project path、pid/command line 至少两项匹配后才允许停止。

---

## 6. 明确不做的事

V1 不做：

- 不做 `/p/<project_id>/` URL 前缀。
- 不做单 server 内多 project route 注入。
- 不做 WebSocket project 分桶。
- 不做 MCP bridge 全局状态分桶。
- 不做一个 UI 内切换所有 project。
- 不做自动打开浏览器；launcher 只负责打印正确 URL。
- 不改变 `open_chat.html` 的默认用途。它仍然只跳到 `http://127.0.0.1:8300`，也就是默认实例；非默认 project 不应引导用户通过它进入 UI。
- 不做 per-project agent roster/config。所有 project 仍共享当前 agentchattr checkout 的 `config.toml` / `config.local.toml`，V1 只覆盖 cwd、data_dir、upload_dir、artifact_root 和端口。
- 不强制迁移旧 `./data`。

这些可以作为 V2 重新评估，但不阻塞 V1。

---

## 7. 风险与约束

### 7.1 多端口是用户可见复杂度

用户需要知道不同 project 对应不同 URL。用 launcher 打印地址和 UI header 可以缓解。

### 7.2 多进程会多占资源

每个 project 都有 web server、MCP server、wrapper 和 agent CLI。相比 single-server 方案更耗资源，但 agentchattr 的 server 本身很轻，主要资源仍来自 agent CLI。

### 7.3 端口 registry 需要处理陈旧记录

进程异常退出后 registry 可能仍记录旧端口。启动时必须用实际端口占用状态校验 registry，而不是盲信 JSON。

### 7.4 tmux session name 必须纳入 project 维度

即使服务端口隔离，两个 project 仍可能都注册 `claude`。如果 tmux session 仍叫 `agentchattr-claude`，后启动的 wrapper 会 kill 前一个 project 的 tmux session。Phase 1 必须修这个点。

### 7.5 home-scoped settings_file agent 不能天然并存

部分 agent 使用 `mcp_inject = "settings_file"`。如果 `mcp_settings_path` 是用户 home 下的全局路径，例如：

```text
~/.codebuddy/.mcp.json
~/.copilot/mcp-config.json
```

那么两个 project 同时启动同名 agent 时，后启动的 wrapper 会把该全局 settings 文件里的 agentchattr MCP URL 改成自己的端口。前一个 project 的同名 agent 如果重新读取这份 settings，就可能连到后一个 project 的 MCP server。

V1 Phase 1 不承诺解决这类 home-scoped settings_file agent 的跨项目并存。可接受处理：

- 明确标注 `codebuddy` / `copilot` 等 home-scoped settings_file agent 不支持同一 checkout 内跨项目并行运行。
- 如果某个 CLI 支持通过 env 或 flag 指定 settings 路径，则后续可以把 settings 文件改到 project/port scoped 路径，例如 `.../provider-config/<agent>-<port>-settings.json`，并在启动时把该路径传给 agent。

相对路径 settings_file 不属于这个问题。比如 `.qwen/settings.json` 会按 project cwd 解析到各自项目目录，因此可以随 V1 的 cwd override 自然分开。

### 7.6 repo-local registry 不是全局真相

`data/project_instances.json` 只属于当前 agentchattr checkout。多个 checkout 各自维护自己的 registry，这是 V1 可接受边界。启动时必须以实际端口监听状态为准，registry 只作为端口复用和 UX 提示的缓存。

### 7.7 mcp_proxy.py 当前不进入端口 registry

`mcp_proxy.py` 用于 `mcp_inject = "proxy_flag"` 的 agent。当前 wrapper 创建 `McpIdentityProxy` 时不传固定端口，proxy 默认 `port=0`，由 OS 分配临时本地端口；wrapper 再把 `proxy.url` 传给 agent。V1 的 project 端口 registry 只需要管理 web/MCP upstream 端口，不需要管理这些 per-wrapper 临时 proxy 端口。

实现时仍要验证这一点：

- `McpIdentityProxy` 继续使用 OS-assigned ephemeral port。
- proxy upstream 指向当前 project 的 `mcp.http_port` / `mcp.sse_port`。
- 如果未来改成固定 proxy port，必须把 proxy port 纳入 project/agent 维度，避免跨项目同名 agent 互踩。

---

## 8. 端到端验证矩阵

### 默认回归

```sh
sh macos-linux/start_claude.sh
curl http://127.0.0.1:8300/api/platform
```

预期：

- 使用旧端口。
- 使用 repo `./data`。
- agent cwd 仍按 `config.toml`。

### 单 project

```sh
cd ~/work
sh ~/workspace/agentchattr/macos-linux/start_codex.sh --project ./api-server
```

预期：

- 输出 web URL，例如 `http://127.0.0.1:8301/`。
- Codex cwd 是 `~/work/api-server`。
- queue 在 `~/work/api-server/.agentchattr/data/`。
- artifact 在 `~/work/api-server/.agentchattr/artifacts/`。

### 双 project 并行

```sh
cd ~/work
sh ~/workspace/agentchattr/macos-linux/start_claude.sh --project ./api-server
sh ~/workspace/agentchattr/macos-linux/start_codex.sh  --project ./frontend
```

预期：

- 两个服务使用不同 web/MCP 端口。
- 两个浏览器 tab 的消息互不可见。
- A 项目 @agent 只写 A 的 queue。
- B 项目 UI 不出现 A 的 status/message/session 事件。
- `tmux ls` 中两个 session 都存在。

### artifact 读回

在 `api-server` 项目内让 agent 写：

```text
.agentchattr/artifacts/general/test-plan.md
```

预期：

- chat 中 artifact card 能 preview。
- `/api/artifact?path=.agentchattr/artifacts/general/test-plan.md` 读取的是 `api-server` 项目下的文件。

### 测试执行记录（2026-05-27）

V1 全套（Phase 1-3 + §8 端到端）已在本地一台 macOS 上跑过。下面是已验证项与方法。

**Phase 1（端口编排 + cwd / data_dir 隔离）**

- ✅ 6 并发 launcher 各拿到独立 web 端口（plan §5 Phase 1 验收 #7：并发不抢端口）
- ✅ `_port_is_free` 用 bind-then-close 探测；锁释放与 server 实际 bind 之间的 TOCTOU 已在脚本顶部 docstring 标注
- ✅ tmux session name 含 port（`agentchattr-<port>-<agent>`），双 project 同名 agent 不互踩（plan §7.4 fix）
- ✅ `wrapper.py:878-879` 用 `AGENTCHATTR_PORT` 命名 session
- ✅ Phase 1 reviewer 三视角（correctness / cross-platform / concurrency）闭环：2 must-fix + 4 建议 fix 全修

**Phase 2（UI project 信息）**

- ✅ `/api/instance` 返回 `project_id` / `project_name` / `project_path` / `web_port` / `mcp_*_port` / `data_dir` / `upload_dir` / `artifact_root`
- ✅ 默认实例 `project_id` 来自 cwd basename（即 `agentchattr`）
- ✅ Phase 2 reviewer 闭环

**Phase 3（管理命令）**

- ✅ `python scripts/resolve_project_instance.py list` 表格 + `--json` 双格式
- ✅ 三态分类：`running` / `stale` / `port-conflict`（port-conflict 触发条件 = 端口在监听但 `/api/instance` 返回的 `project_path` 不匹配 registry 记录）
- ✅ `forget --project <path>` 单清；`forget --all-stale` 批清；running 默认 refuse，`--force` 可覆盖
- ✅ `forget` 不删 `.agentchattr/{data,uploads,artifacts}`
- ✅ `stop` 子命令 + `--help` 都明确 V1 不可用，提示用 `kill <pid>` 或关闭 launcher 终端
- ✅ Registry 路径 = `<repo>/data/project_instances.json`，per-checkout

**§8 端到端验证矩阵**

| 项目 | 方法 | 结果 |
|---|---|---|
| 默认回归 | `run.py` 不带 `--project` 直接起 | server bind 8300 / 老 `./data/` ✓ |
| 单 project | resolve `~/workspace/test-mp-A` → 用其端口起 server | 端口 8301 / data 在 `~/workspace/test-mp-A/.agentchattr/` ✓ |
| 双 project 并行 | resolve A 拿 8301 / 8202-03，再 resolve B 拿 8302 / 8204-05；起两个 server | 两个 `/api/instance` 各自返回正确 `project_path`；`list` 显示两者 `running` ✓ |
| artifact 读回 + 跨实例隔离 | 在 `~/workspace/test-mp-A` 和 `~/workspace/test-mp-B` 下预埋 `.agentchattr/artifacts/general/test-plan.md`；用各自 token 调 `/api/artifact?path=...` | A 读到 A 的文件、B 读到 B 的文件、A 用 B 的 token 被 403 拒（cross-instance token isolation）✓ |
| tmux 双 session 共存 | nohup 起两个 wrapper（codex 接 A，claude 接 B），`tmux ls` | `agentchattr-8301-codex` + `agentchattr-8302-claude` 同时在跑、agent CLI 在各自 session 内启动且 cwd 正确 ✓ |
| agent 实写 artifact 触发 chat preview | 用 ws 给 B 注 user 消息 `@claude 请创建 .agentchattr/artifacts/general/hello-B.md ...` | claude 通过 MCP 读 channel → 在 B 的 `.agentchattr/artifacts/general/hello-B.md` 写入 → chat 里存回复 → `/api/artifact?path=...` 读回内容一致 ✓ |

**未由本地测试覆盖的 caveat**

- codex 端到端在本机被 codex 自身的 `PermissionRequest` hooks 阻塞在 `chat_claim` MCP 调用前（用户本地 codex 配置问题，与 V1 plumbing 无关）。multi-project V1 的链路（消息送达 → wrapper queue watcher → tmux send-keys 注入 → 注入文本"use mcp to read #channel..." 已抵达 codex prompt）已在 codex tmux session 里观察到。
- `start.sh` 在 macOS 走 `osascript` 弹 `Terminal.app` 启 server 的分支没在自动化测试里验证（osascript 弹的真实窗口需要 GUI 环境）；测试改走 `nohup .venv/bin/python run.py ...` 直接起 server，等价于 launcher 在 server-already-running 分支只跑 wrapper.py 的情形。
- `home-scoped settings_file` 类 agent（`copilot` / `codebuddy`）的同 checkout 跨 project 并行不支持（plan §7.5 已声明），未做反向验证。

**测试用环境清理**

```sh
# 杀掉所有测试相关进程
while read pid; do kill $pid 2>/dev/null; done < /tmp/mptest-pids.txt
pkill -f "run.py.*test-mp-"
pkill -f "wrapper.py.*test-mp-"
tmux kill-session -t agentchattr-8301-codex
tmux kill-session -t agentchattr-8302-claude

# 清掉测试 project（registry 用 forget 清）
.venv/bin/python scripts/resolve_project_instance.py forget --project ~/workspace/test-mp-A --force
.venv/bin/python scripts/resolve_project_instance.py forget --project ~/workspace/test-mp-B --force
rm -rf ~/workspace/test-mp-A ~/workspace/test-mp-B
```

---

## 9. 后续 V2 决策点

只有当 V1 使用后确认“多个端口不可接受”或“必须在一个 UI 内跨项目切换”时，再考虑 V2 single-server multi-project：

- `/p/<project_id>/` URL 前缀。
- `ProjectContext` 承载所有 stores/runtime。
- WebSocket 按 project 分桶。
- MCP tools 按 authenticated instance 的 project_id 路由。
- frontend 全量 `apiUrl()` / `wsUrl()` 改造。

V2 是 multi-tenant server 改造，不应混入 V1。

---

## 附录 A：Launcher 文件清单（30 个）

Phase 1 步骤 6 提到"先改一个 `start.sh` + 一个代表性 agent launcher 跑通，再批量同步"。下面列出完整清单和分类，便于估算工作量和确认覆盖。

### A.1 macOS / Linux （`macos-linux/`，15 个）

**Server-only**（单独适配 server-spawn 路径，必须完整接收 `--project` / `--port` / `--mcp-http-port` / `--mcp-sse-port` / `--artifact-root` / `--project-name` 这套 flag）：

- `start.sh`

**标准 agent / API launcher**（共 10 个，统一改：抓 `$PWD` → 解析 project 相关 flag → 调 `scripts/resolve_project_instance.py` → spawn server/wrapper 时透传完整 CLI flag；同时保留各自已有的位置参数或环境变量检查）：

- `start_claude.sh` ← 推荐选作"代表性 agent launcher"先跑通
- `start_codex.sh`
- `start_gemini.sh`
- `start_kimi.sh`
- `start_qwen.sh`
- `start_kilo.sh`（已有位置参数 `provider/model` 透传先例，注意不要被 project flag 覆盖）
- `start_codebuddy.sh`（home-scoped settings_file，§7.5 caveat 适用）
- `start_copilot.sh`（home-scoped settings_file，§7.5 caveat 适用）
- `start_minimax.sh`（API agent，无 cwd 概念但仍需 data_dir/port 隔离）
- `start_api_agent.sh`（API agent，已有必填位置参数 `<agent_name>`；project flag 解析不能吞掉这个参数）

**Auto-approve 变体**（共 4 个，本质上是标准 launcher 加一个固定 CLI flag，批量同步阶段直接复用主 launcher 的 patch 模板）：

- `start_claude_skip-permissions.sh`
- `start_codex_bypass.sh`
- `start_gemini_yolo.sh`
- `start_qwen_yolo.sh`

### A.2 Windows （`windows/`，15 个）

文件名与 `macos-linux/` 一一对应，扩展名为 `.bat`。改造逻辑等价，但 shell 语法差异较大：

- arg 解析建议用 `setlocal enabledelayedexpansion` + `SHIFT` 循环处理 `%1` / `%2`，并保留 `start_kilo.bat`、`start_api_agent.bat` 等已有位置参数；不要复制 POSIX shell 的解析方式。
- 抓原始 `$PWD` 对应 `set ORIG_PWD=%CD%`（必须在 `cd /d "%~dp0.."` 之前）
- `is_server_running` 当前用 `netstat -ano | findstr :8300 | findstr LISTENING`，要改成读 `AGENTCHATTR_PORT` 后再 findstr

清单：

- `start.bat`
- `start_claude.bat`、`start_codex.bat`、`start_gemini.bat`、`start_kimi.bat`、`start_qwen.bat`、`start_kilo.bat`、`start_codebuddy.bat`、`start_copilot.bat`、`start_minimax.bat`、`start_api_agent.bat`
- `start_claude_skip-permissions.bat`、`start_codex_bypass.bat`、`start_gemini_yolo.bat`、`start_qwen_yolo.bat`

### A.3 推荐批量同步顺序

1. `macos-linux/start.sh`（server-only，唯一无 agent 依赖的 launcher，最易验证）
2. `macos-linux/start_claude.sh`（标准 agent，无 home-scoped settings_file 互踩问题）
3. 上面两个跑通后 → 同步 `macos-linux/` 其余 13 个
4. `windows/start.bat` + `windows/start_claude.bat` 各跑通一次
5. 同步 `windows/` 其余 13 个

**不要**先并行改 30 个 —— Phase 1 done criteria 第 2 条要求"先一个 macOS + 一个 Windows 跑通"再批量，是为了让 shell 语法差异和 server-spawn 行为差异在小范围暴露。
