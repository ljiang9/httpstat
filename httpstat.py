#!/usr/bin/env python3
"""httpstat — 把一次 HTTP 请求的时间画成瀑布图。

纯标准库实现：手动建 socket，逐阶段计时
（DNS 解析 / TCP 连接 / TLS 握手 / 服务器处理 TTFB / 内容传输），
不经过 urllib 的黑盒，看到的数字就是真实发生的。
"""
import argparse
import json
import os
import socket
import ssl
import sys
import time
from urllib.parse import urlsplit, urljoin

VERSION = "0.1.0"
MAX_REDIRECTS = 5
BAR_WIDTH = 30


class HttpstatError(Exception):
    pass


def pick_proxy(scheme):
    """按惯例读取 *_proxy 环境变量。返回 (proxy_url, ) 或 None。"""
    for var in ("HTTPS_PROXY", "https_proxy") if scheme == "https" else ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        val = os.environ.get(var)
        if val:
            return val
    if os.environ.get("NO_PROXY") or os.environ.get("no_proxy"):
        return None
    return None


def timed(label, fn):
    t0 = time.perf_counter()
    result = fn()
    return result, (time.perf_counter() - t0) * 1000.0


def recv_all(sock, timeout):
    sock.settimeout(timeout)
    chunks = []
    first_at = None
    t_send_done = time.perf_counter()
    while True:
        try:
            data = sock.recv(65536)
        except socket.timeout:
            raise HttpstatError("读取响应超时")
        if not data:
            break
        if first_at is None:
            first_at = time.perf_counter()
        chunks.append(data)
    t_end = time.perf_counter()
    body = b"".join(chunks)
    ttfb = (first_at - t_send_done) * 1000.0 if first_at is not None else 0.0
    transfer = (t_end - (first_at or t_send_done)) * 1000.0
    return body, ttfb, transfer


def split_head_body(raw):
    idx = raw.find(b"\r\n\r\n")
    if idx == -1:
        raise HttpstatError("响应头不完整")
    head = raw[:idx].decode("latin-1")
    body = raw[idx + 4:]
    lines = head.split("\r\n")
    status = int(lines[0].split()[1])
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return status, headers, body


def do_request(url, method, data, timeout, use_proxy):
    """执行一次请求（含重定向跟随），返回结果 dict。"""
    redirects = []
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        res = single_request(current, method, data, timeout, use_proxy)
        if res["status"] in (301, 302, 303, 307, 308) and res["headers"].get("location"):
            redirects.append((res["status"], current))
            nxt = urljoin(current, res["headers"]["location"])
            if res["status"] == 303 or (res["status"] in (301, 302) and method == "POST"):
                method, data = "GET", None
            current = nxt
            continue
        res["redirects"] = redirects
        res["final_url"] = current
        return res
    raise HttpstatError(f"重定向超过 {MAX_REDIRECTS} 次")


