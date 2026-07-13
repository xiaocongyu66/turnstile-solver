# HF Space：代理协议转换（含 Hysteria2）

HF solver **已内置** sing-box 中继，可把分享链接转成本地 `http://127.0.0.1:PORT`，再给 Chromium / CF-Ares / Playwright 使用。

## 支持的格式（`PROXY_POOL`）

| 格式 | 示例 |
|------|------|
| HTTP | `http://user:pass@host:port` |
| SOCKS5 | `socks5h://user:pass@host:port` |
| 简写 | `host:port` → `http://host:port` |
| Hysteria2 | `hysteria2://uuid@host:port?sni=localhost&insecure=1#HK-1` |
| Hysteria2 短写 | `hy2://...` |
| VMess / VLESS / Trojan / SS | `vmess://` `vless://` `trojan://` `ss://` |
| Base64 订阅 | 整段 base64（解码后多行分享链接） |

多条用 **换行** 或 **逗号** 分隔。

## 环境变量（HF Secrets）

```text
PROXY_POOL=<见下方节点列表或 base64>
PROXY_RELAY_ENABLED=1
PROXY_RELAY_AUTO_INSTALL=1
PROXY_POOL_STRATEGY=round_robin
PROXY_TEST_ENABLED=1
PROXY_TEST_URLS=https://challenges.cloudflare.com/turnstile/v0/api.js,https://accounts.x.ai/sign-up?redirect=grok-com
# 可选：测活全挂则不用代理
# PROXY_TEST_REQUIRE_OK=1
```

启动日志应出现：

```text
[proxy-pool] relay ready (sing-box ...)
[proxy-pool] relayed → http://127.0.0.1:19080 (hysteria2://...)
[proxy-pool] testing N proxy(ies) → xAI
```

## 操作步骤

1. Space → **Settings → Variables and secrets**
2. 新建 Secret：`PROXY_POOL`
3. 粘贴 **明文 hysteria2 多行**（推荐，见 `hf-proxy-pool-hysteria2.txt`）  
   或粘贴 **整段 base64 订阅**
4. 确认 `PROXY_RELAY_ENABLED=1`
5. **Restart** Space（不必每次 rebuild，改 Secret 后重启即可）

## 说明

- 浏览器不能直接用 `hysteria2://`，必须经 **sing-box mixed → 本地 HTTP**。
- 首次启动会自动下载 `sing-box`（需容器能访问 GitHub releases）。
- 节点过多会占本地端口；默认 `PROXY_RELAY_MAX_NODES=48`。
