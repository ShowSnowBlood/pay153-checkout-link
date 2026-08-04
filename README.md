# PAY.153 Checkout Link Router

<p align="center">
  <strong>多支付通道提链控制台。</strong><br>
  Hosted、PayPal、iDEAL、UPI、PIX、Team 与 Codex 空间方案统一任务化处理。
</p>

<p align="center">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white">
  <img alt="Flask" src="https://img.shields.io/badge/Backend-Flask-000000?logo=flask&logoColor=white">
  <img alt="curl_cffi" src="https://img.shields.io/badge/HTTP-curl__cffi-0EA5E9">
  <img alt="Repository" src="https://img.shields.io/badge/Repository-Private-111827">
</p>

## 页面预览

### 桌面端

![桌面端页面](docs/screenshots/desktop.png)

### 手机端

<p align="center">
  <img src="docs/screenshots/mobile.png" alt="手机端页面" width="390">
</p>

## 功能概览

- 支持 Plus、Pro、Team 与 Codex 低价空间等计划参数。
- 支持 Hosted 官方长链、PayPal、iDEAL、UPI 和 PIX 支付路径。
- 支持 Access Token 和 Session JSON 自动识别。
- 双代理池、1–500 条代理、本地保存、代理检测和地区自适应。
- 按支付地区自动选择币种，并处理不受支持币种的回退逻辑。
- 支持优惠更新、金额重新校验与零元账单判断。
- 支持失败重试，每轮重建 Checkout、设备标识和支付参数。
- 提供全局 RPM、单 IP RPM、并发限制和任务排队。
- 支持停止任务、进度展示、精简前端日志和完整后台日志。
- 支持支付二维码、跳转链接、倒计时和结果复制。
- 深色/浅色主题，以及桌面端和手机端响应式布局。

## 支付路径

| 路径 | 用途 |
|---|---|
| Hosted | 返回官方 Checkout 长链 |
| PayPal | 创建 PayPal PaymentMethod 并返回 Approve 跳转 |
| iDEAL | 荷兰银行支付路径 |
| UPI | 印度 UPI 支付与二维码 |
| PIX | 巴西 PIX 支付与二维码 |

## 项目结构

```text
pay153-checkout-link/
├─ app.py                       # Flask API、任务队列、限流与入口
├─ provider_checkout.py         # Checkout、地区、账单与支付提供商流程
├─ stripe_checkout.py           # Stripe 初始化、金额、确认与跳转处理
├─ billing_address_resolver.py  # 在线地图及账单地址解析
├─ sentinel_token.py            # Sentinel Token 生成与请求封装
├─ sentinel_sdk_full.js         # Sentinel SDK/VM 辅助代码
├─ gen_token_jsdom.js           # Node/JSDOM Token 辅助脚本
├─ static/
│  ├─ index.html                # 提链控制台
│  ├─ app.js                    # 前端任务与交互逻辑
│  └─ styles.css                # 主界面样式
├─ docs/screenshots/            # README 截图
├─ requirements.txt
└─ .env.example
```

## 快速启动

```bash
git clone https://github.com/1537271403/pay153-checkout-link.git
cd pay153-checkout-link

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

python app.py
```

生产环境推荐使用 Gunicorn：

```bash
gunicorn --workers 1 --threads 12 --timeout 600 \
  --bind 127.0.0.1:18096 app:app
```

## 环境变量

复制示例文件：

```bash
cp .env.example .env
```

| 变量 | 作用 |
|---|---|
| `GOOGLE_MAPS_API_KEY` | 在线地图地址解析密钥，可选 |
| `PAY153_BILLING_ADDRESS_CACHE` | 地址缓存文件路径 |
| `PAY153_WORKERS` | 后台任务并发数 |
| `PAY153_GLOBAL_RPM` | 全局每分钟创建任务上限 |
| `PAY153_IP_RPM` | 单 IP 每分钟任务上限 |
| `PAY153_LOG_DIR` | 完整后台日志目录 |
| `PAY153_LEGACY_BASE` | 旧服务兼容地址，可选 |
| `PAY153_PROXY_PROBE_COUNT` | 每个动态代理池批次生成和探测的候选数，默认 6 |
| `PAY153_PROXY_PROBE_TIMEOUT` | 单项代理网络探测超时秒数，默认 8 |
| `PAY153_PROXY_PROBE_TTL` | 固定代理深度探测结果缓存秒数，默认 300 |

## 代理池

每行一条代理，支持常见格式：

```text
host:port:username:password
http://username:password@host:port
https://username:password@host:port
socks5://username:password@host:port
socks5://host:port:username:password
http://host:port:username:password
```

`rp.scrapegw.com:6060` 使用 HTTP 协议。直接填写供应商的四段格式时，服务会自动补充
`-session-__rotate__-lifetime-120`，并按 UPI/iDEAL/PIX 写入 IN/NL/BR 国家参数。

动态网关可以把用户名中的 session 值写成 `__rotate__`：

```text
http://username-session-__rotate__-lifetime-120:password@gateway:port
```

每轮任务会生成一批真实 sticky session，并并发验证出口 IP、国家、OpenAI 后端和 Stripe。
系统会按支付方式自动插入或改写网关用户名中的国家参数：iDEAL=`country-nl`、UPI=`country-in`、
PIX=`country-br`，并预留 Kakao=`country-kr`。当前版本尚未实现 Kakao 支付协议。
重复出口会去重，失败候选进入指数冷却。
选中的入口/支付代理会在完整 Checkout 尝试中复用同一 HTTP Session、Cookie、TLS/UA 指纹和设备标识。
代理凭据仅应通过网页或环境变量传入。

## 生产部署

systemd 示例：

```ini
[Unit]
Description=PAY.153 Checkout Link Service
After=network-online.target

[Service]
WorkingDirectory=/opt/pay153
EnvironmentFile=-/opt/pay153/.env
Environment=PAY153_WORKERS=20
Environment=PAY153_GLOBAL_RPM=20
Environment=PAY153_IP_RPM=3
ExecStart=/opt/pay153/.venv/bin/gunicorn --workers 1 --threads 12 --timeout 600 --bind 127.0.0.1:18096 app:app
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
```

Nginx 将域名请求反向代理至：

```text
http://127.0.0.1:18096
```

## 数据与密钥

仓库未包含以下生产数据：

- `.env` 与真实管理令牌
- 代理账号和密码
- Access Token、Session JSON 与账号池
- 任务结果、日志、截图缓存和历史备份
- `data/`、`logs/`、`backups/` 与虚拟环境

提交代码前应继续检查环境变量、调试输出和测试文件，避免将运行凭据写入 Git 历史。

## 与协议支付项目的关系

本仓库负责生成支付链接或二维码；PayPal BA 链生成后，可以交给独立的协议支付服务继续处理：

https://github.com/1537271403/paypal-agreement-protocol

## 更新流程

```bash
git pull
python -m pip install -r requirements.txt
sudo systemctl restart pay153
```

检查服务：

```bash
systemctl status pay153
curl http://127.0.0.1:18096/api/health
```
