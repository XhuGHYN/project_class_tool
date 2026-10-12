# 教学版 Python Agent：阶段一设计文档

## 1. 项目目标

实现一个可运行、便于教学的 Python 命令行 Agent。用户输入问题后，Agent 使用 OpenAI 兼容的 Chat Completions 非流式接口判断是否调用工具；Python 执行工具并将结果按 Function Calling 消息格式回传模型，直到模型给出最终回答。对话及工具调用过程写入 JSON Session，支持后续恢复。

阶段一以清晰的模块边界、可离线测试和可理解的工具循环为目标，不追求通用 Agent 框架能力。

## 2. 本阶段实现范围

- OpenAI 兼容 Chat Completions 非流式调用及 Function Calling。
- Agent Loop 支持直接回答、一次工具调用、多工具调用及多轮工具调用。
- 六个工具：`read_file`、`write_file`、`list_dir`、`change_dir`、`shell`、`search_files`。
- 从 `config.yaml` 读取普通配置；从 `.env` 读取 `AGENT_API_KEY`。
- CLI 交互及 `--message`、`--resume`、`--session` 参数。
- JSON Session 的新建、保存、按 ID 加载及恢复最近 Session。
- 离线 Mock 测试，不需要真实 API Key 或网络。

## 3. 暂不实现的功能

- Skill 系统、`MEMORY.md` 长期记忆、向量数据库、多智能体。
- 流式模型调用、流式输出。
- 任意 shell 执行、命令管道、重定向、shell 脚本。
- GUI、Web 服务、数据库 Session 后端。
- CLI 斜杠命令、输入历史和自动补全。

## 4. 默认设计决策

- 使用 OpenAI Python SDK 的 Chat Completions 接口，`tools` 使用 `type: function` 格式；模型名和服务地址可配置。
- Session 采用每个 Session 一个 JSON 文件，保存完整的 OpenAI 格式消息历史；Session 元信息采用独立 JSON 索引。
- 文件类工具的根目录是程序启动时的当前工作目录，整个 Session 生命周期内固定。工具的当前目录是 `ToolContext` 中可变的工作目录。
- 所有工具路径必须解析到启动根目录内。`change_dir` 仅能在此根目录内切换。
- `shell` 仅允许无副作用的目录查看和文件搜索命令；通过参数数组启动子进程，不经 shell 解释器执行。
- 配置读取失败、模型调用失败等系统错误清晰报告；工具错误转换为工具结果消息并交由模型处理，不使整个 Agent Loop 崩溃。
- 默认 Agent Loop 上限为 10 次模型响应迭代；单次迭代可以包含多个工具调用，按模型返回的顺序执行。
- 文件内容使用 UTF-8；工具不支持任意编码参数。
- 除明确列出的 CLI 参数外，不增加隐含命令或配置覆盖行为。

## 5. 项目目录结构

```text
project_class_tool/
├── DESIGN.md                 # 既有详细设计文档
├── DESIGN_1.md               # 本阶段设计文档
├── config.yaml               # 本地普通配置，不提交敏感信息
├── config.example.yaml       # 普通配置示例
├── .env                      # 本地密钥文件，不提交
├── .env.example              # 环境变量名称示例，不含真实密钥
├── .sessions/
│   ├── index.json
│   └── <session-id>.json
├── agent/
│   ├── __init__.py
│   ├── config.py
│   ├── llm.py
│   ├── tools.py
│   ├── loop.py
│   ├── session.py
│   └── cli.py
├── tests/
│   ├── conftest.py
│   ├── test_config.py
│   ├── test_llm.py
│   ├── test_tools.py
│   ├── test_loop.py
│   ├── test_session.py
│   └── test_cli.py
└── main.py
```

实现以此目标结构为准。`agent/condig.py`（若仍存在）是拼写不一致的旧文件名，不作为新模块接口；迁移时统一为 `agent/config.py`，不得留下两份配置实现。

## 6. config.py 职责

- 定义配置数据结构及代码默认值。
- 解析 `config.yaml`，校验字段类型和必需项。
- 加载 `.env` 中的 `AGENT_API_KEY`，并读取进程环境变量覆盖项。
- 解析绝对启动工作目录、Session 目录、模型端点、模型名、timeout 和最大迭代次数。
- 不在配置对象、日志或 CLI 输出中明文展示 API Key。

