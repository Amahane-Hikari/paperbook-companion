# 验证记录

记录日期：2026-11-15。本页区分原始快照核查与当前主线验证。当前没有调用真实付费模型 API；受控服务输出不能作为真实模型质量、供应商可用性或首音时延成绩。

本次最终结果：**35项 Python 测试通过、5项 Node 前端单测通过、旧 SSE 错误处理回归通过；真实 Edge 的13项受控交互检查和5项未配置场景检查均通过**。Python 环境为3.12.14，Node为24.19.0，FFmpeg为9.0.1。Python 测试仍有上游 `fitz` 兼容导入及 `tqdm` 的弃用警告，不影响本次结果。结构化摘要见[验证结果](validation-2026-11-15.json)。

## 当前已成立的验证类别

| 类别 | 已确认的内容 | 结论边界 |
| --- | --- | --- |
| 真实 PyMuPDF | 文字、扫描及无字混合页；中文原文；稳定页号；模式传递及页数限制 | 使用真实 PDF 文件与解析器，OCR/视觉响应仍由受控方法提供 |
| Flask 与 HTTP loopback | 新入口、书本/任务/资源/会话接口，默认无凭据能力状态；本机受控 HTTP 请求 | 证明本机路由与调用链，不证明外部供应商可用 |
| 任务与缓存 | 失败、重试、重启中断、force、模型身份变化、旧整片失效与会话并发 | 面向当前单进程本地服务，不构成生产多进程验收 |
| 模型返回契约 | 空白 OCR、未知场景/物体结构、缺页引用或非法页引用被拒绝；无证据、弃答及原文摘录明确区分 | 合法页引用不是回答事实正确性的自动证明 |
| 真实 WAV 与 FFmpeg | 实测句子 WAV、整本配音拼接、H264/AAC、字幕、时间轴与工具缺失错误 | 真实编码/解码与时间检查；受控音频不是自然语音质量样本 |
| 20页非整帧回归 | 视频帧边界量化后，全20页与末页配音保留，整本音频一次编码 | 验证累计帧/音频边界，不代表任意素材的主观衔接质量 |
| Node 几何与上下文 | 5项测试覆盖 contain 留黑、缩放、绝对像素大框、切页/seek/播放后的迟到响应与恢复条件 | 纯函数检查；不能替代真实浏览器竞态验证 |
| 真实浏览器配合受控服务 | 三页混合 PDF 导入、原图阅读、失败重试、整片导出、引用问答；13项交互检查含图片/语音附件、刷新恢复、移动视口留黑、缩放高亮、当前视频帧识别、朗读后恢复和跨页迟到响应丢弃 | 使用真实浏览器、真实本地接口/文件/媒体；AI输出由受控适配器提供，不是在线模型验收 |
| 未配置服务的真实浏览器 | 5项检查覆盖缺失能力提示、扫描页 needs_ocr、阻止假媒体完成、明确原文摘录、ASR/视频能力拒绝与源码静态边界 | 不调用真实模型、网络TTS或自动下载ASR权重 |

测试输入在临时/本地受控目录生成，没有把真实书本、配音、视频或模型权重作为测试产物提交。

## 复现命令

以下均从仓库根目录执行，需 Python、Node、FFmpeg 与 ffprobe。Windows PowerShell 可使用本地虚拟环境：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest
node tests/js/test_book_ui.cjs
node tests/js/test_legacy_stream.cjs
```

依赖已激活时，对应命令为 `python -m pip install -r requirements-dev.txt`、`python -m pytest`。当前测试来源包括 [PDF](../tests/test_pdf_reader.py)、[书本管线](../tests/test_book_pipeline.py)、[视频](../tests/test_book_video.py)、[时间轴回归](../tests/test_export_timeline_regression.py)、[提供方契约](../tests/test_provider_contracts.py)、[Agent 回归](../tests/test_agent_regressions.py)、[服务集成](../tests/test_server_integration.py)及 [Node 检查](../tests/js/test_book_ui.cjs)。

启动本机受控应用：

```powershell
.\.venv\Scripts\python.exe tests/controlled_app.py --data-dir .local/controlled --port 18782
```

依赖已激活时可直接使用 `python tests/controlled_app.py --data-dir .local/controlled --port 18782`。打开 `http://127.0.0.1:18782/`，选择 `.local/controlled/controlled-mixed.pdf`。

[受控应用](../tests/controlled_app.py)使用真实 Flask、PDF、文件落盘与 FFmpeg；OCR、视觉、问答、ASR、配音和页动画均被替换为明确受控输出，不需要其他供应商。其配音是测试音、动画可由静态原页构造，不能作为真实模型演示或质量/时延成绩。

## 仍需验收

- 实际模型配置、认证、额度、响应契约、网络稳定性与错误恢复。
- OCR/视觉、语音识别、自然语音、视频画质/角色一致性及回答事实正确性。
- 真实麦克风及自然语音；不同浏览器的自动播放权限，以及切书/seek/取消等更完整的跨浏览器交互矩阵。已覆盖的本地恢复与竞态项不等于全部浏览器组合均已验收。
- 真实首音时延、多绘本泛化、内容定位与点读准确性；目标指标需对照正式规则。
- 目标机器完整安装和生产部署能力。

后续记录应给出输入、环境/版本、运行命令、实际结果和验证边界；仅有语法检查或受控响应时，不得写成真实 AI 已验收。

## 原始快照的历史核查

导入前两个快照曾通过36个 Python 文件与4段内联 JavaScript 的语法检查，并用隔离探针发现模式丢失、页错配、未定义函数、Agent 重试和假成功等问题。这是历史证据，不能当作当前主线测试总数。

原共享样例的9个媒体曾经 ffprobe 确认可解析：3张2240×1680 PNG、3份24kHz单声道 WAV、3段864×480且无音轨的约5.04秒视频。原样例 manifest 标注插画来自应用外工具；这些运行数据未纳入源码仓库，也不证明自动生成闭环。[源码来源清单](SOURCE_PROVENANCE.md)及原始校验和保持历史记录，不随当前实现修改。
