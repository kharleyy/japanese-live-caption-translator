# Japanese Live Caption Translator

这是一个运行在 Windows 11 上的本地实时字幕翻译工具。它读取 Windows“实时辅助字幕”中显示的日语文本，通过 OpenAI 兼容接口翻译成中文，并在桌面上显示可拖动的置顶双语悬浮窗。

项目默认使用 DeepSeek：不改配置时，API 地址是 `https://api.deepseek.com`，模型是 `deepseek-chat`。也可以切换到其他 OpenAI 兼容服务。

项目不会直接录制扬声器音频，也不使用截图或 OCR。字幕来源是 Windows 实时字幕通过 UI Automation 暴露的文本。因此占用很小，是个轻量化翻译插件。

## 主要功能

- 自动启动 Windows 实时辅助字幕，并尝试切换到“日语（日本）”。
- 持续读取字幕，等待内容稳定后按句分段。
- 调用 OpenAI 兼容 API 翻译稳定片段，默认使用 DeepSeek。
- 在置顶悬浮窗中显示日文原文和中文翻译。
- 支持暂停翻译、清空当前上下文、查看当天的请求与 Token 统计。
- 提供仅绑定本机 `127.0.0.1` 的历史页面，可查看和删除指定日期的记录。
- 自动保存字幕历史到本地 SQLite，关闭程序后可以再次打开。

## 使用要求

开始前请确认：

- 使用 Windows 11 普通桌面环境，不适用于无界面的服务器或远程非交互桌面。
- 已安装 Python 3.12 或更高版本。
- Windows“实时辅助字幕”功能可用。
- 已安装日语语音识别语言包。
- 可以访问你选择的 OpenAI 兼容 API，默认是 DeepSeek。
- 有可用的 API Key 和账户余额。

程序的音频识别由 Windows 实时辅助字幕完成，因此系统中需要能够正常显示日文字幕。

## 首次使用
## 再次注意 Windows“实时辅助字幕”功能可用（win+ctrl+L能跳出来实时字幕）。
## 在实时字幕里安装日语语音识别语言包。
### 1. 安装 Python

