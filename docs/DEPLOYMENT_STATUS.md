# 当前部署状态

当前已安装并运行 **v3 / 3.0.0**。

公网 MCP 地址：

```text
https://doublemacbook-air.tail7ebfa6.ts.net/mcp
```

## 本机执行权限

- 按机主要求启用 `MCP_BRIDGE_FULL_ACCESS=true` 和 `MCP_BRIDGE_NETWORK_ACCESS=true`。
- 工作目录根设为 `/`，允许选择任意本地目录。
- 操作权限与当前 macOS 用户一致；系统隐私授权和系统文件保护仍然生效。
- OAuth 认证与本机回环监听保持开启。

## 已完成

- Tailscale Funnel 已开启，后台转发到 `127.0.0.1:8200`，HTTPS 可用。
- MCP 在 `127.0.0.1:8000` 运行，Gateway 在 `127.0.0.1:8200` 运行。
- 使用标准 Conda 环境 `chatgpt-mcp-bridge`；旧 `.venv` 已删除。
- Gateway 的公开地址已更新为上述 `ts.net` 域名。
- OAuth 动态客户端注册已持久化，PKCE S256 和资源绑定已实现。
- OAuth 授权码与刷新令牌均绑定客户端及 MCP 资源；错误资源和回调会被拒绝。
- GitHub OAuth App 的 Homepage 和 Callback 已由用户确认更新。
- ChatGPT 连接器已完成动态注册、真实 GitHub 回调与 OAuth Token 兑换。
- MCP 和 Gateway 已交给 macOS launchd 管理：登录自动启动，异常退出重启。
- Tailscale 应用已配置为登录后在后台打开；Funnel 使用其保存的后台配置。
- 原有 Cloudflare Named Tunnel 保留运行。

## 验证结果

| 检查 | 结果 |
| --- | --- |
| 公网 `/healthz` | HTTP 200 |
| 公网授权服务器元数据 | HTTP 200，全部 URL 指向新的公网地址 |
| 公网受保护资源元数据 | HTTP 200 |
| 公网未授权 `/mcp` | HTTP 401，携带 OAuth 元数据地址 |
| 公网动态客户端注册 | HTTP 201 |
| 公网授权跳转 | 正确跳转 GitHub，使用新的 GitHub callback |
| ChatGPT 实际 OAuth | DCR 201、GitHub Callback 302、Token 兑换 200；已生成绑定当前 MCP 的访问令牌和刷新令牌 |
| 公网 MCP 发现 | 17 个工具，通过现有私有 Gateway bearer token 验证 |
| 公网执行工具 | `printf funnel-ok` 成功 |
| 相关回归测试 | 当前源码 185 项通过，另有 5 个子测试通过 |
| 代码静态检查 | 通过本机 Ruff 检查 |
| 当前权限 | Gateway 实际调用确认 full access / network access 均为 true，workspace 为 `/` |
| 原生任务闭环 | Codex → MCP 模型响应 → completed 验证通过 |

最新状态检查已确认真实账号完成 GitHub 回调和 OAuth Token 兑换。公网 17 个工具
的独立验证使用私有 Gateway bearer token；在 ChatGPT 中使用连接器的首次实际
开发任务仍应单独验证。离线回归测试中的 GitHub 身份接口使用模拟数据。

## 在 ChatGPT 使用连接

在 ChatGPT 的自定义 MCP 连接入口选择 OAuth，并填写上述 `/mcp` URL。
服务器提供动态客户端注册。若界面要求手动指定已有客户端，可使用
`chatgpt-mcp-bridge`，客户端认证方式为 `none`；它不使用客户端 Secret。
可用 scopes 为 `mcp offline_access`。

连接器显示名称为 **ChatGPT MCP Bridge**。当前已完成 GitHub 授权；在新对话中
启用连接器，确认能发现 17 个工具，然后测试一个只读命令。
普通浏览器访问 `/mcp` 收到 401 是预期的未授权状态；根地址 `/` 没有网页。

## 运维位置

- Conda 安装：`/opt/anaconda3/envs/chatgpt-mcp-bridge`。
- 私有 OAuth 配置：`~/.config/ssh-mcp/github-oauth.env`。
- Mac Gateway 启动器：`~/.config/ssh-mcp/start-gateway-macos.sh`。
- 服务日志：`~/Library/Logs/chatgpt-mcp-bridge/`。
- 服务定义：`~/Library/LaunchAgents/com.double.chatgpt-mcp-bridge.*.plist`。
- 自启动安装脚本：`deploy/install-macos-launchagents.py`。

后台服务使用 Conda 内的安装包，不依赖 launchd 访问 Documents 下的源码目录。
后续修改源码时，在 Conda 中重新安装更新的包并重启 Gateway；无需重编译原生
执行程序或重新创建环境。Mac 仍需保持开机、联网和 Tailscale 已连接。
