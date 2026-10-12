# project_class_tool

一个教学版 Python 命令行 Agent，用于学习配置管理、OpenAI 兼容 Chat Completions、Function Calling、工具执行、Agent Loop、Session 持久化和 CLI 交互。

## 功能

- 支持 OpenAI 兼容的 Chat Completions 接口；
- 支持非流式和流式文本输出；
- 支持 Function Calling 和多轮工具调用；
- 提供六个基础工具：
  - `read_file`：读取 UTF-8 文本文件；
  - `write_file`：覆盖或追加文本文件；
  - `list_dir`：列出目录内容；
  - `change_dir`：切换当前工作目录；
  - `shell`：在当前工作目录执行教学用命令；
  - `search_files`：按文件名 glob 和内容关键词搜索文件；
- 保存和恢复 JSON Session；
- 支持交互式 REPL 和单条消息模式；
- 支持 Mock LLM 的离线测试；
- API Key 不写入代码、Session 或日志。

## 环境要求

- Python 3.10 或更高版本；
- 一个 OpenAI 兼容的模型服务及其 API Key；
- Windows、Linux 和 macOS 均可运行。

## 安装

本项目使用 Conda 管理 Python 虚拟环境。在项目根目录执行：

```bash
conda create -n project-class-tool python=3.11 -y
conda activate project-class-tool
python -m pip install -r requirements.txt
```

如果 Conda 尚未初始化当前终端，请先执行：

```bash
conda init
```

然后重新打开终端，再执行环境创建和激活命令。

确认当前使用的是项目 Conda 环境：

```bash
conda env list
python --version
```

日后进入项目时，只需要激活已有环境：

```bash
conda activate project-class-tool
```

## 配置

### 1. 普通配置

复制示例配置：

```bash
copy config.example.yaml config.yaml
```

Linux/macOS：

```bash
cp config.example.yaml config.yaml
```

`config.yaml` 保存普通项目配置，例如：

```yaml
llm:
  base_url: "https://api.openai.com/v1"
  model: "gpt-4o-mini"
  temperature: 0.7
  timeout: 60
  max_retries: 3
  streaming: true

agent:
  work_dir: "."
  max_iterations: 10
  system_prompt: |
    你是一个可以使用工作区工具的助手。
    当前工作目录为 {work_dir}。

session:
  dir: ".sessions"
  auto_save: true

tools:
  shell_timeout: 10
  search_max_results: 100
```

相对路径以配置文件所在目录为基准解析。`agent.work_dir` 是工具允许访问的根目录，工具不能通过路径切换扩大这个范围。

### 2. API Key

复制环境变量示例：

```bash
copy .env.example .env
```

Linux/macOS：

```bash
cp .env.example .env
```

然后只在 `.env` 中填写 API Key：

```dotenv
AGENT_API_KEY=你的API_KEY
```

不要把真实 `.env` 提交到 Git。项目的 `.gitignore` 已忽略 `.env` 和 `.sessions/`。

也可以通过系统环境变量覆盖配置：

```text
AGENT_API_KEY
AGENT_BASE_URL
AGENT_MODEL
AGENT_WORK_DIR
```

配置优先级为：代码默认值 < `config.yaml` < `.env` < 系统环境变量。系统环境变量优先级最高。YAML 中的 `llm.api_key` 不作为 API Key 来源。

## 启动方式

### 交互式 CLI

创建新 Session 并进入 REPL：

```bash
python main.py
```

禁用流式输出：

```bash
python main.py --no-stream
```

使用指定配置文件：

```bash
python main.py --config path/to/config.yaml
```

### 单条消息

执行一条消息、显示回答并保存 Session 后退出：

```bash
python main.py --message "请调用 list_dir 列出当前目录"
```

单条消息使用非流式输出：

```bash
python main.py --no-stream --message "请只回复：连接成功"
```

### 恢复 Session

恢复最近保存的 Session：

```bash
python main.py --resume
```

恢复最近 Session 并直接发送新消息：

```bash
python main.py --resume --message "我们刚才聊了什么"
```

加载指定 Session：

```bash
python main.py --session <session-id>
```

`--session` 指向不存在的 Session 时会明确报错，不会自动创建。`--resume` 与 `--session` 同时使用会报参数冲突。

## 交互命令

在 CLI 中输入：

```text
/help
/exit
/quit
/tools
/session new
/session list
/session load <session-id>
/session info
/history
/clear
/cd <path>
/config
```

说明：

- `/help`：显示帮助；
- `/exit`、`/quit`：保存当前 Session 并退出；
- `/tools`：列出六个工具及启用状态；
- `/session new`：保存当前 Session 并创建新 Session；
- `/session list`：列出已保存 Session；
- `/session load <id>`：保存当前 Session 后加载指定 Session；
- `/session info`：显示当前 Session 元数据；
- `/history`：显示当前消息摘要；
- `/clear`：清空消息历史，但不会撤销已经完成的文件或命令操作；
- `/cd <path>`：通过 `change_dir` 工具切换目录，支持带空格的路径；
- `/config`：显示非敏感配置，不显示 API Key。

## 工具和安全边界

文件工具、目录工具和搜索工具都限制在 Agent 启动时的允许工作目录范围内。`change_dir` 只能在这个根目录内切换，后续相对路径以新的当前目录为基准。

`shell` 使用当前工作目录作为进程 `cwd`，支持 timeout，并返回 stdout、stderr 和退出码。它是教学用命令执行工具，不是安全沙箱；CLI 不声称它可以完全限制命令内部访问其他路径。请只在受信任的本地项目目录中使用，并避免让模型执行破坏性命令。

工具结果在终端显示时可以截断，但会话消息中保存完整结果。API Key 不会写入 Session。

## Session 文件

Session 默认保存在：

```text
.sessions/
```

每个 Session 是一个 JSON 文件，索引位于：

```text
.sessions/index.json
```

Session 保存：

- Session ID；
- 创建时间和更新时间；
- 标题；
- `root_dir` 和当前 `work_dir`；
- 完整消息历史，包括 assistant `tool_calls` 和 tool `tool_call_id`。

Session 不保存 API Key、完整配置对象或 LLM 客户端对象。

## 测试

所有测试均使用 Mock LLM 和临时目录，不调用真实模型或付费 API：

```bash
python -m pytest -q
```

当前测试覆盖：

- 配置加载和环境变量优先级；
- 非流式和流式 LLM 响应解析；
- 六个工具及路径边界；
- Agent Loop、工具调用和消息顺序；
- Session 保存、恢复、删除和损坏文件；
- CLI 参数、交互命令、流式/非流式路径和 Session 切换。

## 项目结构

```text
project_class_tool/
├── agent/
│   ├── cli.py       # REPL 和斜杠命令
│   ├── config.py    # YAML、.env 和环境变量配置加载
│   ├── llm.py       # OpenAI 兼容 LLM 客户端
│   ├── loop.py      # Agent Loop 和 Function Calling 闭环
│   ├── session.py   # JSON Session 持久化
│   └── tools.py     # 工具注册和六个基础工具
├── tests/           # 离线测试
├── config.example.yaml
├── .env.example
├── DESIGN.md
├── DESIGN_1.md
├── main.py
├── README.md
└── requirements.txt
```

## 当前未实现

以下内容不属于当前阶段：

- Skill 系统；
- `MEMORY.md` 长期记忆；
- 向量数据库；
- 多智能体；
- 数据库 Session 后端；
- 并发工具执行；
- 复杂 Session 数据迁移；
- GUI 或 Web 服务。
