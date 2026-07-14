# CodeForge

> 一款运行在 **Termux / Linux / macOS / Windows** 上的轻量级 **AI Agent IDE**。
> 基于 Flask + OpenAI 兼容协议,内置 **plan / act / answer** 三阶段工作流,
> 让模型在改动你的代码之前,先把方案摆到桌面上等你点头。

---

## ✨ 核心特性

| 模块 | 能力 |
| --- | --- |
| 🤖 **Agent Loop** | `plan → act → answer` 闭环;涉及写文件前必须先出方案,等用户确认才落盘 |
| 🧠 **多模型热切换** | 通过 Web UI 实时切换 `MiniMax-M3` / DeepSeek / GLM 等模型,无需重启 |
| 📂 **文件/目录管理** | 创建、重命名、复制、移动、删除、读取、保存、移动设备端默认根 `/sdcard` |
| 🔍 **全局搜索** | 子串大小写不敏感,跨目录递归定位 |
| 🌿 **Git 集成** | `status / diff / add / reset / commit / push / discard` 全套 API |
| 🔐 **SSH 远程会话** | 多会话管理、远程列目录、远端执行命令(密码或私钥) |
| 💻 **本地终端** | 受 timeout 约束的 shell 执行,可绑定 cwd,支持转 SSH |
| 🟢 **Diff 审查** | 改前先存基线,改后渲染 unified diff + 行级高亮,**支持一键回滚 / 应用 / 丢弃** |
| 📡 **SSE 流式输出** | 边想边推,前端实时显示「第 N 轮 / 调用工具 X / 工具返回」 |
| 📱 **移动端适配** | 通过 UA 自动识别,默认根目录 `/sdcard`,适合手机/平板浏览器 |
| 🚀 **一键启动** | `start.sh` / `start.ps1` 自动建 venv、装依赖、检测端口冲突、开浏览器 |

---

## 📁 目录结构

```
codeforge/
├── main.py                 # Flask 入口 + 全部 HTTP API
├── ssh.py                  # SSH 会话管理器(asyncssh)
├── start.sh                # Linux / macOS / Termux 一键启动
├── start.ps1               # Windows PowerShell 一键启动
├── requirement.txt         # 依赖清单
├── .config.json            # 全局开关(flow / max_round)
├── AGENTS.md               # AI Agent 安全/代码/回复规范
├── agent/                  # Agent 核心
│   ├── loop.py             #   主循环(run / run_stream)
│   ├── state.py            #   plan_model / auto / pending
│   ├── tool.py             #   工具注册与调度
│   ├── diff.py             #   diff 暂存、基线、revert / apply
│   └── prompt.json         #   系统提示词 + 工具规则
├── explorer/               # 本地文件系统
│   ├── file.py             #   增删改查 + 改模式(write/append)
│   └── search.py           #   全局子串搜索
├── git/                    # Git 子进程封装
│   └── engine.py
├── models/                 # 模型管理
│   ├── model.json          #   模型清单 + 当前激活
│   └── request.py          #   OpenAI 兼容协议调用
├── terminal/               # 本地 shell 执行器
│   └── engine.py
├── static/                 # Web 前端
│   ├── index.html
│   ├── app.js
│   ├── style.css
│   └── animations.css
├── test/                   # 单元/集成测试
│   └── test.py
└── log/                    # 运行日志(自动生成)
    ├── server.log
    └── server.err.log
```

---

## 🚀 快速开始

### 环境要求
- **Python 3.10+**
- 可访问公网(或自建 OpenAI 兼容协议的 API)
- Windows 用户建议使用 PowerShell 或 Git Bash

### 安装与启动

#### Linux / macOS / Termux
```bash
git clone <your-repo-url> codeforge
cd codeforge
./start.sh                       # 默认 0.0.0.0:9191,自动开浏览器
./start.sh --port 8080           # 自定义端口
./start.sh --host 127.0.0.1      # 自定义监听地址
./start.sh --no-browser          # 不自动开浏览器
./start.sh --rebuild             # 强制重建 .venv
./start.sh --update              # 只更新 pip 依赖
./start.sh --dev                 # 开发模式(启用 Flask debug)
```

#### Windows
```powershell
# PowerShell 首次需放宽执行策略
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned

.\start.ps1
```
脚本会自动:
1. 探测/创建 `.venv`
2. 升级 pip 并安装 `requirement.txt` 中的依赖
3. 检测目标端口占用,冲突时询问是否继续
4. 后台拉起浏览器,前台保持 Flask 运行

启动成功后访问 **<http://localhost:9191/>** 即可。

---

## ⚙️ 配置

### `.config.json` — 全局开关
```json
{
  "flow":      "true",   // 是否启用"流式思考 + 分步确认"模式
  "max_round": 20        // Agent 单次会话最多自动跑的轮数
}
```

### `models/model.json` — 模型清单
- `current_model`:启动时默认激活的模型 id
- `models[]`:每个条目包含 `id / name / vendor / apiKey / url / maxInputTokens / maxOutputTokens / supportsToolCall`
- `availableModels[]`:展示给前端下拉框的 id 列表(可隐藏某些模型)

> ⚠️ **生产环境务必**把真实 key 放到环境变量或外部 secret 管理中,
> 不要把 `model.json` 提交到公开仓库。

### `.gitignore`
已忽略 `.venv / __pycache__ / .env / log/*.log / .vscode / .codebuddy / .trae` 等。

---

## 🔌 HTTP API 概览