从 [Python 官方网站](https://www.python.org/downloads/windows/)安装 Python 3.12 或更高版本。

安装时建议勾选：

```text
Add python.exe to PATH
```

安装完成后，可以在命令提示符中运行下面的命令检查：

```powershell
python --version
```

### 2. 第一次双击启动

直接双击项目根目录的：

```text
start-live-caption-translator.cmd
```

第一次启动时，脚本会自动：

1. 创建项目专用的 `.venv` 虚拟环境。
2. 安装项目依赖。
3. 如果项目中没有 `.env`，从 `.env.example` 创建一个。
4. 打开 Notepad，让你填写 API Key。

首次安装依赖需要联网，可能需要等待片刻。安装成功后，脚本会在 `.venv` 中保存安装标记；以后再次双击会直接启动，不会重复安装。

### 3. 填写 API Key

使用 DeepSeek 时，只需在 `.env` 中填写 `API_KEY`。如果使用其他 OpenAI 兼容模型，还需要同时修改 `.env` 中的 `API_BASE_URL` 和 `API_MODEL`。

在首次打开的 `.env` 文件中找到：

```dotenv
API_KEY=
```

保留配置名，在等号右侧粘贴你自己的密钥。不要额外添加引号，也不要保留尖括号或其他占位符。

保存并关闭 Notepad 后，程序会继续启动。

默认配置使用的 API Key 可以在 DeepSeek 开放平台控制台中创建。使用其他兼容服务时，请填写对应平台提供的密钥。不要把 `.env` 发给别人，也不要把真实密钥写进 README、截图、聊天记录或提交到 GitHub。

### 4. 启动 Windows 实时字幕

程序会尝试自动启动实时字幕并选择“日语（日本）”。如果自动配置失败，可以手动检查：

1. 打开 `设置 > 辅助功能 > 字幕 > 实时辅助字幕`。
2. 确认实时字幕功能已启用。
3. 确认已经安装或启用日语语音识别语言包。
4. 播放带有日语音频的内容。
5. 确认 Windows 实时字幕本身能够显示日文。

如果 Windows 自己无法生成日文字幕，本程序也无法读取或翻译字幕。

## 日常使用

环境安装完成后，通常只需要：

1. 双击 `start-live-caption-translator.cmd`。
2. 播放日语音频。
3. 查看屏幕上的双语悬浮窗。

悬浮窗提供以下按钮：

| 按钮 | 作用 |
| --- | --- |
| 暂停 | 暂停新的翻译请求，字幕仍会继续采集和保存 |
| 清空 | 清空悬浮窗并重置后续翻译上下文，不删除历史记录 |
| 统计 | 查看当天的请求次数、成功/失败次数、Token 和估算费用 |
| 历史 | 打开仅在本机运行的历史页面 |
| 设置 | 调整透明度以及深色或浅色主题 |

默认历史页面地址为：

```text
http://127.0.0.1:8765/
```

该服务只绑定本机回环地址，不会向局域网或互联网开放。

## 配置项

本地配置位于根目录的 `.env`。它不会被提交到 Git。

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `API_KEY` | 空 | 必填，服务商提供的 API 密钥 |
| `API_BASE_URL` | `https://api.deepseek.com` | OpenAI 兼容 API 根地址 |
| `API_MODEL` | `deepseek-chat` | 翻译使用的模型 |
| `API_TIMEOUT_SECONDS` | `30` | 单次请求超时时间 |
| `API_MAX_OUTPUT_TOKENS` | `512` | 单次翻译的最大输出 Token |
| `API_INPUT_PRICE_PER_MILLION` | `0` | 每百万输入 Token 的价格 |
| `API_OUTPUT_PRICE_PER_MILLION` | `0` | 每百万输出 Token 的价格 |
| `FINAL_DEBOUNCE_MS` | `1500` | 当前句停止变化多久后视为稳定 |
| `TRANSLATION_RETRY_SECONDS` | `5,15,60,300` | 临时网络错误的退避重试时间 |
| `HISTORY_PORT` | `8765` | 本地历史页面端口 |

`API_BASE_URL` 不要包含最后的 `/chat/completions`，程序会自行拼接。例如：

```dotenv
API_BASE_URL=https://api.deepseek.com
API_MODEL=deepseek-chat
```

替换服务时，只需要修改这三个值：

```dotenv
API_KEY=
API_BASE_URL=你的 OpenAI 兼容 API 根地址
API_MODEL=服务商提供的模型名称
```

服务商需要支持 `POST {API_BASE_URL}/chat/completions`，接受 Bearer Token，并按照 OpenAI Chat Completions 格式返回 `choices[0].message.content`。DeepSeek、OpenAI 以及大多数兼容网关都使用这种格式；原生 Anthropic、Gemini 等不同接口格式不能直接使用。

价格默认是 `0`，这只影响悬浮窗中的费用估算，不代表服务免费。需要准确估算时，请按自己账户当前的计费标准填写。

旧配置名称 `DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL`、`DEEPSEEK_MODEL` 等仍然兼容，因此已有 `.env` 不需要立即修改。若同一项同时存在 `API_*` 和 `DEEPSEEK_*`，程序优先使用 `API_*`。

## 数据与隐私

- API Key 只保存在根目录的 `.env` 中。
- `.env`、`.venv`、数据库和日志都已加入 `.gitignore`。
- 字幕历史和运行状态保存在本地 `data/` 目录。
- 程序部署日志保存在本地 `logs/` 目录。
- 历史服务只监听 `127.0.0.1`。
- 需要翻译的字幕片段和少量上下文会发送到你配置的 API 地址。
- 项目自身不会直接录取音频、截图或进行 OCR。

如果密钥曾经被提交到公开仓库，只删除当前文件并不够。应立即在对应服务商的控制台撤销旧密钥并创建新密钥，然后再清理 Git 历史。

## 常见问题

### 双击后提示找不到 Python

确认已经安装 Python 3.12 或更高版本。如果没有自动加入 PATH，可以重新运行 Python 安装程序并启用 `Add python.exe to PATH`。

### 首次安装依赖失败

检查网络连接、代理或 pip 镜像配置。修复网络后重新双击脚本。如果虚拟环境已经损坏，可以删除 `.venv` 目录，再运行 `start-live-caption-translator.cmd` 重新安装。

### 提示 `API_KEY is missing`

打开根目录的 `.env`，确认 `API_KEY=` 后面填写了密钥，然后保存并关闭文件。旧配置中的 `DEEPSEEK_API_KEY` 也仍然可用。

### 程序找不到实时字幕

确认 Windows 版本支持实时辅助字幕，并在 Windows 设置中启用该功能。程序需要在当前登录用户的普通桌面环境中运行。

### Windows 能听到声音但没有日文字幕

检查 Windows 实时字幕选择的语言、日语语音识别语言包以及播放内容是否确实包含日语。

### 历史页面打不开

检查 `.env` 中的 `HISTORY_PORT` 是否被其他程序占用，例如改用：

```dotenv
HISTORY_PORT=8766
```

### 翻译请求产生费用吗

会。程序会对稳定字幕发起真实的 API 请求，费用由你的服务账户承担。悬浮窗中的价格字段只用于本地估算。

## 手动安装与开发

如果一键脚本无法运行，可以在 PowerShell 中手动执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
.\.venv\Scripts\python.exe main.py
```

运行测试：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

提交或分享前检查密钥：

```powershell
.\.venv\Scripts\python.exe tools\check_secrets.py
git status
```

## 项目结构

```text
start-live-caption-translator.cmd
                      首次安装和日常启动入口
main.py               应用入口
src/realtime_translator/
  application.py      启动、进程管理和整体生命周期
  config.py           .env 配置读取
  database.py         本地 SQLite 数据
  live_captions.py    Windows 实时字幕 UIA 接入
  translator.py       OpenAI 兼容 API 请求与响应处理
  translation_worker.py 翻译队列和重试
  overlay.py          置顶双语悬浮窗
  history_server.py   本机历史页面服务
  segmenter.py        字幕分段
templates/            历史页面模板
static/               历史页面样式和脚本
tests/                自动化测试
tools/                密钥检查和调试工具
```
