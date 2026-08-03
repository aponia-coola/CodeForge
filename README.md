# CodeForge

[English](README_ENG.md)

> 一款运行在 **Termux / Linux / macOS / Windows** 上的轻量级 **AI Agent IDE**。
> 基于 Flask + OpenAI 兼容协议,内置 **plan / act / answer** 三阶段工作流,
> 让模型在改动你的代码之前,先把方案摆到桌面上等你点头。

---

## ✨ 核心亮点

| 亮点 | 做了什么 |
| --- | --- |
| **Plan-before-Act 工作流** | 模型在动任何文件之前,必须先调 `plan` 工具提交结构化方案(目标 / 方向 / 依据 / 影响文件 / 步骤 / 风险等级),loop 在此处停下等用户审阅。不是 prompt 里的"建议",是 loop 级别的硬约束 |
| **集中式审批门** | 7 个工具的审批策略集中在 `dispatch()` 里统一执行,新增工具自动继承,不存在"忘了抄一份审批检查"的漏洞。`run_command` 无视 auto 开关,每次都必须逐条确认 |
| **一次性作用域授权** | 确认"删 a.txt"不会顺带放行"删 b.txt"——授权记录精确匹配 action + 关键参数(`file_path` / `command`+`cwd`),消费一次即失效,绝不打开全局开关 |
| **SHA256 防覆盖** | `edit_file` 的 patch 只存在内存里;用户点「应用」时,先把磁盘内容的 sha256 与生成 patch 时的基线对比,不一致直接返回 `409`——你在编辑器里的手动保存不会被 agent 静默盖掉 |
| **自我修改闭环阻断** | `prompt.json` / `model.json` / `.config.json` / `sandbox.py` 四个治理文件即使在工作区内也禁止 agent 改写,堵死"agent 改写自己的系统提示词,对之后所有会话永久生效"这条路径 |
| **DNS Rebinding 防御** | 三层认证:Host 白名单(只认回环名 / IP 字面量 / 显式配置)→ Origin 同源 → `X-CodeForge-Token`(恒定时间比较)。攻击者网页把域名重绑定到 127.0.0.1 骗过浏览器同源策略,但 Host 头会暴露域名,在这一层被拒 |
| **三层 key 解析 + 防泄漏** | API key 优先级:环境变量 → 未跟踪的 `model.local.json` → 跟踪的 `model.json`。系统从不回写 `model.json`。API 响应序列化后逐条比对进程内所有 key(整串 + 12 字符滑窗),命中就换成 `500 key_leak` 而不是把 key 发出去 |
| **按标签页会话隔离** | `X-CodeForge-Session` 按 sid 隔离 history / 审批 / diff 池,两个标签页各带各的 sid 互不干扰。RLock 保护,单用户默认全部落到 `default` 会话 |
| **热重载不停服** | 模型清单、系统提示词、全局开关全部支持文件监听热重载,原子写(`os.replace`)+ 自触发抑制,改完不用重启 |
| **SSE 流式 + 重试退避** | 边想边推,前端实时显示「第 N 轮 / 调用工具 X / 工具返回」。模型请求失败按指数退避重试,确定性异常(权限/路径错误)不重试 |

---

## 📁 目录结构

```
codeforge/
├── main.py                 # Flask 入口 + 全部 HTTP API(46 个路由)
├── auth.py                 # Host / Origin / token 三道关卡
├── sandbox.py              # 路径沙箱与治理文件保护
├── ssh.py                  # SSH 会话管理器(asyncssh)
├── start.sh                # Linux / macOS / Termux 一键启动
├── start.ps1               # Windows PowerShell 一键启动
├── requirements.txt        # 依赖清单(带上界)
├── pyproject.toml          # 项目元数据 + pytest 配置
├── .config.json            # 全局开关(flow / max_round / workspace_roots)
├── AGENTS.md               # AI Agent 安全/代码/回复规范
├── agent/                  # Agent 核心
│   ├── loop.py             #   主循环(run / run_stream)
│   ├── session.py          #   按 sid 隔离的会话状态(history / 审批 / diff)
│   ├── state.py            #   兼容层,委托给 session
│   ├── tool.py             #   工具注册、审批门、dispatch
│   ├── diff.py             #   基线快照、pending patch、apply / revert
│   └── prompt.json         #   系统提示词 + 工具规则
├── explorer/               # 本地文件系统
│   ├── file.py             #   增删改查 + 编码/行尾探测 + 原子写
│   └── search.py           #   全局子串搜索
├── git/engine.py           # Git 子进程封装
├── models/                 # 模型管理
│   ├── model.json          #   模型清单 + 当前激活 + apiKey
│   └── request.py          #   OpenAI 兼容协议调用 + 热重载
├── terminal/engine.py      # 本地 shell 执行器
├── static/                 # Web 前端(index.html / app.js / style.css)
├── tests/                  # pytest 套件
└── log/                    # 运行日志(自动生成)
```

