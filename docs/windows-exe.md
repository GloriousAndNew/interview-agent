# Windows 本地程序使用说明

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
