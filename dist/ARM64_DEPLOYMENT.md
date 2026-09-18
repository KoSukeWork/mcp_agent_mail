# MCP Agent Mail ARM64 部署与更新指南

本目录中的离线镜像使用稳定标签 `mcp-agent-mail:arm64`。后续拿到同名的新镜像包后，可以重复执行“更新部署”流程，无需修改 Compose 文件。

## 1. 文件清单

将以下文件放在 ARM64 Linux 服务器的同一个目录中：

```text
mcp-agent-mail-arm64.tar
docker-compose.yaml
.env.example
ARM64_DEPLOYMENT.md
```

运行环境要求：

- ARM64 Linux，`uname -m` 通常显示 `aarch64` 或 `arm64`
- Docker Engine
- Docker Compose v2（命令形式为 `docker compose`）

检查环境：

```bash
uname -m
docker version
docker compose version
```

## 2. 首次部署

### 2.1 加载离线镜像

在上述文件所在目录执行：

```bash
docker load -i ./mcp-agent-mail-arm64.tar
```

确认标签和架构：

```bash
docker image inspect mcp-agent-mail:arm64 \
  --format 'image={{.RepoTags}} platform={{.Os}}/{{.Architecture}} id={{.Id}}'
```

输出中的平台应为 `linux/arm64`。

### 2.2 创建配置文件

仅在 `.env` 不存在时复制模板，避免覆盖已有配置：

```bash
test -f .env || cp .env.example .env
```

生成随机密钥：

```bash
openssl rand -hex 32
openssl rand -hex 32
```

编辑 `.env`，至少设置以下项目：

```dotenv
# MCP 客户端使用的 Bearer Token；使用第一条随机值
HTTP_BEARER_TOKEN=替换为随机值

# 私有、单操作者实例需要创建项目、注册 Agent 和发送消息，因此授予写角色
HTTP_RBAC_ENABLED=true
HTTP_RBAC_DEFAULT_ROLE=writer

# 网页登录账号与密码
MAIL_UI_USERNAME=operator
MAIL_UI_PASSWORD=替换为至少12位的强密码

# 网页 Cookie 会话密钥；使用第二条随机值，至少32个字符
MAIL_UI_SESSION_SECRET=替换为随机值
```

静态 `HTTP_BEARER_TOKEN` 本身不携带角色。JWT 未启用时，服务端使用
`HTTP_RBAC_DEFAULT_ROLE`；默认的 `reader` 只能调用只读工具，创建项目、注册 Agent、
发送消息等操作会返回 `403 Forbidden`。上述 `writer` 配置适合只有可信使用者的私人实例；
多人或不同权限的客户端共用服务时，应改用带角色声明的 JWT。

不要把 `.env`、密码或 Token 提交到 Git，也不要把它们发到聊天或日志中。

### 2.3 选择监听方式

如果服务器直接向局域网开放 8765 端口：

```dotenv
MCP_AGENT_MAIL_PUBLISH_ADDRESS=0.0.0.0
MCP_AGENT_MAIL_PUBLISH_PORT=8765
```

如果前面有 Nginx、Caddy 或其他反向代理，建议只监听本机：

```dotenv
MCP_AGENT_MAIL_PUBLISH_ADDRESS=127.0.0.1
MCP_AGENT_MAIL_PUBLISH_PORT=8765
```

使用外部域名时，推荐 HTTPS，并设置实际访问地址：

```dotenv
IDENTITY_CONFIRMATION_BASE_URL=https://mail.example.com
IDENTITY_CONFIRMATION_ALLOW_INSECURE_HTTP=false
```

只在可信局域网确实必须使用明文 HTTP 时，才使用：

```dotenv
IDENTITY_CONFIRMATION_BASE_URL=http://mail.example.lan
IDENTITY_CONFIRMATION_ALLOW_INSECURE_HTTP=true
```

### 2.4 校验并启动

```bash
docker compose -f docker-compose.yaml config --quiet
docker compose -f docker-compose.yaml up -d
docker compose -f docker-compose.yaml ps
```

检查服务健康状态：

```bash
curl -fsS http://127.0.0.1:8765/health/liveness
curl -fsS http://127.0.0.1:8765/health/readiness
```

访问入口：

- 网页管理端：`http://服务器地址:8765/mail`
- MCP 地址：`http://服务器地址:8765/api/`
- 存活检查：`http://服务器地址:8765/health/liveness`

网页登录使用 `MAIL_UI_USERNAME` 和 `MAIL_UI_PASSWORD`。MCP 客户端使用 `HTTP_BEARER_TOKEN`，两套认证相互独立。

## 3. 域名反向代理示例

下面是精简的 Nginx 代理配置。生产环境应同时配置有效的 TLS 证书：

```nginx
server {
    listen 443 ssl http2;
    server_name mail.example.com;

    ssl_certificate     /path/to/fullchain.pem;
    ssl_certificate_key /path/to/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}
```

配置完成后：

- 网页管理端：`https://mail.example.com/mail`
- MCP 地址：`https://mail.example.com/api/`
- 健康检查：`https://mail.example.com/health/liveness`

## 4. Codex 客户端配置

