# 纸书智伴

这是一个协作开发快照：已有主题故事、多角色写作、逐页媒体播放和互动接口，仍需补齐指定 PDF 理解、整片合成、基于书本的检索问答及验收。当前快照尚未完成新环境启动、真实模型 API 或浏览器全流程验证。

## 目录与开发基线

- [`app/`](app/)：共享版主开发基线，后续业务修改集中在这里。
- [`reference/pdf-import/`](reference/pdf-import/)：PDF 分支的冻结参考快照，保留其导入实现与已知断点；尚未与 `app/` 合并。
- [`docs/`](docs/)：功能现状、开发顺序与验证边界。

两个目录是同一项目的不同快照，不能直接相互覆盖。共享版的接口前缀兼容、语音适配和视频任务适配应在集成中保留。

## 当前功能

| 功能 | 当前状态 |
| --- | --- |
| 主题故事与多角色写作 | 已有实现，重试及异常处理待修复 |
| TTS、录音 ASR、通用问答 | 已有调用链，真实服务与浏览器流程未实测 |
| 配图、音效、音乐 | 有适配代码，默认配置不完整 |
| 单页图生视频、当前帧点读、书架 | 已有实现，坐标契约与任务失败状态待修复 |
| 指定 PDF 导入与理解 | 仅参考快照有入口，模式传递、渲染和页号映射存在断点 |
| 跨页配音整片、书本检索问答、可恢复聊天历史 | 缺失 |
| 全链路流式与低延迟 | 部分事件流已有，未形成边生成边播放或达标证据 |

“已有实现”表示能追踪到代码，不表示已通过真实服务验收。样例插画包含应用外生成素材，现成媒体不能证明自动生成闭环。详见[功能状态](docs/FEATURE_STATUS.md)。

## 本地启动准备

克隆私有仓库后，在仓库根目录执行以下 Windows PowerShell 命令。这是待验收的运行步骤；当前静态检查使用 Python 3.12.14，项目完整依赖兼容范围尚未验证。

```powershell
cd app
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

在本地 `.env` 填写自己的配置。参考 [`app/config.py`](app/config.py)：故事、问答、图像理解和图生视频需要对应服务配置；文生图、音乐、音效还需补齐模板未列出的 `TEXT2IMAGE_*`、`TEXT2MUSIC_*`、`TEXT2AUDIO_*`。不要将实际凭据提交到仓库。

ASR 权重没有随快照分发。准备 `Systran/faster-whisper-small` 模型至 `app/models/faster-whisper-small/`，并核对 `.env` 的 `WHISPER_MODEL`。TTS 使用 `edge-tts`，仍需要网络；未配置的生成能力不能据此视为可用。

完成准备后，在 `app/` 运行：

```powershell
.\.venv\Scripts\python.exe story_tts_server.py
```

浏览器打开 `http://localhost:5000/tts_v2.html`。`/health` 目前只返回固定状态，不能证明模型服务可用。`app/` 当前没有 PDF 上传入口；参考快照不能当作已合并功能使用。

## 协作入口

- [贡献约定](CONTRIBUTING.md)：修改范围、分支与 PR 要求。
- [功能状态与已知断点](docs/FEATURE_STATUS.md)：源码证据及实现边界。
- [开发路线](docs/ROADMAP.md)：可认领条目与验收条件。
- [验证记录](docs/VALIDATION.md)：已做检查、结果与未测项目。
- [源码来源与导入范围](docs/SOURCE_PROVENANCE.md)：两个快照、逐文件校验和与未纳入仓库的运行数据。
