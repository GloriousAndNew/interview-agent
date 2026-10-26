# Linux 多人部署说明 · v0.2.0

适用环境：Linux Docker Compose，建议 8 核、16 GB。默认通过 SSH 隧道访问，不向公网开放网页。Windows EXE 仍按本地模式运行；本版本不自动搬迁原有个人知识库。

## 默认能力

- 管理员创建账号、启停账号、重置密码、调整每人预算与空间；普通用户可以修改自己的密码。密码为 scrypt 哈希，登录会话有效期 8 小时，密码变更撤销旧会话。
- 每位用户独立目录与 SQLite 数据库，包含录音、转写、问答、个人简历、讲稿和高分答案。模型密钥统一由部署管理员配置，不显示给普通用户。
- 全局最多同时处理 2 个录音任务，每人同时 1 个、最多 3 个待处理任务；高分答案最多同时 2 个请求，每人 1 个。调度按账号轮转。
- 默认每人 2 GB 存储、每月 50 元的应用侧费用预留，单文件 100 MB。预算是估算与预留，不是供应商账单。存储为写入前检查，处理中的衍生文件可能超出配额；磁盘不足 2 GB 时停止新写入。
- 失败任务保留检查点，任务页显示阶段、进度和耗时。服务重启可能要等过期租约释放，再恢复任务；正在云端执行的请求不能保证取消计费。
- 文件下载、播放与上传检查登录和归属；下载票据 1 小时有效。禁用账号后无法登录或下载，已执行的云端任务仍可能结束。禁用账号不删除数据；禁用前应完成或清理其待处理任务与 OSS 临时对象。

## 首次部署

将源码放到 `/opt/interview-agent`，安装 Docker 与 Compose v2。执行：

```bash
cd /opt/interview-agent
cp deploy/.env.example deploy/.env
chmod 600 deploy/.env
# 使用编辑器填写 deploy/.env，勿提交密钥
openssl rand -hex 40  # 生成 GATEWAY_SECRET，填写到配置中
# BOOTSTRAP_ADMIN_PASSWORD 使用独立随机密码，至少 12 位

docker compose --env-file deploy/.env -f deploy/compose.yaml build auth
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d
docker compose --env-file deploy/.env -f deploy/compose.yaml ps
```

首次启动生成管理员；后续启动不会覆盖已有账号或密码。`deploy/.env` 包含云端密钥和初始管理员密码，只授予运维账号读取。不要将 Windows 环境变量文件或个人数据库放进镜像。Docker 构建上下文使用白名单。

必填配置：`GATEWAY_SECRET`、`BOOTSTRAP_ADMIN_PASSWORD`；使用模型还需 `AliApiKey`、`deepseek`，录音转写需 `OSS_ACCESS_KEY_ID`、`OSS_ACCESS_KEY_SECRET`、`ASR_STORAGE_BUCKET`、`OSS_ENDPOINT`。`OSS_SESSION_TOKEN` 仅临时凭据需要。云端录音流程仍使用私有 OSS，与本地版一致。

### 无域名，通过 SSH 隧道使用

保持模板配置：`BIND_ADDRESS=127.0.0.1`、`HTTP_PORT=8080`、`PUBLIC_ORIGIN=http://127.0.0.1:18501`、`SERVER_ALLOW_HTTP=1`，Caddy 使用 `./Caddyfile.loopback`。

在每位使用者的电脑运行：

```bash
ssh -N -L 18501:127.0.0.1:8080 SSH用户名@服务器IP
```

保持终端打开，浏览器访问 `http://127.0.0.1:18501`。地址必须与 PUBLIC_ORIGIN 完全一致，请使用 127.0.0.1，不要改为 localhost。不同电脑可以使用相同端口。服务器上的 8080/8443 只监听回环，不能直接访问公网 IP 的网页端口。

这是通过 SSH 加密连接访问服务器本机网页，不是开放局域网或公网 HTTP。团队成员需要各自的 SSH 访问方式，建议分配独立 SSH 密钥账号；不要共享 root 密码。若有 VPN，也可后续增加 VPN 内网入口。

