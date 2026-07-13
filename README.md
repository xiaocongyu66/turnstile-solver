# Turnstile Solver (Hybrid)

独立 **Cloudflare Turnstile** 求解服务（原 `grok-free-register` 多语言 hybrid 栈拆出）。

| 层 | 语言 | 作用 |
|----|------|------|
| Gateway | Go | HTTP API、任务队列、worker 调度 |
| Watchdog | Rust | 内存 / RSS 守护 |
| Util | C++ | 压力分、token 形状校验 |
| Browser | Python | Playwright Chromium 求解 |

兼容 Theyka / D3-vin 风格 API，可供 `grok-free-register` 等注册机远程调用。

## API

```text
GET  /health                         → 始终 200（HF 探针）
GET  /turnstile?url=&sitekey=        → {"task_id":"...","id":"..."}
GET  /result?id=                     → pending | success | fail
GET  /stats  /v1/memory
POST /v1/solve                       JSON {url, sitekey, action?, cdata?, proxy?}
```

鉴权（可选，公网务必开启）：

```text
SOLVER_API_TOKEN=your_secret
# 请求头其一：
Authorization: Bearer your_secret
X-API-Key: your_secret
```

## 本地运行

```bash
# 编译
bash scripts/build.sh

export PORT=5080 HOST=127.0.0.1
export SOLVER_GATEWAY_WORKERS=2
export SOLVER_API_TOKEN=dev-token   # 可选
./gateway/solver-gateway --host 127.0.0.1 --port 5080 --workers 2
```

测一下：

```bash
curl -sS "http://127.0.0.1:5080/health"
curl -sS -H "Authorization: Bearer dev-token" \
  "http://127.0.0.1:5080/turnstile?url=https://accounts.x.ai/sign-up&sitekey=0x4AAAAAAAhr9JGVDZbrZOo0"
# 轮询 /result?id=...
```

## Hugging Face Space

1. 新建 Space → **SDK: Docker**
2. Hardware：**≥ 2 vCPU · 16 GB**（Chromium）
3. 推送本仓库；或 Settings 里连到本 GitHub
4. Secrets：

```text
SOLVER_API_TOKEN=强随机串
SOLVER_GATEWAY_WORKERS=auto
SOLVER_GATEWAY_WORKERS_MAX=4
TURNSTILE_SOLVER_HEADLESS=1
```

5. 打开 `https://<space>.hf.space/health` 应返回 `ok`

## 给 grok-free-register 用

在注册机 `.env` / HF Secrets：

```text
TURNSTILE_SOLVER=api
TURNSTILE_API_URL=https://你的-solver.hf.space
# 若 solver 设了 token，注册侧需能带 Header（见下方）
```

当前注册机 HTTP 客户端若只认 URL 轮询，请：

- 把 solver 与注册放同一内网且 **不设 token**（仅内网），或  
- 后续在注册侧加 `TURNSTILE_API_TOKEN` 请求头支持。

本地同机：

```text
TURNSTILE_SOLVER=hybrid
TURNSTILE_API_URL=http://127.0.0.1:5080
```

也可继续用注册仓库内嵌 hybrid；本仓库是 **独立部署** 形态。

## 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `PORT` / `SOLVER_GATEWAY_PORT` | `7860` (HF) / `5080` | 监听端口 |
| `HOST` | `0.0.0.0` | 绑定地址 |
| `SOLVER_GATEWAY_WORKERS` | `auto` | 浏览器进程数 |
| `SOLVER_GATEWAY_WORKERS_MAX` | `4` | auto 上限 |
| `SOLVER_WORKER_CONCURRENCY` | `0` | 每浏览器页数，0=自动 |
| `SOLVER_WATCHDOG_SOFT_MB` | `700` | 软回收 |
| `SOLVER_WATCHDOG_HARD_MB` | `1100` | 硬回收 |
| `SOLVER_API_TOKEN` | 空 | 公网鉴权 |
| `TURNSTILE_SOLVER_HEADLESS` | `1` | 无头 |

## License

与上游项目一致用途限制：仅用于你有权测试的环境。
