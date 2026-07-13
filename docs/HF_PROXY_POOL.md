# HF Space：独立 sing-box 代理服务（全局 + 协议转换）

基于 [SagerNet/sing-box](https://github.com/SagerNet/sing-box) 单独拉起 **proxy-service**：

- 多协议：`hysteria2` / `hy2` / `vmess` / `vless` / `trojan` / `ss` / `socks5` / `http`
- Base64 订阅或明文多行 `PROXY_POOL`
- **自定义 DNS**（`PROXY_SERVICE_DNS`）
- 本地 **mixed** 入口（HTTP + SOCKS 同一端口）
- **urltest** 自动选最快节点
- 应用到容器全局：`HTTP_PROXY` / `HTTPS_PROXY`（solver worker 跟随）

## HF Secrets 推荐

```text
# 节点（明文多行 或 整段 base64）
PROXY_POOL=<hysteria2://... 或 base64 订阅>

# 独立代理服务
PROXY_SERVICE_ENABLED=auto
PROXY_SERVICE_PORT=7890
PROXY_SERVICE_HOST=127.0.0.1
PROXY_SERVICE_DNS=1.1.1.1,8.8.8.8,8.8.4.4
PROXY_SERVICE_DNS_STRATEGY=prefer_ipv4
PROXY_SERVICE_MODE=urltest
PROXY_SERVICE_URLTEST_URL=https://www.gstatic.com/generate_204
PROXY_SERVICE_URLTEST_INTERVAL=3m
PROXY_SERVICE_APPLY_GLOBAL=1
PROXY_RELAY_AUTO_INSTALL=1
```

## 启动日志应看到

```text
🛰️  Starting sing-box proxy service (DNS=1.1.1.1,8.8.8.8 mode=urltest port=7890)...
[proxy-service] outbound n1_HK-1 type=hysteria2 ...
[proxy-service] ready HTTP/SOCKS mixed on 127.0.0.1:7890
   🌍 global proxy applied → http://127.0.0.1:7890
```

## 手动本地调试

```bash
export PROXY_POOL="$(cat docs/hf-proxy-pool-hysteria2.txt)"
export PROXY_SERVICE_DNS=1.1.1.1,8.8.8.8
export PROXY_SERVICE_PORT=7890
python worker/proxy_service.py print-config   # 看生成的 sing-box.json
python worker/proxy_service.py run           # 前台运行
# 另开终端
curl -x http://127.0.0.1:7890 -I https://accounts.x.ai/
```

## 说明

- 浏览器不能直接吃 `hysteria2://`，必须经 sing-box 转本地 HTTP。
- 首次启动自动下载 sing-box（需访问 GitHub Releases）。
- `PROXY_SERVICE_APPLY_GLOBAL=1` 时 gateway **保留** `HTTP_PROXY` 传给 browser-worker。
- 仍保留旧的 per-node relay（`proxy_pool`）作回退。