配置至少包含 `llm.base_url`、`llm.model`、`llm.timeout`、`agent.max_iterations`、`session.dir`、`tools.shell_timeout`、`tools.search_max_results`。`base_url`、`model` 默认值只用于示例/配置，调用前必须确认配置有效。

## 7. llm.py 职责

- 封装 OpenAI 兼容 Chat Completions 非流式请求。
- 输入完整 `messages` 和六个工具的 Function Calling schema。
- 返回可追加到历史记录的 assistant 消息字典，完整保留 `content`、`tool_calls`、`tool_call_id` 对应所需字段。
- 把 SDK 响应对象转换成普通 JSON 可序列化字典。
- 对网络/API 异常抛出不包含密钥的可读错误；不负责执行工具或管理 Session。

## 8. tools.py 职责

- 定义 `ToolContext`、工具描述/schema 注册表及工具分发器。
- 实现六个工具和路径安全校验。
- 将模型提供的 JSON arguments 解析为对象，校验工具名及参数。
- 工具成功和失败均返回字符串结果；不得把异常堆栈或密钥回传模型。
- 对 shell 命令执行白名单与参数校验。

## 9. loop.py 职责

- 接收用户文本，将 user 消息追加至 Session。
- 调用 LLM，保存 assistant 完整响应；若有 `tool_calls`，逐个执行并追加匹配的 tool 消息。
- 工具结果回传模型，循环直到得到不含工具调用的最终 assistant 回答或达到迭代上限。
- 维护一次运行期间的 `ToolContext`，使 `change_dir` 状态供后续工具使用。
- 返回最终回答；达到上限时给出明确提示并保留完整历史，不伪造模型答复。

## 10. session.py 职责

- 定义 Session 数据对象和 `SessionManager`。
- 新建、按 ID 加载、保存、列出及获取最近更新的 Session。
- 将对话消息和必要元信息序列化为 JSON；每轮完成后自动保存。
- `--session <id>` 指向不存在的 ID 时创建该 ID 的新 Session；ID 必须符合安全格式，避免路径穿越。

## 11. cli.py 职责

- 实现交互式输入/输出循环，调用 Agent Loop 并显示最终回答与错误。
- 实现单条消息模式的执行入口（CLI 参数解析仍集中于 `main.py`）。
- 不读取配置文件、不直接请求模型、不实现工具逻辑。

## 12. main.py 职责

- 解析 `--message`、`--resume`、`--session`；`--resume` 与 `--session` 同时指定时报参数错误。
- 加载配置，建立固定的启动目录边界，初始化 SessionManager、Session、LLM、工具上下文、Agent Loop 和 CLI。
- `--message` 执行一条消息、保存后退出；无 `--message` 时进入交互模式。
- 未指定 `--resume` 或 `--session` 时新建 Session；`--resume` 恢复最近更新的 Session。没有可恢复 Session 时报告清晰错误。

## 13. Function Calling 消息结构

所有历史消息使用 Chat Completions 的标准字典结构。assistant 工具调用消息必须完整保留模型给出的调用 ID、函数名和 JSON 字符串 arguments；每个结果都以 `role: "tool"` 记录，并用相同的 `tool_call_id` 配对。

```json
[
  {"role": "system", "content": "你是一个可以使用受限工作区工具的助手。"},
  {"role": "user", "content": "读取 notes.txt"},
  {
    "role": "assistant",
    "content": null,
    "tool_calls": [{
      "id": "call_example_1",
      "type": "function",
      "function": {"name": "read_file", "arguments": "{\"path\":\"notes.txt\"}"}
    }]
  },
  {"role": "tool", "tool_call_id": "call_example_1", "content": "文件内容"},
  {"role": "assistant", "content": "notes.txt 的内容是……"}
]
```

若一条 assistant 消息含多个 `tool_calls`，每个调用均须产生且只产生一条相同 ID 的 tool 结果消息，之后才能再次请求模型。无工具调用的 assistant 最终消息也必须保存。

## 14. 六个工具的 Schema

schema 采用 OpenAI `{"type":"function","function":{...}}` 包装。所有字段均严格校验；额外字段拒绝。以下为函数参数对象。

### 14.1 `read_file`

```json
{"type":"object","properties":{"path":{"type":"string","description":"相对于当前工作目录或根目录内的绝对文件路径"}},"required":["path"],"additionalProperties":false}
```

读取 UTF-8 文本文件。

### 14.2 `write_file`

