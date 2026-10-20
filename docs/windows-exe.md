# Windows 本地程序使用说明

当前版本：**v0.1.4**。页面侧栏、启动窗口及 EXE 属性可查看版本号。

本次更新：优化导入工作台、最近面试入口、问答阅读卡片、任务进度层级及设置页分组，并适配窄屏。保留自动更新、上传测速、断点续传和 MP3 压缩功能。下载包名称为 `interview-agent-v0.1.4-windows-x64.zip`。v0.1.3 用户可通过程序内检查更新升级；更旧版本请退出后完整解压程序包，保留原数据目录。

后续自动更新的使用、发布和故障恢复见 [自动更新说明](automatic-updates.md)。自动安装需保留同目录 `interview-agent-updater.exe`，并将知识库数据放在程序目录之外。

## 打开程序

适用于 Windows 10/11 x64。解压整个程序包到可写目录，双击 `interview-agent.exe`，稍候自动打开本地网页。程序自带 Python、网页依赖和 FFmpeg，无需安装 Python 或 Office。只复制 EXE 文件无法运行，必须保留同目录 `_internal` 文件夹。

控制窗口可再次打开页面、查看日志或停止服务。关闭浏览器不停止任务；关闭控制窗口会停止本地服务，未完成任务保留检查点，下次启动后恢复。端口默认 8501，被占用时尝试后续端口。

## 模型配置

云端模型需要联网，并使用本机系统环境变量：

- `deepseek`：DeepSeek 文本整理密钥。
- `AliApiKey`：阿里云转写密钥。
- `OSS_ACCESS_KEY_ID`、`OSS_ACCESS_KEY_SECRET`：OSS 凭据。
- `ASR_STORAGE_BUCKET`、`OSS_ENDPOINT`：OSS 存储桶及 HTTPS 区域地址。

导入 DOCX、Markdown、TXT 或标准转写 JSON 可跳过 OSS/语音转写，仍需文本模型密钥。程序包不包含任何个人密钥、录音或知识库。

## 数据保存与更新

默认数据目录为 `%LOCALAPPDATA%/InterviewAgent`，可通过 `DATA_DIR` 或 `--data-dir` 指定。若默认目录尚无数据库，且发现本机 Codex 容器中仅有一份旧知识库，会继续使用其原路径，不移动或删除文件。具体路径在“设置与数据”中显示。

更新时先退出程序，再替换程序目录；数据独立保存，不在 `_internal` 中。完整迁移需关闭程序并复制整个数据目录，单独备份 EXE 不含面试记录。

## 故障排查

点击控制窗口的“打开日志”，检查 `desktop-server.log`、`desktop-worker.log`、`desktop-console.log`。打包版仅监听 `127.0.0.1`。环境变量变更后重新打开程序。

需要手动指定数据目录或端口时：

```powershell
.\interview-agent.exe --data-dir 'D:\InterviewData' --port 8510
```

可用相同数据目录发送停止请求：

```powershell
.\interview-agent.exe --stop --data-dir 'D:\InterviewData'
```

## 重新构建

在 Windows x64、Python 3.13 项目虚拟环境中安装运行依赖和 `requirements-build.txt`，执行 `build-windows.ps1`。脚本先运行测试，再构建目录式程序、ZIP 和 SHA-256 校验文件。构建过程不读取个人数据目录或打包密钥。此构建未做发行者数字签名。

`tools/smoke_windows.py` 可在独立中文数据目录中禁用模型密钥、移除 Python PATH，验证 EXE 页面服务、工作进程及内置 FFmpeg。该测试不产生云端调用。

打包使用 [PyInstaller 官方资源路径机制](https://pyinstaller.org/en/stable/runtime-information.html)，通过 [spec 文件](https://pyinstaller.org/en/stable/spec-files.html) 收集网页静态资源、依赖元数据和音频工具。


## 高分答案功能更新

知识库每题可按需生成、重新生成与下载高分参考答案，结果与原答分开保存，调用计入文本月预算。新版本使用数据库版本 4；升级后应使用新 EXE，旧版无法打开已升级数据库。替换程序前先退出旧控制窗口，保留数据目录和备份；请勿将旧版程序文件覆盖到新版目录。


## 压缩上传副本更新

新任务预处理后上传 16 kHz、单声道、64 kbps MP3，原始录音与回听不变。双声道分别识别时每路单独压缩。已有 WAV 检查点和远端任务继续沿用，避免重复转写。

## v0.1.2 上传与恢复

「设置与数据 → OSS 上传测试」可手动上传 1 MB 随机数据，显示结果并自动清理。不上传录音、不调用 ASR 或 DeepSeek，会产生少量 OSS 请求与存储费用。测试使用已保存的 OSS 配置，小文件平均速度包含连接和协议开销，仅供参考。最近测试保存在本机；配置变化后请重新测试。

处理任务每 3 秒刷新上传字节、已确认分片、当前速度、记录上传耗时及恢复次数。展开每路上传记录可看各次尝试、平均速度和接续字节；脱敏诊断报告也包含这些信息，不包含上传会话 ID、对象地址或密钥。后台进度最多约每秒落盘一次，分片确认后立即保存。异常退出时最后一小段计时可能未落盘；耗时不计等待重试及关闭程序的时间。进度达到 100% 后仍需等待 OSS 合并确认。

上传以 1 MB 分片顺序进行。断网自动重试，重启后沿用任务检查点并查询云端分片，只补传缺失/未确认部分；失败或取消后可点「从检查点重试」。已上传、已提交转写的任务继续沿用，避免重复提交 ASR。开始分片上传后绑定原 bucket/endpoint，配置修改只影响尚未开始上传的任务。v0.1.1 及更旧版本没有分片会话记录，未完成上传第一次升级重试需重新开始，之后可续传。

副本哈希不匹配时阻止混合不同文件；会话已被 OSS 清理时自动新建并从头上传。临时对象和分片纳入后台清理，待处理任务的会话不会被清理；完成识别后即可清理，失败任务保留约 49 小时以供重试。权限不足会保留清理记录并提示待处理。可在 bucket 配置过期分片生命周期作为补充（建议至少 7 天），覆盖远端初始化成功但本地来不及保存会话等极端情况。

OSS 凭据除对象上传、读取、删除外，还需允许查询和取消分片：`oss:ListParts`、`oss:AbortMultipartUpload`；初始化、上传、合并分片使用 `oss:PutObject`。上传验证使用 `oss:GetObject`，清理需要 `oss:DeleteObject`。只授予所用 bucket 的 `interview-agent/*` 对象前缀权限即可，不需设为公共读。参见 [阿里云分片上传文档](https://www.alibabacloud.com/help/en/oss/developer-reference/use-oss-python-sdk-to-multipart-upload)。

本地环境真实 OSS 验证：1 MB 上传成功，约 5.35 秒；另用 2.25 MB 随机文件在确认首个 1 MB 分片后主动中断，恢复时沿用同一上传会话，只再发送 1.25 MB，最终合并成功。测试对象及分片均已清理，未调用 ASR 或 DeepSeek。这是当前网络下的小样本验证，不代表固定上传速度。