---

## 🚀 快速开始

### 环境要求
- **Python 3.10+**(开发与测试在 3.11 上进行)
- 可访问公网,或自建 OpenAI 兼容协议的 API

### Linux / macOS / Termux

```bash
git clone https://github.com/aponia-coola/codeforge.git
cd codeforge
./start.sh                       # 默认 127.0.0.1:9191,自动开浏览器
./start.sh --port 8080           # 自定义端口
./start.sh --host 0.0.0.0        # 监听所有网卡(会打印风险提示)
./start.sh --no-browser          # 不自动开浏览器
./start.sh --rebuild             # 强制重建 .venv
./start.sh --update              # 强制重装依赖
./start.sh --dev                 # 开发模式(启用 Flask debug)
```

### Windows

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned    # 首次需要

.\start.ps1
.\start.ps1 -Port 8080
.\start.ps1 -ListenHost 0.0.0.0
.\start.ps1 -Dev
.\start.ps1 -Console             # 输出留在控制台,不写 log/
```

两个脚本做的事情一样:

1. 探测/创建 `.venv`
2. 比对 `requirements.txt` 的 sha256 与 `.venv/.deps_installed`,不一致才重装依赖
3. 检测端口占用,冲突时询问是否继续
4. 生成 `CODEFORGE_TOKEN` 并透传给 `main.py`
5. 打印带 token 的访问地址,后台拉起浏览器,前台保持 Flask 运行
6. stdout / stderr 分别重定向到 `log/server.log` 与 `log/server.err.log`

启动后终端里会打印这样一段,**直接用这个 URL 打开就不用手填 token**:

```
════════════════════════════════════════════════════════════════════
  CodeForge
  访问地址: http://127.0.0.1:9191/#token=Xk3n…
  token   : Xk3n…
  来源    : 本次启动随机生成
  请求头  : X-CodeForge-Token: <token>
            X-CodeForge-Session: <sid>(可选,缺省 default 会话)