def single_request(url, method, data, timeout, use_proxy):
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise HttpstatError(f"只支持 http/https，收到：{parts.scheme or url}")
    host = parts.hostname
    if not host:
        raise HttpstatError(f"URL 解析不出主机名：{url}")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query

    proxy_url = pick_proxy(parts.scheme) if use_proxy else None
    via_proxy = proxy_url is not None
    if via_proxy:
        pp = urlsplit(proxy_url)
        dial_host, dial_port = pp.hostname, pp.port or 8080
    else:
        dial_host, dial_port = host, port

    # 1) DNS（getaddrinfo 本身没有超时参数，用线程包一层，超时则报错）
    def _dns():
        import threading
        box, err = {}, {}
        def _run():
            try:
                box["infos"] = socket.getaddrinfo(dial_host, dial_port,
                                                  type=socket.SOCK_STREAM)
            except socket.gaierror as e:
                err["e"] = e
        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            raise HttpstatError(f"DNS 解析超时（>{timeout:g}s）：{dial_host}")
        if "e" in err:
            raise HttpstatError(f"DNS 解析失败：{dial_host}")
        return box["infos"][0]
    (family, socktype, proto, _, sockaddr), dns_ms = timed("dns", _dns)

    # 2) TCP
    def _tcp():
        s = socket.socket(family, socktype, proto)
        s.settimeout(timeout)
        try:
            s.connect(sockaddr)
        except (socket.timeout, OSError) as e:
            raise HttpstatError(f"无法连接 {dial_host}:{dial_port}：{e.strerror or e}")
        return s
    sock, tcp_ms = timed("tcp", _tcp)

    try:
        # 2b) 代理 CONNECT 隧道（仅 https 经代理时）
        proxy_ms = 0.0
        if via_proxy and parts.scheme == "https":
            req = (f"CONNECT {host}:{port} HTTP/1.1\r\n"
                   f"Host: {host}:{port}\r\n\r\n").encode()
            t0 = time.perf_counter()
            sock.sendall(req)
            resp = b""
            while b"\r\n\r\n" not in resp:
                chunk = sock.recv(4096)
                if not chunk:
                    raise HttpstatError("代理 CONNECT 被拒绝")
                resp += chunk
            proxy_ms = (time.perf_counter() - t0) * 1000.0
            code = int(resp.split(b" ", 2)[1])
            if code != 200:
                raise HttpstatError(f"代理 CONNECT 失败：HTTP {code}")

        # 3) TLS
        tls_ms = 0.0
        if parts.scheme == "https":
            ctx = ssl.create_default_context()
            def _tls():
                ss = ctx.wrap_socket(sock, server_hostname=host,
                                     do_handshake_on_connect=False)
                ss.do_handshake()
                return ss
            sock, tls_ms = timed("tls", _tls)

        # 4) 发请求
        body = b"" if data is None else (data.encode() if isinstance(data, str) else data)
        lines = [f"{method} {path} HTTP/1.1",
                 f"Host: {host}",
                 "User-Agent: httpstat/0.1.0",
                 "Accept-Encoding: identity",
                 "Connection: close"]
        if body:
            lines.append(f"Content-Length: {len(body)}")
        raw_req = ("\r\n".join(lines) + "\r\n\r\n").encode() + body
        sock.sendall(raw_req)
        t_sent = time.perf_counter()

        # 5) 收响应：TTFB + 传输
        raw, ttfb_ms, transfer_ms = recv_all(sock, timeout)
    finally:
        try:
            sock.close()
        except OSError:
            pass

    status, headers, resp_body = split_head_body(raw)
    return {
        "url": url, "status": status, "headers": headers,
        "bytes": len(raw),
        "via_proxy": via_proxy, "proxy_host": dial_host if via_proxy else None,
        "phases_ms": {
            "dns": dns_ms, "tcp": tcp_ms,
            "proxy_connect": proxy_ms, "tls": tls_ms,
            "ttfb": ttfb_ms, "transfer": transfer_ms,
        },
    }


def render(result):
    p = result["phases_ms"]
    order = [("dns", "DNS 解析"), ("tcp", "TCP 连接")]
    if result["via_proxy"]:
        order.append(("proxy_connect", f"代理 CONNECT（{result['proxy_host']}）"))
    order += [("tls", "TLS 握手"), ("ttfb", "服务器处理（TTFB）"), ("transfer", "内容传输")]
    total = sum(p[k] for k, _ in order)
    scale = max((p[k] for k, _ in order), default=0) or 1

    print("===== httpstat =====")
    print(f"最终 URL：{result['final_url']}")
    if result["redirects"]:
        chain = " → ".join(f"{code} {u}" for code, u in result["redirects"])
        print(f"重定向链：{chain}")
    print(f"状态码：{result['status']}")
    if result["via_proxy"]:
        print("注：经代理发出，DNS/TCP 阶段是对代理的计时")
    print()
    for key, label in order:
        ms = p[key]
        bar = "█" * max(1, round(ms / scale * BAR_WIDTH)) if ms > 0.05 else "▏"
        print(f"  {label:18s} {ms:9.1f} ms  {bar}")
    print(f"  {'总计':18s} {total:9.1f} ms")
    print()
    print(f"下载字节：{result['bytes']}")


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="httpstat",
        description="把一次 HTTP 请求的时间画成瀑布图：DNS / TCP / TLS / TTFB / 传输。",
    )
    ap.add_argument("url", nargs="?", help="目标 URL（http/https）")
    ap.add_argument("-X", "--method", default="GET", help="HTTP 方法（默认 GET）")
    ap.add_argument("--data", default=None, help="请求体（配合 -X POST 等使用）")
    ap.add_argument("--timeout", type=float, default=15, help="超时秒数（默认 15）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--no-proxy", action="store_true", help="忽略 *_proxy 环境变量，直连目标")
    ap.add_argument("--no-tls", action="store_true", help="把 https:// 改写为 http://（明文测试）")
    ap.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    args = ap.parse_args(argv)

    if not args.url:
        ap.error("请提供 URL，例如：httpstat https://example.com")
    url = args.url
    if args.no_tls and url.startswith("https://"):
        url = "http://" + url[len("https://"):]

    try:
        result = do_request(url, args.method.upper(), args.data,
                            args.timeout, use_proxy=not args.no_proxy)
    except HttpstatError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        render(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