| 分组 | 方法 | 路径 | 说明 |
| --- | --- | --- | --- |
| 页面 | GET  | `/`                                 | 静态首页 |
| 模型 | GET  | `/api/models`                       | 当前激活 + 模型列表 |
| 模型 | POST | `/api/model`                        | 切换模型 `{model}` |
| 文件 | POST | `/api/file/create`                  | 新建文件 `{path,name,content}` |
| 文件 | GET  | `/api/file/read`                    | 读取 `?path=` |
| 文件 | POST | `/api/file/save`                    | 覆写 `{path,content}` |
| 文件 | POST | `/api/file/rename`                  | 重命名 `{path,new_name}` |
| 文件 | POST | `/api/file/duplicate`               | 同目录复制 `{path}` |
| 文件 | POST | `/api/file/move`                    | 移动/改名 `{path,to_dir,new_name?}` |
| 文件 | POST | `/api/file/delete`                  | 删除文件 `{path}` |
| 目录 | GET  | `/api/folder`                       | 列目录 `?path=`(UA 自适应) |
| 目录 | POST | `/api/folder/create`                | 新建目录 `{path,name}` |
| 搜索 | GET  | `/api/search`                       | `?q=&path=` |
| Git  | GET  | `/api/git/status`                   | 仓库状态 |
| Git  | GET  | `/api/git/diff`                     | unified diff |
| Git  | POST | `/api/git/stage`                    | `git add` |
| Git  | POST | `/api/git/unstage`                  | `git reset` |
| Git  | POST | `/api/git/discard`                  | 丢弃工作区改动 |
| Git  | POST | `/api/git/commit`                   | `git commit -m` |
| Git  | POST | `/api/git/push`                     | `git push` |
| SSH  | POST | `/api/ssh/connect`                  | 新建会话 `{host,port,user,password?\|key_path?}` |
| SSH  | GET  | `/api/ssh/sessions`                 | 列出活动会话 |
| SSH  | POST | `/api/ssh/disconnect`               | 关闭 `{sid}` |
| SSH  | GET  | `/api/ssh/list`                     | 列远端目录 `?sid=&path=` |
| SSH  | POST | `/api/ssh/exec`                     | 远端执行 `{sid,command,cwd?,timeout?}` |
| 终端 | POST | `/api/terminal/run`                 | 本地/SSH 执行 `{command,cwd?,timeout?,ssh_sid?}` |
| Chat | POST | `/api/chat`                         | 单轮 chat(阻塞) |
| Chat | POST | `/api/chat/stream`                  | **SSE 流式** chat |
| Chat | GET  | `/api/chat`                         | 拉取当前 history + agent state |
| Chat | POST | `/api/chat/clear`                   | 清空历史 |
| Agent| GET  | `/api/agent/state`                  | 读 plan_model / auto / pending |
| Agent| POST | `/api/agent/state`                  | 改 `{plan_model?,auto?}` |
| Agent| POST | `/api/agent/pending/confirm`        | 确认 pending 卡片 |
| Agent| POST | `/api/agent/pending/reject`         | 拒绝 pending |
| Diff | GET  | `/api/diffs`                        | 列出所有待审 diff |
| Diff | GET  | `/api/diff`                         | 单文件 unified diff |
| Diff | GET  | `/api/diff/baseline`                | 改动前原文(用于高亮基线) |
| Diff | POST | `/api/diff/clear`                   | 清空 diff(可指定 path) |
| Diff | POST | `/api/diff/revert`                  | 撤销单个文件改动 |
| Diff | POST | `/api/diff/apply`                   | 应用 patch 到磁盘 |
| Diff | POST | `/api/diff/discard`                 | 丢弃 patch |
| 配置 | GET  | `/api/config`                       | 读全局开关 |

---

## 🛡️ 安全与 Agent 规范

完整规范见 `AGENTS.md`,核心要点:

1. **Plan First**:任何 `create_file / edit_file / remove_file` 之前,必须先调用 `plan` 工具,
   携带 `intent / direction / basis / affected_files / steps / risk` 六字段。
2. **不 sudo**:禁止执行 `sudo` 命令。
3. **不臆测**:执行指令前必须验证目标路径/文件名真实存在。
4. **删文件需明确许可**:未经用户允许,不得以任何方式删除文件。
5. **IO 同步**:文件读写必须保证完整性与一致性。
6. **代码风格**:简洁、风格一致、行末不加注释、禁止复杂注释。
7. **依赖最小化**:不引入未经允许的第三方库。
8. **回复格式**:`[修改计划] / [修改依据] / [回滚计划] / [简明总结] / [下一步计划]`。

**前端 diff 卡片** 是这套规范在 UI 上的落地:Agent 改文件前先存基线,
改完后弹卡片让你点 *应用 / 回滚 / 丢弃*,从机制上避免 AI 误改覆盖你的源码。

---

## 📝 日志

启动脚本会把 stdout/stderr 分别重定向到:
- `log/server.log`
- `log/server.err.log`

首次启动会尝试把历史上落在根目录的同名旧日志迁移到 `log/`。

---

## 🧪 开发与测试

```bash
# 在项目根目录、.venv 已激活的情况下
python test/test.py
```

> 测试脚本目前覆盖 `explorer / terminal / git / diff / agent state` 等核心路径,
> 跑全量约几秒。如需新增用例,请保持函数级粒度并复用 `explorer` 的 `tmp_dir` fixture。

调试时建议加 `--debug` 让 Flask 自带热重载:
```bash
python main.py --port 9191 --debug
```

---

## 🗺️ Roadmap(占位)

- [ ] Web 端 Monaco Editor 集成 diff 红绿高亮
- [ ] 多 Agent 协作 / Sub-Agent
- [ ] 持久化会话(目前 history 仅内存)
- [ ] 插件化工具注册

---

## 📄 License

TODO — 待补充。

## 👤 Author

TODO — 待补充。