════════════════════════════════════════════════════════════════════
```

绑定地址不等于访问地址:`--host 0.0.0.0` 是通配绑定,浏览器仍然要用回环地址打开,
横幅里显示的就是可点的那一个,并且会额外打印一段局域网暴露的风险提示。

---

## 🔐 认证

所有 `/api/*` 接口都要求认证。豁免的只有 `GET /`、`GET /favicon.ico` 和 `/static/*`。

### token 从哪来

- 设了环境变量 `CODEFORGE_TOKEN` 就用它;
- 没设则每次启动随机生成一个(`secrets.token_urlsafe(32)`),打印在启动横幅里。
- 用启动脚本时,token 由脚本生成后 export 给 `main.py`,所以脚本打印的 URL 和后端横幅里的一定是同一个。

### 怎么带

| 方式 | 用法 |
| --- | --- |
| 浏览器 | 打开 `http://<host>:<port>/#token=<token>`,前端读到后存进 `sessionStorage` 并把 hash 清掉;没有 token 时会弹框让你手填 |
| 命令行 | 每个请求加请求头 `X-CodeForge-Token: <token>` |

```bash
curl -H "X-CodeForge-Token: $CODEFORGE_TOKEN" http://127.0.0.1:9191/api/config
```

漏带或带错一律返回 `401 {"ok":false,"error":"unauthorized"}`(恒定时间比较,不泄露前缀)。

### 会话头

`X-CodeForge-Session: <sid>` 可选,缺省落到 `default` 会话。
history、`plan_model`、`auto`、pending、diff 池全部挂在会话上,所以两个标签页各带各的 sid 就不会互相踩。
sid 只接受 `[A-Za-z0-9-_]` 且不超过 64 字符,非法值按缺省处理。

### 另外两道关卡

| 关卡 | 规则 | 拒绝时 |
| --- | --- | --- |
| Host 白名单 | 只接受回环名(`localhost` 等)、IP 字面量、`CODEFORGE_ALLOWED_HOSTS` 里显式列出的名字;端口须与服务端口一致 | `403 forbidden_host` |
| Origin 同源 | 没有 Origin 视为同源;有 Origin 时必须与 Host 完全一致 | `403 forbidden_origin` |

Host 白名单挡的是 **DNS rebinding**:攻击者的网页可以把自己的域名重绑定到 `127.0.0.1` 骗过浏览器同源策略,
但 Host 头会带上那个域名,在这一层被拒。IP 字面量无法被重绑定,所以放行,局域网按 IP 访问不受影响。


| 键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `flow` | bool | `false` | 是否默认走 SSE 流式输出。`"true"` / `"1"` / `"yes"` / `"on"` 这类字符串也认 |
| `max_round` | int | `20` | Agent 单次任务的轮数上限,合法范围 1–200,越界回落 20 |
| `workspace_roots` | list[str] | 见下 | 沙箱允许读写的根目录,支持 `~` |

改完不用重启:配置带 2 秒缓存,过期后按 `(mtime, size)` 判断要不要真的重读。

`workspace_roots` 缺省是用户主目录(`/sdcard` 存在时追加,即移动端),
再加一条兜底 —— 安装目录若不在上述根之下会被自动补进去。
所有路径都要落在这些根里,否则统一 `403 {"code":"sandbox"}`。

以下四个文件即使在根内也**禁止 agent 改写**(你在编辑器里手动改不受限):
`agent/prompt.json`、`models/model.json`、`.config.json`、`sandbox.py`。
挡的是「agent 改写自己的系统提示词,对之后所有会话永久生效」这条自我修改闭环。

### `models/model.json` — 模型清单

```json
{
    "current_model": "MiniMax-M3",
    "models": [
        {
            "id": "MiniMax-M3",
            "name": "MiniMax-M3",
            "vendor": "MiniMax",
            "apiKey": "sk-...",
            "url": "https://api.minimaxi.com/v1",
            "maxInputTokens": 1000000,
            "maxOutputTokens": 8192,
            "supportsToolCall": true
        }
    ],
    "availableModels": ["MiniMax-M3"]
}
```

- `current_model`:启动时默认激活的模型 id
- `availableModels`:给前端下拉框看的白名单,省略则显示全部
- `url` 写成 `.../chat/completions` 也可以,内部会自动裁成 `base_url`
- 文件被 watchdog 监听,改完自动热重载,不用重启

### `models/model.local.json` — 本地覆盖层(untracked)

与 `model.json` 结构完全一致,但**不进 git**(已在 `.gitignore` 里),是放 key 的推荐位置。
样例见 `models/model.local.example.json`。

```json
{
    "models": [
        { "id": "MiniMax-M3", "apiKeyEnv": "MINIMAX_API_KEY" },
        { "id": "my-local", "url": "http://127.0.0.1:8000/v1", "apiKey": "sk-..." }
    ]
}
```

- 以 `id` 为键**逐字段**覆盖 `model.json`:本地条目只写 `{"id", "apiKey"}` 即可,`url` 等继续继承
- 新 id 直接追加;顶层 `current_model` / `availableModels` 本地写了就盖住跟踪层
- 文件不存在、为空或坏掉,一切按只有 `model.json` 的现状工作

**key 解析优先级(高到低):**

| 顺序 | 来源 | `keySource` |
| --- | --- | --- |
| 1 | 条目 `apiKeyEnv` 指定的环境变量 | `env` |
| 2 | `model.local.json` 里同 id 条目的 `apiKey` | `local` |
| 3 | `model.json` 里的 `apiKey`(向后兼容) | `tracked` |
| — | 三层都没有,请求会以 401 失败而不是启动失败 | `none` |

切换模型、新增/修改/删除模型只写 `model.local.json`(临时文件 + `os.replace` 原子写),
`model.json` 全程只读,明文 key 不会因为一次 UI 操作被重新写回被跟踪的文件。

### 环境变量

| 变量 | 作用 |
| --- | --- |
| `CODEFORGE_TOKEN` | 固定认证 token,不设则每次启动随机生成 |
| `CODEFORGE_ALLOWED_HOSTS` | 额外放行的 Host,逗号分隔 |
| `CODEFORGE_HOST` / `CODEFORGE_PORT` | `--host` / `--port` 的默认值 |
| `CODEFORGE_DEBUG` | `1` / `true` 时开启 Flask debug,等价于 `--debug` |
| `CODEFORGE_MODEL_TIMEOUT` | 模型请求超时秒数,默认 120 |
| `CODEFORGE_MODEL_RETRIES` | 模型请求重试次数,默认 2 |
| `CODEFORGE_SSH_KNOWN_HOSTS` | known_hosts 路径,默认 `~/.ssh/known_hosts` |
| 条目 `apiKeyEnv` 指定的任意变量名 | 该模型的 API key,优先级高于配置文件里的 `apiKey` |

---

## 🛡️ 审批语义

Agent 的 7 个工具里,`plan` / `list_dir` / `read_file` 是只读的,直接执行;
其余 4 个会改变外部状态,必须过审批门。审批门在 `dispatch()` 里统一执行,
不是抄在每个工具函数体内 —— 「新增工具时漏抄一份」的路径已经不存在了。

| 工具 | 风险 | 审批要求 |
| --- | --- | --- |
| `create_file` | medium | `auto=true` 或针对本次调用的一次性授权 |
| `edit_file` | medium | 同上 |
| `remove_file` | high | 同上 |
| `run_command` | high | **无视 `auto`,每次调用都必须逐条确认** |

### 三种状态

- **每步确认(默认,`auto=false`)**:工具调用被拦下,写一条 pending,
  返回 `{"status":"pending_approval", ...}`,loop 停下等你。
  前端弹确认卡片,点确认即 `POST /api/agent/pending/confirm`。
- **一次性授权**:确认**不会**打开全局开关。它只写一条 approved 记录,
  记着「哪个动作 + 哪些关键参数」,被匹配上的那一次调用消费掉它就失效。
  确认了「删 a.txt」不会顺带放行「删 b.txt」,确认了命令 A 也不会被拿去执行命令 B
  (`run_command` 比对 `command` 和 `cwd`,文件类工具比对 `file_path`)。
- **全程放行(`auto=true`)**:`POST /api/agent/state {"auto":true}` 打开,
  除 `run_command` 外的 mutating 工具不再逐条询问。**只在你完全清楚代价时用。**

`plan_model` 默认 `true`:模型在动任何文件之前必须先调 `plan` 提交结构化方案
(intent / direction / basis / affected_files / steps / risk),loop 会在这里停下等你看。

### 改动怎么落盘

| 工具 | 获批之后 |
| --- | --- |
| `create_file` | **立即写盘**,同时存一份 baseline(为空表示文件原本不存在) |
| `remove_file` | **立即删除**,删之前把原文存进 baseline |
| `edit_file` | **不写盘**,patch 存在会话的内存 diff 池里,前端渲染 unified diff |

对 `edit_file` 产生的 pending patch,有三个出口:

- `POST /api/diff/apply` —— 写入磁盘。**写之前把磁盘内容的 sha256 和生成 patch 时的基线对比,
  不一致返回 `409`**,也就是你在编辑器里的手动保存不会被 agent 的 patch 静默盖掉。
- `POST /api/diff/discard` —— 丢弃 patch,磁盘本来就没动过。
- `POST /api/diff/revert` —— 把文件恢复到 baseline:agent 新建的文件被删掉,被删的文件被写回。
  这条是给 `create_file` / `remove_file` 这类已经落盘的操作准备的。

---

## 🔌 HTTP API

46 个路由。所有 `/api/*` 都需要 `X-CodeForge-Token`,可选 `X-CodeForge-Session`。
请求体一律 JSON,上限 8 MB。**标 `必填` 的参数缺失时按「返回码」列直接失败,不会退回默认值。**

### 页面

| 方法 | 路径 | 参数 | 说明 |
| --- | --- | --- | --- |
| GET | `/` | — | 单页应用入口,**唯一不需要 token 的页面** |

### 模型

| 方法 | 路径 | 参数 | 返回码 |
| --- | --- | --- | --- |
| GET | `/api/models` | — | 200 `{current, models, base_url}` |
| POST | `/api/model` | `{model}` **必填** | 200 / 400 缺少 model / 404 未知模型 |

### 模型管理

写的是 `models/model.local.json`,`model.json` 只读。
**这四个端点的响应绝不含明文 key**:序列化后逐条比对进程内所有 key(整串 + 12 字符滑窗),
命中就换成 `500 key_leak`,而不是把 key 发出去。

| 方法 | 路径 | 参数 | 返回码 |
| --- | --- | --- | --- |
| GET | `/api/models/admin` | — | 200 `{ok, models: [...]}` / 500 `key_leak` |
| POST | `/api/models/upsert` | 见下表,`id` **必填** | 200 `{ok, id, models}` / 400 `invalid_spec`、`upsert_failed` |
| POST | `/api/models/delete` | `{id}` **必填**,只能删 `origin=="local"` 的条目 | 200 `{ok, id, models}` / 400 `invalid_spec`、`delete_failed` |
| POST | `/api/models/test` | `{id}` **必填** | 200 `{ok, id, latency_ms}` / 400 `invalid_spec` / 502 `test_failed` / 504 `timeout`(15 秒) |

`/api/models/admin` 每项的字段:

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` / `name` / `vendor` / `url` | str | 条目基本信息 |
| `maxInputTokens` / `maxOutputTokens` | int \| null | 未配置时为 `null` |
| `supportsToolCall` | bool | 是否支持工具调用 |
| `hasKey` | bool | 三层解析后有没有拿到 key |
| `keySource` | `env` \| `local` \| `tracked` \| `none` | key 来自哪一层 |
| `origin` | `local` \| `tracked` | 条目由哪一层提供 |
| `editable` | bool | 等价于 `origin == "local"`,只有它能被删 |

`/api/models/upsert` 的 body 字段与校验:

| 字段 | 必填 | 校验 |
| --- | --- | --- |
| `id` | 是 | 非空、≤128 字符、不含路径分隔符、不以 `.` 开头、无控制字符 |
| `name` / `vendor` | 否 | 字符串,≤128 字符 |
| `url` | 否 | 字符串,≤512 字符,必须是 `http://` / `https://` 且带主机名;新增条目时若跟踪层也没有 url 则报错 |
| `apiKey` | 否 | 字符串 ≤4096 字符。**空串 = 保持原值**,**`null` = 清除本地 key**,省略 = 不改动 |
| `apiKeyEnv` | 否 | 环境变量名,≤128 字符,只能 `[A-Za-z_][A-Za-z0-9_]*` |
| `maxInputTokens` | 否 | 整数,1..20000000 |
| `maxOutputTokens` | 否 | 整数,1..1000000 |
| `supportsToolCall` | 否 | bool(`"true"` / `"1"` / `"on"` 这类字符串也认) |

### 配置

| 方法 | 路径 | 参数 | 返回码 |
| --- | --- | --- | --- |
| GET | `/api/config` | — | 200 `{flow, max_round, path}` |
| POST | `/api/config` | `{flow?: bool, max_round?: int}`,至少给一个 | 200 / 400 无可改字段、max_round 非整数或越界 |

### 目录与搜索

| 方法 | 路径 | 参数 | 返回码 |
| --- | --- | --- | --- |
| GET | `/api/folder` | `?path=`(可选,缺省用服务端探测的起始目录) | 200 / 400 不是目录 / 403 沙箱 / 404 不存在 |
| GET | `/api/search` | `?q=`(可选,空则返回空结果集)`&path=`(可选) | 200 / 404 根不存在 |
| POST | `/api/folder/create` | `{path, name}` **两个都必填** | 200 / 400 / 409 已存在 |

> 起始目录只看服务端文件系统上 `/sdcard` 是否真的存在,不看 User-Agent。

### 文件

| 方法 | 路径 | 参数 | 返回码 |
| --- | --- | --- | --- |
| GET | `/api/file/read` | `?path=` **必填**,绝对路径 | 200 / 400 缺 path / 404 / 413 超 2 MB / 415 非文本 |
| POST | `/api/file/create` | `{path, name}` **必填**,`{content?}` 默认 `""` | 200 / 400 / 409 已存在 |
| POST | `/api/file/save` | `{path, content}` **必填**,`{encoding?='utf-8', newline?='\n'}` | 200 / 400 未知编码或非法 newline / 404 |
| POST | `/api/file/rename` | `{path, new_name}` **两个都必填** | 200 / 400 / 404 / 409 目标已存在 |
| POST | `/api/file/duplicate` | `{path}` **必填** | 200 / 400 |
| POST | `/api/file/move` | `{path, to_dir}` **必填**,`{new_name?}` 缺省沿用原名 | 200 / 400 / 404 / 409 |
| POST | `/api/file/delete` | `{path}` **必填**,仅限文件 | 200 / 400 / 404 |

> 读文件按 `utf-8 → utf-8-sig → 本机 locale → latin-1` 探测编码,行尾探测结果放在 `newline` 字段,
> `content` 统一成 LF 交给编辑器。保存时把 `read` 返回的 `encoding` / `newline` 原样传回来,
> GBK 的 `.bat`、CRLF 的文件才不会被改写。

### Chat

| 方法 | 路径 | 参数 | 说明 |
| --- | --- | --- | --- |
| GET | `/api/chat` | — | 当前会话 history + state |
| POST | `/api/chat` | `{message?, history?, max_rounds?, plan_model?, cwd?}` 全部可选 | 阻塞式跑一轮 loop |
| POST | `/api/chat/stream` | 同上,另加 `{flow?}` | **SSE**,事件 `round / tool_call / cancelled / error / done` |
| POST | `/api/chat/clear` | — | 清空 history + pending + 一次性授权 |
| POST | `/api/chat/stop` | — | 置取消标志,loop 在下一个事件边界停 |

> `history` 传了就以前端为准,不传用会话里存的;`max_rounds` 不传取 `.config.json` 的 `max_round`。
> `stop` 打断不了**正在执行中**的那一次工具调用,它会先跑完。
> 返回里的 `stopped` ∈ `answer` / `pending` / `max_rounds` / `error` / `cancelled`。

### Agent 状态与审批

| 方法 | 路径 | 参数 | 返回码 |
| --- | --- | --- | --- |
| GET | `/api/agent/state` | — | 200 `{plan_model, auto, pending, sid}` |
| POST | `/api/agent/state` | `{plan_model?: bool, auto?: bool}` | 200 |
| POST | `/api/agent/pending/confirm` | `{resume?=true, history?, cwd?, max_rounds?, plan_model?}` | 200 / **400 当前没有待确认的操作** |
| POST | `/api/agent/pending/reject` | — | 200 |

> `confirm` 缺省顺带把 loop 续跑下去;`resume:false` 表示只授权不续跑,由前端自己再调 `/api/chat/stream`。

### Diff

| 方法 | 路径 | 参数 | 返回码 |
| --- | --- | --- | --- |
| GET | `/api/diffs` | — | 200 待审文件列表 |
| GET | `/api/diff` | `?path=` **必填** | 200 / 400 缺 path / 404 无 diff |
| GET | `/api/diff/baseline` | `?path=` **必填** | 200 / 400 缺 path / 404 无基线 |
| POST | `/api/diff/clear` | `{path?}` 省略表示清空全部 | 200 |
| POST | `/api/diff/revert` | `{path}` **必填** | 200 / 400 |
| POST | `/api/diff/apply` | `{path}` **必填** | 200 / 400 / **409 文件已被外部修改** |
| POST | `/api/diff/discard` | `{path}` **必填** | 200 / 400 |

### Git

| 方法 | 路径 | 参数 | 返回码 |
| --- | --- | --- | --- |
| GET | `/api/git/status` | `?path=`(可选,缺省起始目录) | 200(非仓库返回 `is_git:false`)/ 400 路径不存在 |
| GET | `/api/git/diff` | `?path=` **必填** `&cwd=` **必填** `&staged=1`(可选) | 200 / **400 缺少 path/cwd** |
| POST | `/api/git/stage` | `{cwd, path}` **两个都必填**,`path:"-A"` 表示 `git add -A` | 200 / 400 |
| POST | `/api/git/unstage` | `{cwd}` **必填**;`{path}` 省略或 `"-A"` 表示全部撤出暂存 | 200 / 400 缺 cwd |
| POST | `/api/git/discard` | `{cwd, path}` **两个都必填** | 200 / 400 |
| POST | `/api/git/commit` | `{cwd, message}` **两个都必填** | 200 / 400 |
| POST | `/api/git/push` | `{cwd}` **必填**,`{remote?, branch?}` 缺省用 upstream | 200 / 400 缺 cwd |

> `cwd` 是仓库路径,`path` 是仓库内的文件路径 —— 少给任何一个都是 400,不会退回当前目录。

### SSH

| 方法 | 路径 | 参数 | 返回码 |
| --- | --- | --- | --- |
| POST | `/api/ssh/connect` | `{host, user}` **必填**,`{port?=22}`,`password` 与 `key_path` **至少给一个** | 200 / 400 / **409 主机指纹待确认** / 500 |
| POST | `/api/ssh/host_key/confirm` | `{token}` **必填**,来自 409 响应 | 200 / 400 |
| POST | `/api/ssh/host_key/cancel` | `{token}` **必填** | 200 |
| GET | `/api/ssh/sessions` | — | 200 |
| POST | `/api/ssh/disconnect` | `{sid}` **必填** | 200 / 400 / 500 |
| GET | `/api/ssh/list` | `?sid=` **必填**,`&path=` 缺省 `"."` | 200 / **404 session not found** |
| POST | `/api/ssh/exec` | `{sid, command}` **必填**,`{cwd?, timeout?=30}` | 200 / 400 / 404 |

> 首次连一台主机会返回 `409` + 指纹,核对无误后调 `host_key/confirm` 写进 known_hosts(TOFU),
> **然后要重新发起一次 `connect`** —— 票据里不缓存密码。
> 指纹与已有记录不符时不发 token,必须人工处理 known_hosts。

### 终端

| 方法 | 路径 | 参数 | 返回码 |
| --- | --- | --- | --- |
| POST | `/api/terminal/run` | `{command}` **必填**,`{cwd?, timeout?=30, ssh_sid?}` | 200 / 400 缺 command / 404 ssh 会话不存在 |

> 传了 `ssh_sid` 就走远端执行,此时 `cwd` 属于远端文件系统,不过本地沙箱;
> 本地执行时 `cwd` 先过 `sandbox.resolve()`。超时后按进程组终止,孙进程一并杀掉。

### 错误格式

`/api/*` 下的所有错误都收敛成 JSON:

```json
{"ok": false, "error": "……", "code": "sandbox | unauthorized | forbidden_host | internal | ……"}
```

---

## 🧪 开发与测试

```bash
.venv/Scripts/python.exe -m pytest          # Windows
.venv/bin/python -m pytest                  # Linux / macOS
```

`tests/` 是 pytest 套件,覆盖 auth / sandbox / diff / tools / loop / explorer / terminal / http / session。
三条硬约束:

- 不发起真实网络或 LLM 调用(`no_network` 是 autouse 夹具,漏网的调用会直接抛异常)
- 不写进仓库(`tmp_workspace` 把沙箱根整个换成临时目录)
- 用例之间重置全局状态(会话表、diff 池、沙箱根)

需要真实模型的用例集中在 `tests/test_live_agent.py`,打了 `live` 标记,
默认被 `pyproject.toml` 里的 `-m "not live"` 排除。**它们会产生真实的付费调用**,要跑得显式指定:

```bash
.venv/Scripts/python.exe -m pytest -m live
```

调试服务端时加 `--debug` 开 Flask 调试器:

```bash
python main.py --port 9191 --debug
```

**debug 默认是关的。**`--debug`(或 `CODEFORGE_DEBUG=1`、`start.sh --dev`、`start.ps1 -Dev`)才会打开。
即使开了 debug,reloader 也始终关闭(`use_reloader=False`)—— 否则 agent 编辑项目内文件会触发重启,
把会话历史、diff、pending 全部清空,SSE 流当场断掉。

---

## 🗺️ Roadmap

- [ ] `read_file` 分段读 + 历史压缩,控制长会话的 token 增长
- [ ] 资源管理器轮询在页面隐藏时暂停
- [ ] 多 Agent 协作 / Sub-Agent
- [ ] 持久化会话(目前 history 仅内存)

---