```json
{"type":"object","properties":{"path":{"type":"string"},"content":{"type":"string"},"mode":{"type":"string","enum":["overwrite","append"]}},"required":["path","content","mode"],"additionalProperties":false}
```

写入或追加 UTF-8 内容。为避免模型省略操作方式，`mode` 必填，不提供默认覆盖行为。父目录不存在时不自动创建，返回工具错误。

### 14.3 `list_dir`

```json
{"type":"object","properties":{"path":{"type":"string","description":"目录路径，省略时为当前工作目录"}},"additionalProperties":false}
```

列出指定目录的一层直接子项，默认不列出隐藏项；输出名称、类型，不递归。

### 14.4 `change_dir`

```json
{"type":"object","properties":{"path":{"type":"string"}},"required":["path"],"additionalProperties":false}
```

将 `ToolContext.current_dir` 切换到已存在且位于根目录范围内的目录。切换失败时保持原目录。

### 14.5 `shell`

```json
{"type":"object","properties":{"command":{"type":"string","description":"仅支持白名单内的只读目录查看或文件搜索命令"},"timeout":{"type":"integer","minimum":1,"maximum":30}},"required":["command"],"additionalProperties":false}
```

命令白名单限于 `pwd`、`ls`、`find`、`rg` 的受限子集；执行器只接受允许的命令及选项，不支持 shell 元字符、管道、重定向、命令串联、变量展开、脚本或其他可写命令。`timeout` 省略时使用配置默认值 10 秒，最大 30 秒。超时后终止子进程并返回超时错误。

### 14.6 `search_files`

```json
{"type":"object","properties":{"pattern":{"type":"string","description":"文件名 glob 模式，例如 **/*.py"},"path":{"type":"string","description":"搜索起始目录，省略时为当前工作目录"},"max_results":{"type":"integer","minimum":1,"maximum":100}},"required":["pattern"],"additionalProperties":false}
```

递归搜索起始目录树中的文件名，不搜索文件内容。结果默认最多 100 条；可请求更小上限但不能超过 100。超限时明确标注结果已截断。忽略隐藏目录和 `.sessions` 目录。

## 15. ToolContext 设计

`ToolContext` 是一次 Agent 运行共享的可变上下文，至少包含：

```text
root_dir: Path       # 程序启动时固定的绝对根目录，不可变
current_dir: Path    # 初始为 root_dir，可由 change_dir 修改
session_id: str      # 当前 Session 标识
config: AgentConfig  # 已解析配置（不得向工具暴露 API Key）
```

每个工具从同一上下文获取根目录和当前目录。路径参数相对 `current_dir` 解析；绝对路径可用但必须仍在 `root_dir` 内。

## 16. change_dir 如何影响后续工具

成功调用 `change_dir` 后只更新 `ToolContext.current_dir`，不调用进程级 `os.chdir`。之后 `read_file`、`write_file`、`list_dir`、`search_files` 和 `shell` 的相对路径/工作目录均以新目录为准。绝对路径仍受根目录约束。Session 保存 `root_dir` 和最近的 `current_dir`；恢复时重新校验路径仍存在且位于根目录下，否则将当前目录重置到根目录并提示。

## 17. shell 的工作目录和 timeout 设计

- 子进程的 `cwd` 始终为 `ToolContext.current_dir`。
- 启动根目录校验不因 `change_dir` 改变；子进程不得通过 `cwd` 越出根目录。
- 默认 timeout 为 10 秒，配置及工具参数的有效范围为 1 至 30 秒。
- 使用参数数组及 `shell=False` 执行经白名单解析的命令，不经系统 shell 解释。
- 捕获 stdout、stderr 和退出码；输出设置上限，超出时截断并标注。非零退出码作为工具结果返回，不伪装为成功。
- 只允许只读查询命令；不开放 `write_file`、删除、复制、移动、权限修改、进程启动或网络访问命令。白名单按平台实现并在文档/帮助中列出实际支持项。

## 18. search_files 的搜索范围和结果上限

- 起始目录省略时为 `current_dir`，否则相对 `current_dir` 解析。
- 搜索范围为起始目录及其后代，且仍受固定 `root_dir` 边界约束。
- 仅按文件名 glob 匹配；默认跳过隐藏目录、`.sessions` 及符号链接目录，避免意外扩展搜索范围。
- 默认最多返回 100 个匹配项，参数可请求 1 至 100 项；达到上限时标注截断。
- 结果使用相对于 `root_dir` 的路径，稳定排序后返回。

