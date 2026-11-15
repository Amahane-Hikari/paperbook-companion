# 功能状态与已知断点

本页描述导入时的代码快照。主线为 `app/`，PDF 功能仍在冻结参考目录中，尚未合并或修复。

状态含义：“已有实现”表示存在可追踪的代码；“部分实现”表示链路、配置或失败处理不完整；“缺失”表示当前可达业务路径没有相应能力。所有外部模型调用与完整浏览器流程均未实测，详见[验证记录](VALIDATION.md)。

## 功能矩阵

| 功能 | 状态 | 实现与边界 |
| --- | --- | --- |
| 网页与服务入口 | 部分实现 | `app/` 有 [前缀中间件](../app/story_tts_server.py#L48-L62)及 [.env 加载](../app/config.py#L9-L27)，完整启动未验收；PDF 参考快照缺同等前缀兼容。 |
| 指定 PDF 导入 | 部分实现，仅参考快照 | [PDFReader](../reference/pdf-import/pdf_book_reader.py#L689-L812) 有转图、文字、OCR、视觉和物体路径；网页默认仍进入 fast，完整 AI 路径未接通。`app/` 没有 PDF 入口。 |
| PDF 阅读 | 部分实现，仅参考快照 | [阅读函数](../reference/pdf-import/tts_v2.html#L3738-L3803) 调用未定义函数，空文字页又会造成图文错位，不能直接视为可用阅读链。 |
| 主题故事 | 已有实现 | [简易写作](../app/text.py#L50-L106) 与 [深度写作](../app/ai_story_generate.py#L81-L189) 均存在；简易模式先取得整篇结果。 |
| 多 Agent | 部分实现 | 有提问者、专家、大纲与章节作者等角色流水线；[基类](../app/text_agent_base.py#L37-L96) 重试/异常处理有缺陷，尚无可靠跨 PDF、检索、视频任务编排。 |
| 配图、音乐、音效 | 部分实现 | [配图调用](../app/story_tts_server.py#L821-L846) 后可派生视频；[配置](../app/config.py#L59-L71) 仍有占位端点，模板不完整。 |
| TTS 与 ASR | 已有实现 | [TTS 保存后转 WAV](../app/local_tts.py#L48-L85)、[ASR 惰性加载](../app/local_asr.py#L15-L28)和 [录音问答链](../app/tts_v2.html#L7381-L7455) 已有；模型权重未分发，TTS 仍需网络。 |
| 单页图生视频 | 已有实现 | [提交、轮询、下载](../app/volcano_video.py#L434-L496)及 [后台任务](../app/story_tts_server.py#L2299-L2346) 已有；当前固定 480p/5秒，PDF 阅读尚未连接此链。 |
| 配音整片 | 缺失 | [播放器](../app/tts_v2.html#L3893-L3912) 循环静音短片、独立播放 WAV；[音频拼接](../app/story_tts_server.py#L1747-L1791) 属段内处理，没有跨页配音视频导出。 |
| 点击识别与高亮 | 部分实现 | [暂停并读取当前时间](../app/tts_v2.html#L3832-L3886)后调用 [截帧识别](../app/story_tts_server.py#L2510-L2603)，标签可朗读；坐标格式仍靠范围猜测。 |
| 通用语音问答 | 已有实现 | ASR → [仅问题的问答请求](../app/tts_v2.html#L7433-L7437) → TTS；[NLU 消息](../app/nlu.py#L49-L56) 没有当前书与历史。 |
| 书本/视频检索问答 | 缺失 | [/api/nlu](../app/story_tts_server.py#L2170-L2177) 只转发 question，无原文、页/帧/时间定位、检索依据。 |
| 历史记录 | 部分实现 | [书架加载保存](../app/story_tts_server.py#L1213-L1436) 已有；[聊天消息](../app/tts_v2.html#L7236-L7242) 仅追加 DOM。保存助手 WAV 不等于可恢复、可检索的多模态会话。 |
| 全链流式与低延迟 | 部分实现 | 有 [SSE](../app/story_tts_server.py#L550-L668)，但 [前端](../app/tts_v2.html#L6721-L6727) 等全部段落和视频后才播放；ASR、NLU、TTS 均无当前浏览器全链流式验收。 |

## 阻断完整流程的问题

- **PDF 模式未传递**：[分析入口](../reference/pdf-import/story_tts_server.py#L2504-L2610) 未把 mode 放入后台 options；底层默认 fast，只取文字层。勾选 OCR 校验也不会使 fast 执行视觉理解。
- **PDF 开始阅读即报错**：[前端第 3761 行](../reference/pdf-import/tts_v2.html#L3761) 调用未定义的 `renderCurrentPage()`，第 3803 行还调用未定义的 `updateCurrentAudio()`。原函数隔离执行时，TTS 任务未能启动。
- **页号映射丢失**：[服务端过滤空文字页](../reference/pdf-import/story_tts_server.py#L2746-L2757)，但保留所有 page_details；前端按数组下标选原页图，导致错配。[完整模式](../reference/pdf-import/pdf_book_reader.py#L809-L812) 仍会跳过无字页物体检测。
- **Agent 失败处理**：[重试循环](../app/text_agent_base.py#L54-L88) 在结构校验失败时不增加计数，首次请求异常又引用未赋值的 response；[章节重试](../app/ai_story_generate.py#L168-L181) 传入不支持的 seed 参数；[模型输出解析](../app/ai_story_generate.py#L63-L66) 使用 eval。
- **假成功与长等待**：[SSE error](../app/tts_v2.html#L6711-L6715) 被解析 catch 吞掉；[视频等待](../app/tts_v2.html#L6393-L6456) 可持续五分钟，失败状态主要靠日志与文件存在性表达。
- **点读坐标误判**：[模型要求绝对像素](../app/gemini_analyze.py#L104-L122)，[前端](../app/tts_v2.html#L5422-L5429) 却把最大坐标处于 500—1000 的框猜为归一化。864×480 帧上的合法框 `[100,100,600,400]` 会被错改为 `[86,48,518,192]`。

## 样例能说明什么

本地核查确认，原始共享样例的 manifest 记录插画由应用外工具生成，非应用内文生图产物。现成样例包含三段约 5.04 秒的无音轨视频和三份独立 WAV；这些运行产物未纳入源码仓库。[媒体核查摘要](VALIDATION.md)只确认文件存在且可解析，不证明新绘本自动生成、视觉/语音质量或完整运行流程已验收。

开发优先级与完成条件见[路线](ROADMAP.md)。