登录后在「账号与配额 → 账号管理」添加团队账号。账号名仅小写字母、数字、下划线、短横线，3–32 字符；密码 12–128 字符。登录页和管理页不提供注册入口。

### 后续配置公网 HTTPS

先取得解析到服务器的域名，配置防火墙 80/443；把 BIND_ADDRESS 改为 0.0.0.0、HTTP_PORT=80、HTTPS_PORT=443、DOMAIN=域名、PUBLIC_ORIGIN=https://域名、SERVER_ALLOW_HTTP=0、CADDYFILE=./Caddyfile。重新启动 Compose，Caddy 自动申请证书。迁移前验证 DNS 和证书签发条件。不要将允许 HTTP 的配置直接暴露公网。

## 运行与配置

```bash
cd /opt/interview-agent
docker compose --env-file deploy/.env -f deploy/compose.yaml ps
docker compose --env-file deploy/.env -f deploy/compose.yaml logs --tail 80 worker web auth
docker compose --env-file deploy/.env -f deploy/compose.yaml restart
```

`SERVER_JOB_CONCURRENCY` 默认 2，允许 1–4。默认 worker 内存上限 8 GB，web 4 GB，auth 768 MB。初期保持默认，先看任务阶段耗时再调整；增加并发受供应商限流、内存和网络影响。

`SERVER_ANSWER_CONCURRENCY` 控制高分答案并发，`SERVER_UPLOAD_MB` 控制应用层录音导入上限。增大上传限制还须同步修改 compose 的 Streamlit `--server.maxUploadSize` 和 Caddy `request_body max_size`，并留出请求编码开销。文档抽取还受原有字符限制及模型上下文限制，文件大小不等于可处理文本长度。

服务端不提供 EXE 更新和公共密钥设置页。模型地址、价格等共用设置从数据卷 `/data/config/settings.json` 加载；修改前备份并重启。不要直接编辑用户目录内的 settings.json，它不会覆盖管理员配置。界面不支持更改全局模型设置。

## 备份与恢复

```bash
cd /opt/interview-agent
bash deploy/backup.sh
install -m 644 deploy/interview-agent-backup.service /etc/systemd/system/
install -m 644 deploy/interview-agent-backup.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now interview-agent-backup.timer
systemctl list-timers interview-agent-backup.timer
```

每日 03:00 备份，保留最近 7 份；备份前短暂停止 auth/web/worker，完成或失败均尝试恢复服务。备份含账号、密码哈希、个人数据、文件和检查点，输出到 `/opt/interview-agent/backups`，权限 600，附 SHA-256。`deploy/.env` 不在数据卷备份内，应由运维另行安全保存。当前为同机备份，不能防止整台云盘损坏；上线后应将备份加密复制到独立存储。

恢复先停止全部服务并保留当前数据卷副本，校验 `sha256sum -c 备份.tar.gz.sha256`，将归档解压到新建的 app_data 数据卷根目录，设置归属 UID/GID 10001，核对 .env 与版本后启动。恢复会覆盖账号和数据，应先在独立环境演练。禁止在运行中覆盖 SQLite 文件，禁止使用 `docker compose down -v` 作为升级步骤。

## 更新与验收

更新前备份、记录当前镜像版本；拉取经验证的新版本源码，设置 APP_VERSION，构建 auth，然后 `up -d` 更新全部服务。同一数据库格式不兼容时不能只回滚镜像，应恢复相应备份。服务器升级由管理员操作，用户不通过 EXE 更新器更新服务器。

每次验收至少检查：未登录跳转登录；两用户录音、知识库、个人资料互不可见；跨用户下载和上传被拒绝；任务并发不超过配置；退出或改密码后旧会话失效；备份完成后服务恢复；宿主机没有公开 8000/8501/8080 端口。

自动化测试使用临时数据和模拟供应商，不代表已验证真实账号余额、ASR 限流和付费转写成功。