## 19. 配置加载优先级

普通配置按以下优先级从低到高合并：代码默认值 → `config.yaml` → 进程环境变量中明确支持的配置项。阶段一只为普通配置支持 `AGENT_BASE_URL`、`AGENT_MODEL`、`AGENT_MAX_ITERATIONS`；未设置时使用 YAML/默认值。

API Key 不属于普通 YAML 配置：只从进程环境变量 `AGENT_API_KEY` 读取；若不存在，则从项目根目录 `.env` 加载。进程环境变量优先于 `.env`。不从 `config.yaml`、命令行参数或 Session 读取 API Key，也不将其写入 Session。

## 20. .env 和 config.yaml 的分工

- `config.yaml`：非敏感运行配置，例如 `llm.base_url`、`llm.model`、timeout、Agent 最大迭代次数、Session 目录、工具限制。
- `.env`：仅存放 `AGENT_API_KEY` 等本地秘密；本阶段只要求读取 `AGENT_API_KEY`。
- `config.example.yaml` 和 `.env.example` 可提交；真实 `.env` 和本地 `config.yaml` 应加入忽略规则。
- 启动时缺少 API Key 时，离线测试仍可运行；实际模型请求前给出不泄漏密钥的配置错误。

## 21. 文件路径安全规则

- `root_dir` 在程序启动时由当前工作目录解析为绝对路径并固定，不接受工具更改。
- 先将相对路径相对 `current_dir` 解析为绝对路径，再规范化并验证其位于 `root_dir` 内；使用路径组件边界判断，不能仅做字符串前缀比较。
- 已存在目标使用解析后的真实路径校验，以阻止符号链接逃逸；新建文件时校验最近的已存在父目录真实路径，并逐层确保目标仍在根目录内。
- 拒绝 `..` 导致越界的路径、越界符号链接、无效路径及目录/文件类型不匹配。
- 所有工具，包括 shell 的 cwd、shell 接收的路径参数、搜索起点，均受相同边界约束。
- 工具不接受任意 Python 表达式或命令执行内容；文件读取/写入错误通过工具结果返回。

## 22. 工具错误处理

- 未知工具、无效 JSON arguments、缺少参数、额外参数、参数类型错误均生成对应 `tool_call_id` 的 `role: "tool"` 错误结果。
- 文件不存在、权限不足、路径越界、编码错误、目录不存在、shell 命令不在白名单、超时、非零退出码均返回简明且不含堆栈的结果。
- 一次响应中的某个工具失败不阻止同批次其他工具执行，也不破坏消息配对；Agent 将错误结果发送回模型，由模型决定如何答复。
- API/网络异常属于模型调用错误：停止当前轮并向 CLI 报错；已生成的消息仍保留，并尽力保存 Session。
- 不打印请求头、API Key、完整秘密环境变量或含 Key 的异常对象。

## 23. Agent Loop 最大迭代次数

默认 `max_iterations` 为 10，可由 `config.yaml` 设置为 1 至 50 的整数。每次向模型发起请求计为一次迭代，包括最终回答请求；一条 assistant 响应可包含多个工具调用。达到上限后停止，不再请求模型，返回明确的“达到工具调用迭代上限”提示并保存当前消息历史。

## 24. Session JSON 数据结构

单个 Session 文件 `.sessions/<session-id>.json`：

```json
{
  "schema_version": 1,
  "id": "20261012-153000-a1b2c3",
  "created_at": "2026-10-12T15:30:00Z",
  "updated_at": "2026-10-12T15:31:00Z",
  "title": "首条用户消息的简短摘要",
  "root_dir": "C:/Users/example/project",
  "current_dir": "C:/Users/example/project/docs",
  "messages": [
    {"role": "system", "content": "..."},
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": null, "tool_calls": []},
    {"role": "tool", "tool_call_id": "call_example_1", "content": "..."},
    {"role": "assistant", "content": "..."}
  ]
}
```

所有消息只包含模型协议需要的字段；Session 不保存 API Key。`.sessions/index.json` 保存 `schema_version` 及 Session 的 `id`、`title`、`created_at`、`updated_at`，以 `updated_at` 排序。最近 Session 指最近成功保存/更新时间最新的一项。恢复时保留历史消息及 tool call 配对关系。

## 25. Mock 测试方案

