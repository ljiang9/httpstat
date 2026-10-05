# httpstat

一次 HTTP 请求的时间去哪了？`httpstat` 用纯标准库手动建 socket，
逐阶段计时并画成 ASCII 瀑布图：

```
===== httpstat =====
最终 URL：http://127.0.0.1:18321/slow
状态码：200

  DNS 解析                  0.1 ms  ▏
  TCP 连接                  0.3 ms  ▏
  服务器处理（TTFB）        305.2 ms  ██████████████████████████████
  内容传输                  0.2 ms  ▏
  总计                    305.8 ms

下载字节：13
```

## 安装

零依赖，Python 3.10+：

```bash
python3 -m httpstat https://example.com
```

## 用法

```bash
httpstat https://example.com            # 画瀑布图
httpstat http://内网服务:8080/health     # http 明文也行
httpstat -X POST --data "a=1" https://api.example.com/things
httpstat --json https://example.com     # 机器可读
httpstat --no-proxy https://example.com # 忽略 *_proxy，直连
httpstat --no-tls https://example.com   # 改写为 http://（明文测试）
httpstat --timeout 5 https://slow.example.com
```

重定向（301/302/303/307/308，最多 5 跳）会自动跟随，
输出显示最终 URL 和重定向链。

## 阶段说明

| 阶段 | 含义 |
|---|---|
| DNS 解析 | `getaddrinfo` 耗时 |
| TCP 连接 | `connect()` 耗时 |
| 代理 CONNECT | 经代理时，与代理建隧道耗时（阶段会标注代理主机） |
| TLS 握手 | `do_handshake()` 耗时（仅 https） |
| 服务器处理（TTFB） | 请求发完 → 首字节到达 |
| 内容传输 | 首字节 → 最后一个字节 |

## 诚实说明

- **代理**：默认读取 `HTTPS_PROXY`/`HTTP_PROXY` 等环境变量走代理；
  此时 DNS/TCP 阶段是对代理的计时，输出会明确标注"经代理发出"。
  用 `--no-proxy` 强制直连——但在必须走代理才能出网的环境里，
  直连公网大概率连不上，这是环境限制不是 bug。
- **阶段归因是近似的**：比如代理场景下 TLS 仍是对目标站点的握手
  （CONNECT 隧道之后），但 TCP 时间是到代理的；数字真实，解读要结合场景。
- 单次采样，网络抖动会直接反映在数字上；看趋势请多次运行。
- 只读响应、不校验业务语义；`Connection: close` 保证读到 EOF 即结束。