在客户端的 `~/.codex/config.toml` 中添加：

```toml
[mcp_servers.agent_mail]
url = "https://mail.example.com/api/"
bearer_token_env_var = "AGENT_MAIL_TOKEN"
startup_timeout_sec = 300
tool_timeout_sec = 300
enabled = true
```

启动 Codex 前，把 `.env` 中的 `HTTP_BEARER_TOKEN` 放入客户端环境变量：

Linux/macOS：

```bash
export AGENT_MAIL_TOKEN='与服务端 HTTP_BEARER_TOKEN 相同的值'
```

Windows PowerShell（当前终端会话）：

```powershell
$env:AGENT_MAIL_TOKEN = '与服务端 HTTP_BEARER_TOKEN 相同的值'
```

重启 Codex 后检查：

```bash
codex mcp list
```

## 5. 重复更新部署

新版本仍应使用以下固定名称和标签：

- 文件：`mcp-agent-mail-arm64.tar`
- 镜像：`mcp-agent-mail:arm64`

先记录当前镜像 ID：

```bash
docker image inspect mcp-agent-mail:arm64 --format '{{.Id}}'
```

把新版 tar 放入部署目录，然后执行：

```bash
docker load -i ./mcp-agent-mail-arm64.tar
docker compose -f docker-compose.yaml up -d --force-recreate
docker compose -f docker-compose.yaml ps
```

再次验证镜像和服务：

```bash
docker image inspect mcp-agent-mail:arm64 \
  --format 'platform={{.Os}}/{{.Architecture}} id={{.Id}} created={{.Created}}'
curl -fsS http://127.0.0.1:8765/health/liveness
curl -fsS http://127.0.0.1:8765/health/readiness
```

Compose 使用固定的命名卷 `mcp-agent-mail-arm64-data`，强制重建容器不会清除数据库和邮箱数据。

> 不要执行 `docker compose down -v`，也不要删除 `mcp-agent-mail-arm64-data` 卷，否则会丢失持久化数据。

## 6. 更新前备份数据

以下流程会短暂停止服务，并把命名卷完整备份到当前目录：

```bash
docker compose -f docker-compose.yaml stop
docker run --rm \
  --user 0:0 \
  --entrypoint /bin/sh \
  -v mcp-agent-mail-arm64-data:/data:ro \
  -v "$PWD:/backup" \
  mcp-agent-mail:arm64 \
  -c 'tar -czf /backup/mcp-agent-mail-data-backup-$(date +%Y%m%d-%H%M%S).tar.gz -C /data .'
docker compose -f docker-compose.yaml start
```

确认备份文件已生成后，再加载和部署新镜像：

```bash
ls -lh mcp-agent-mail-data-backup-*.tar.gz
```

## 7. 日常运维与排查

查看状态：

```bash
docker compose -f docker-compose.yaml ps
```

查看最近日志：

```bash
docker compose -f docker-compose.yaml logs --tail=200 agent-mail
```

持续跟踪日志：

```bash
docker compose -f docker-compose.yaml logs -f agent-mail
```

重启服务：

```bash
docker compose -f docker-compose.yaml restart agent-mail
```

修改 `.env` 后不能只运行 `restart`，需要重新创建容器以加载新的环境变量：

```bash
docker compose -f docker-compose.yaml up -d --force-recreate
docker compose -f docker-compose.yaml exec agent-mail printenv HTTP_RBAC_DEFAULT_ROLE
```

私人实例的第二条命令应输出 `writer`。

检查镜像包 SHA-256：

```bash
sha256sum mcp-agent-mail-arm64.tar
```

常见问题：

- `/mail` 跳转到登录页是正常行为，使用 `MAIL_UI_USERNAME` 和 `MAIL_UI_PASSWORD` 登录。
- MCP 返回 `401 Unauthorized` 时，检查客户端 `AGENT_MAIL_TOKEN` 是否与服务端 `HTTP_BEARER_TOKEN` 完全一致。
- MCP 读取正常但创建项目、注册 Agent 或发送消息返回 `403 Forbidden` 时，确认 `.env` 中设置了 `HTTP_RBAC_DEFAULT_ROLE=writer`，然后使用 `up -d --force-recreate` 重新创建容器。
- 网页能开但 MCP 不通时，确认客户端地址包含 `/api/`，并检查反向代理是否关闭缓冲、放宽长连接超时。
- 域名访问异常时，先直接测试 `curl http://127.0.0.1:8765/health/liveness`。本机正常通常表示问题在反向代理、DNS、防火墙或证书配置。
- 容器反复重启时，先运行 `docker compose -f docker-compose.yaml logs --tail=200 agent-mail` 查看实际错误。

## 8. 当前部署约定摘要

| 项目 | 固定值 |
|---|---|
| CPU 架构 | `linux/arm64` |
| 镜像标签 | `mcp-agent-mail:arm64` |
| 离线包名称 | `mcp-agent-mail-arm64.tar` |
| Compose 文件 | `docker-compose.yaml` |
| 容器端口 | `8765` |
| MCP 路径 | `/api/` |
| 网页路径 | `/mail` |
| 持久化卷 | `mcp-agent-mail-arm64-data` |