- 使用 `pytest`、临时目录和 `unittest.mock`/Mock LLM，不依赖网络、API Key 或真实模型。
- `config`：默认值、YAML 解析、配置优先级、缺失/无效配置、密钥不进入普通配置序列化。
- `llm`：Mock SDK 响应，验证 request 参数、非流式响应转消息、tool_calls 保真和异常隐藏秘密。
- `tools`：六工具成功/失败、参数校验、相对路径、越界路径、符号链接逃逸、change_dir 后续影响、白名单拒绝、shell timeout、search 截断。
- `loop`：无工具直接回答、单工具、多工具同批、多轮工具、错误结果继续、tool_call_id 配对、最大迭代次数。
- `session`：新建/保存/加载往返、恢复最近 Session、指定不存在 ID 新建、messages 完整性、密钥不落盘、非法 ID 拒绝。
- `cli/main`：参数互斥、单条消息保存退出、交互模式启动和无最近 Session 时的提示；将 LLM 与文件目录依赖注入 Mock/临时目录。

## 26. 分阶段实现顺序

1. 建立包结构、配置数据结构、`config.py` 与 `config.example.yaml`，覆盖配置和密钥来源测试。
2. 实现 `ToolContext`、路径安全辅助逻辑、六个 schema 与工具执行器，先实现文件工具，再实现白名单 shell 和搜索。
3. 实现 `llm.py` 非流式适配和 SDK Mock 测试。
4. 实现 `session.py` JSON 序列化、索引及恢复流程。
5. 实现 `loop.py` Function Calling 循环、错误回传、迭代上限及端到端 Mock 测试。
6. 实现 `cli.py`、`main.py` 参数解析、交互模式、Session 生命周期。
7. 运行全量离线测试，按最终验收标准检查安全边界、消息历史和文档示例。

## 27. 最终验收标准

- 不配置真实 Key 时，离线测试可完整通过且不发网络请求。
- 配置正确时可通过 OpenAI 兼容 Chat Completions 非流式接口获取回答及 Function Calling。
- 六个工具的 schema 与实现一致；工具调用和结果按 `tool_call_id` 一一配对并写入历史。
- 支持同批多个调用及跨多轮工具调用，且达到迭代上限时有明确结果。
- `change_dir` 影响后续相对路径解析，但任何工具均不能越出启动目录树。
- shell 仅允许文档列出的只读白名单命令，固定使用当前工作目录和有界 timeout，不调用 shell 解释器。
- Session 保存并恢复消息、工具调用、根目录和当前目录；支持新建、指定 ID、最近 Session 恢复。
- `--message` 单条执行后保存退出；`--resume` 与 `--session` 同时使用会明确报参数错误。
- API Key 仅从环境/.env 输入，不写入 YAML、Session、日志或错误信息。
- 全部自动化测试离线可运行；目标目录和配置测试使用隔离临时目录。

## 28. 所有非阻塞问题采用的默认决策

- OpenAI SDK 封装 Chat Completions；只做非流式请求。
- 普通配置可由 YAML 和指定进程环境变量提供；API Key 只由 `AGENT_API_KEY` 环境/.env 提供，进程环境优先。
- Agent Loop 默认最多 10 次模型请求迭代，可配置 1 至 50 次。
- Session 文件存于 `.sessions/`，每个会话单独 JSON，并用 `index.json` 查找最近会话；默认每轮自动保存。
- Session ID 使用时间戳加短随机后缀；指定的有效 ID 不存在时创建该 ID 的 Session。
- 无会话参数时创建新 Session；`--resume` 恢复最近更新者；两者与 `--session` 互斥；无最近 Session 时 `--resume` 报错。
- `--message` 执行一条消息、保存并退出；否则进入交互式 CLI。
- 文件读写 UTF-8；写入模式显式指定 `overwrite` 或 `append`；不自动创建父目录。
- 目录列表只列一层；搜索只按文件名递归，忽略隐藏目录和 `.sessions`，最多返回 100 项。
- Shell 默认 timeout 10 秒、最多 30 秒，仅开放只读目录查看/搜索命令；参数数组执行、禁止 shell 元字符。
- 工具异常变成工具错误消息继续循环；模型/API 异常中止当前轮并尽力保存。
- 测试使用 `pytest` 与 Mock，覆盖模块单测及临时目录上的 Agent 集成测试。
- 不增加流式输出、斜杠命令、输入历史、数据库、多智能体、Skill 或长期记忆功能。
