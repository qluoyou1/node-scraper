#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
免费节点抓取脚本
- 来源1: 公开免费节点发布站网页（正则提取节点 URI）
- 来源3: Telegram 公开频道（t.me/s/<频道> 免登录预览，提取消息中的节点 URI
        与站外链接，并对疑似节点发布页做一层跟进抓取）
- 输出: output/nodes.txt（原始节点链接）、output/subscription.txt（base64 订阅）、
        output/clash.yaml（Clash/Mihomo 可直接导入的配置）
用法: python3 scraper.py [-c sources.yaml] [-o output]
"""

import argparse
import base64
import binascii
import html
import json
import re
import sys
import time
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

import requests
import yaml

HERE = Path(__file__).resolve().parent

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# 支持的节点协议（URI 形式）
NODE_SCHEMES = ("ss", "ssr", "vmess", "vless", "trojan",
                "hysteria", "hysteria2", "hy2", "tuic", "wireguard", "socks")
URI_RE = re.compile(
    r"\b(?:%s)://[^\s<\"'`)\\\]]+" % "|".join(NODE_SCHEMES))

# TG 消息里常见的"节点发布页"域名特征（跟进抓取用）
NODE_PAGE_HINTS = ("clash", "v2ray", "node", "freenode", "ssr", "proxy",
                   "github.io", "pages.dev", "vercel.app")


# ---------------------------------------------------------------- 抓取
def fetch(url, timeout=25):
    r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
    r.raise_for_status()
    # 有些订阅文件是纯文本无 charset，按内容嗅探
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding or "utf-8"
    return r.text


def render_url_template(tpl):
    """支持 {today:%Y%m%d} / {today_nopad}（如 2026-1-5）两种日期模板。"""
    now = datetime.now()
    out = tpl
    for m in re.finditer(r"\{today:([^}]+)\}", tpl):
        out = out.replace(m.group(0), now.strftime(m.group(1)))
    out = out.replace("{today_nopad}", f"{now.year}-{now.month}-{now.day}")
    return out


def try_dates(tpl, days_back=3):
    """按日期模板依次尝试最近几天，返回 (url, text)。"""
    now = datetime.now()
    last_err = None
    for d in range(days_back):
        day = now - timedelta(days=d)
        url = tpl
        for m in re.finditer(r"\{today:([^}]+)\}", tpl):
            url = url.replace(m.group(0), day.strftime(m.group(1)))
        url = url.replace("{today_nopad}", f"{day.year}-{day.month}-{day.day}")
        try:
            return url, fetch(url)
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise RuntimeError(f"连续 {days_back} 天的日期 URL 均失败: {tpl} ({last_err})")


def extract_uris(text):
    """从任意文本中提取节点 URI。"""
    text = html.unescape(text)
    found = []
    for m in URI_RE.finditer(text):
        uri = m.group(0).rstrip(".,;!?")
        # 过滤掉明显是示例/占位的内容
        if "example.com" in uri or "{password}" in uri:
            continue
        found.append(uri)
    return found


# ------------------------------------------------------- Telegram 频道
def scrape_tg_channel(channel, pages=4, max_follow_pages=8):
    """抓取 t.me/s/<channel> 公开预览（免登录、无需 API）。

    翻 pages 页取最新消息（每页约 20 条），提取：
    - 消息中的节点 URI
    - 消息中的订阅文件链接（.txt/.yaml/.yml）-> 直接按订阅抓取
    - 消息中指向节点发布站的链接 -> 跟进抓取一层
    """
    uris, proxies = [], []
    seen_subs, seen_pages = set(), set()
    before = None
    all_texts = []
    for _ in range(pages):
        url = f"https://t.me/s/{channel}" + (f"?before={before}" if before else "")
        try:
            raw = fetch(url)
        except Exception as e:  # noqa: BLE001
            print(f"    [tg:{channel}] 预览页抓取失败: {e}")
            break
        ids = re.findall(r'data-post="[^/]+/(\d+)"', raw)
        texts = re.findall(
            r'class="[^"]*tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
            raw, re.S)
        if not texts:
            break
        all_texts.extend(texts)
        before = min(ids, key=int) if ids else None
        if not before:
            break
        time.sleep(1)

    for t in all_texts:
        t = html.unescape(t)
        uris.extend(extract_uris(t))
        for sub in extract_subscription_links(t):
            if sub not in seen_subs:
                seen_subs.add(sub)
        for link in set(re.findall(r'href="(https?://[^"]+)"', t)):
            if "t.me/" in link or "telegram.org" in link:
                continue
            low = link.lower()
            if any(h in low for h in NODE_PAGE_HINTS) and link not in seen_pages:
                seen_pages.add(link)

    # 先抓订阅链接（通常最新最全）
    for sub in list(seen_subs)[:max_follow_pages]:
        try:
            u, p = fetch_subscription(sub)
            uris.extend(u)
            proxies.extend(p)
            print(f"    [tg:{channel}] 订阅: {sub[:70]} -> {len(u)} 节点")
        except Exception as e:  # noqa: BLE001
            print(f"    [tg:{channel}] 订阅失败: {sub[:70]} ({e})")
    # 再跟进节点发布页（不跟进页内订阅链接，避免扩散抓取）
    for link in list(seen_pages)[:max_follow_pages]:
        try:
            u, p = scrape_node_page(link, max_sub_links=0)
            uris.extend(u)
            proxies.extend(p)
            print(f"    [tg:{channel}] 跟进页面: {link[:70]} (+{len(u)} 节点)")
        except Exception as e:  # noqa: BLE001
            print(f"    [tg:{channel}] 跟进页面失败: {link[:70]} ({e})")
    return uris, proxies


# ------------------------------------------------------- 订阅链接
SUB_LINK_RE = re.compile(
    r"https?://[^\s<\"'`)\\\]]+?\.(?:txt|yaml|yml)(?:\?[^\s<\"'`)\\\]]*)?",
    re.I)


def extract_subscription_links(text):
    """从页面文本中找出疑似订阅文件的 URL。"""
    links = []
    for m in SUB_LINK_RE.finditer(html.unescape(text)):
        u = m.group(0)
        if "t.me" in u:
            continue
        if u not in links:
            links.append(u)
    return links


def scrape_node_page(url, max_sub_links=4):
    """抓取节点发布页：直接提取节点 URI，并跟进页面内的订阅文件链接。"""
    text = fetch(url)
    uris = extract_uris(text)
    proxies = []
    for sub in extract_subscription_links(text)[:max_sub_links]:
        try:
            u, p = fetch_subscription(sub)
            uris.extend(u)
            proxies.extend(p)
            print(f"    [page-sub] {sub[:80]} -> {len(u)} 节点, {len(p)} clash代理")
        except Exception as e:  # noqa: BLE001
            print(f"    [page-sub] {sub[:80]} 失败: {e}")
    return uris, proxies


def fetch_subscription(url):
    """抓取订阅 URL，返回 (node_uris, clash_proxy_dicts)。"""
    text = fetch(url).strip()
    if not text:
        return [], []
    # Clash YAML 订阅
    if text.lstrip().startswith(("proxies:", "mixed-port", "port:")) or "\nproxies:" in text[:2000]:
        try:
            data = yaml.safe_load(text)
            proxies = data.get("proxies") if isinstance(data, dict) else None
            return [], proxies if isinstance(proxies, list) else []
        except Exception:  # noqa: BLE001
            pass
    # base64 订阅
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) == 1 and re.fullmatch(r"[A-Za-z0-9+/=_-]+", lines[0]):
        try:
            padded = lines[0] + "=" * (-len(lines[0]) % 4)
            text = base64.urlsafe_b64decode(padded).decode("utf-8", "ignore")
        except (binascii.Error, ValueError):
            pass
    return extract_uris(text), []


# ------------------------------------------------------- 去重
def dedupe_key(uri):
    """去掉 fragment 后作为去重键，保留第一次出现的备注名。"""
    try:
        scheme, rest = uri.split("://", 1)
        no_frag = rest.split("#", 1)[0]
        return scheme.lower() + "://" + no_frag
    except ValueError:
        return uri


# ------------------------------------------------------- URI -> Clash
def _b64decode(s):
    s = s.strip().replace("-", "+").replace("_", "/")
    return base64.b64decode(s + "=" * (-len(s) % 4))


def parse_ss(uri):
    """ss:// -> clash proxy dict。兼容两种常见写法。"""
    body = uri[5:].split("#", 1)
    name = urllib.parse.unquote(body[1]) if len(body) > 1 else "ss"
    main = body[0]
    if "@" in main and not main.startswith(tuple("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=")):
        # 明文 userinfo 形式少见，统一走 base64 逻辑
        pass
    if "@" in main:
        userinfo_b64, hostport = main.rsplit("@", 1)
        try:
            method, password = _b64decode(userinfo_b64).decode("utf-8").split(":", 1)
        except Exception:  # noqa: BLE001
            return None
    else:
        try:
            decoded = _b64decode(main).decode("utf-8")
        except Exception:  # noqa: BLE001
            return None
        m = re.match(r"^(?P<method>[^:]+):(?P<pw>[^@]+)@(?P<host>[^:]+):(?P<port>\d+)", decoded)
        if not m:
            return None
        method, password = m.group("method"), m.group("pw")
        hostport = f"{m.group('host')}:{m.group('port')}"
    host, _, port = hostport.partition(":")
    try:
        port = int(port)
    except ValueError:
        return None
    return {"name": name, "type": "ss", "server": host, "port": port,
            "cipher": method, "password": password, "udp": True}


def parse_vmess(uri):
    try:
        data = json.loads(_b64decode(uri[8:].split("#", 1)[0]).decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None
    try:
        port = int(data.get("port", 0))
    except (TypeError, ValueError):
        return None
    proxy = {"name": data.get("ps") or "vmess", "type": "vmess",
             "server": data.get("add"), "port": port,
             "uuid": data.get("id"), "alterId": int(data.get("aid", 0) or 0),
             "cipher": data.get("scy", "auto") or "auto", "udp": True}
    net = (data.get("net") or "tcp").lower()
    if net != "tcp":
        proxy["network"] = net
    if data.get("path"):
        proxy["ws-opts"] = {"path": data["path"],
                            "headers": {"Host": data.get("host") or ""}}
    elif data.get("host"):
        proxy["ws-opts"] = {"headers": {"Host": data["host"]}}
    tls = str(data.get("tls", "")).lower()
    if tls == "tls":
        proxy["tls"] = True
        if data.get("sni"):
            proxy["servername"] = data["sni"]
    return proxy if proxy["server"] and proxy["uuid"] else None


def _parse_query_params(uri):
    parts = urllib.parse.urlsplit(uri)
    q = urllib.parse.parse_qs(parts.query)
    name = urllib.parse.unquote(parts.fragment) if parts.fragment else ""
    return parts, {k: v[0] for k, v in q.items()}, name


def parse_vless(uri):
    parts, q, name = _parse_query_params(uri)
    try:
        port = int(parts.port or 0)
    except ValueError:
        return None
    if not parts.hostname or not port:
        return None
    proxy = {"name": name or "vless", "type": "vless",
             "server": parts.hostname, "port": port,
             "uuid": urllib.parse.unquote(parts.username or ""), "udp": True}
    net = (q.get("type") or "tcp").lower()
    if net != "tcp":
        proxy["network"] = net
    if net == "ws":
        proxy["ws-opts"] = {"path": q.get("path", "/"),
                            "headers": {"Host": q.get("host", "")}}
    elif net == "grpc":
        proxy["grpc-opts"] = {"grpc-service-name": q.get("serviceName", "")}
    if (q.get("security") or "").lower() == "tls":
        proxy["tls"] = True
        if q.get("sni"):
            proxy["servername"] = q["sni"]
        if q.get("fp"):
            proxy["client-fingerprint"] = q["fp"]
    elif (q.get("security") or "").lower() == "reality":
        proxy["tls"] = True
        proxy["reality-opts"] = {"public-key": q.get("pb", ""),
                                "short-id": q.get("sid", "")}
        if q.get("sni"):
            proxy["servername"] = q["sni"]
        if q.get("fp"):
            proxy["client-fingerprint"] = q["fp"]
    if q.get("flow"):
        proxy["flow"] = q["flow"]
    return proxy if proxy["uuid"] else None


def parse_trojan(uri):
    parts, q, name = _parse_query_params(uri)
    try:
        port = int(parts.port or 0)
    except ValueError:
        return None
    if not parts.hostname or not port:
        return None
    proxy = {"name": name or "trojan", "type": "trojan",
             "server": parts.hostname, "port": port,
             "password": urllib.parse.unquote(parts.username or ""),
             "udp": True}
    net = (q.get("type") or "tcp").lower()
    if net != "tcp":
        proxy["network"] = net
    if net == "ws":
        proxy["ws-opts"] = {"path": q.get("path", "/"),
                            "headers": {"Host": q.get("host", "")}}
    elif net == "grpc":
        proxy["grpc-opts"] = {"grpc-service-name": q.get("serviceName", "")}
    if (q.get("security") or "").lower() == "tls":
        proxy["tls"] = True
        if q.get("sni"):
            proxy["sni"] = q["sni"]
        if q.get("fp"):
            proxy["client-fingerprint"] = q["fp"]
    if q.get("allowInsecure") == "1":
        proxy["skip-cert-verify"] = True
    if q.get("alpn"):
        proxy["alpn"] = q["alpn"].split(",")
    return proxy if proxy["password"] else None


PARSERS = {"ss": parse_ss, "vmess": parse_vmess, "vless": parse_vless,
           "trojan": parse_trojan}


def uri_to_clash(uri):
    scheme = uri.split("://", 1)[0].lower()
    fn = PARSERS.get(scheme)
    if not fn:
        return None
    try:
        return fn(uri)
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- 主流程
def load_sources(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def main():
    ap = argparse.ArgumentParser(description="免费节点抓取脚本")
    ap.add_argument("-c", "--config", default=str(HERE / "sources.yaml"))
    ap.add_argument("-o", "--output", default=str(HERE / "output" / "raw"),
                    help="原始抓取结果目录（供 geoip_rename.py 进一步处理）")
    args = ap.parse_args()

    outdir = Path(args.output)
    outdir.mkdir(parents=True, exist_ok=True)
    cfg = load_sources(args.config)

    uris, yaml_proxies = [], []
    stats = {}

    # 1) 网页来源
    for tpl in cfg.get("web_pages") or []:
        label = tpl
        try:
            if "{today" in tpl:
                url, _ = try_dates(tpl)
            else:
                url = tpl
            got, got_proxies = scrape_node_page(url)
            uris.extend(got)
            yaml_proxies.extend(got_proxies)
            stats[label] = f"OK, {len(got)} 个节点 + {len(got_proxies)} 个 clash 代理"
            print(f"[page] {url[:80]} -> {len(got)} 个节点, {len(got_proxies)} clash代理")
        except Exception as e:  # noqa: BLE001
            stats[label] = f"失败: {e}"
            print(f"[page] {label[:80]} 失败: {e}")

    # 2) 订阅链接来源
    for tpl in cfg.get("subscriptions") or []:
        try:
            url = render_url_template(tpl)
            u, p = fetch_subscription(url)
            uris.extend(u)
            yaml_proxies.extend(p)
            stats[tpl] = f"OK, {len(u)} 个节点 + {len(p)} 个 clash 代理"
            print(f"[sub ] {url[:80]} -> {len(u)} 节点, {len(p)} clash代理")
        except Exception as e:  # noqa: BLE001
            stats[tpl] = f"失败: {e}"
            print(f"[sub ] {tpl[:80]} 失败: {e}")

    # 3) Telegram 频道
    for ch in cfg.get("tg_channels") or []:
        try:
            got, got_proxies = scrape_tg_channel(ch)
            uris.extend(got)
            yaml_proxies.extend(got_proxies)
            stats[f"tg:{ch}"] = f"OK, {len(got)} 个节点 + {len(got_proxies)} 个 clash 代理"
            print(f"[tg  ] {ch} -> {len(got)} 个节点, {len(got_proxies)} clash代理")
        except Exception as e:  # noqa: BLE001
            stats[f"tg:{ch}"] = f"失败: {e}"
            print(f"[tg  ] {ch} 失败: {e}")

    # 去重（保留首次出现的备注名）
    seen, uniq_uris = set(), []
    for u in uris:
        k = dedupe_key(u)
        if k not in seen:
            seen.add(k)
            uniq_uris.append(u)
    print(f"\n去重后节点 URI: {len(uniq_uris)}（原始 {len(uris)}）")

    # 输出 1: nodes.txt
    (outdir / "nodes.txt").write_text("\n".join(uniq_uris) + "\n", encoding="utf-8")

    # 输出 2: subscription.txt（base64）
    b64 = base64.b64encode(("\n".join(uniq_uris)).encode()).decode()
    (outdir / "subscription.txt").write_text(b64, encoding="utf-8")

    # 输出 3: clash.yaml
    proxies, skipped = [], 0
    for u in uniq_uris:
        p = uri_to_clash(u)
        if p:
            proxies.append(p)
        else:
            skipped += 1
    proxies.extend(yaml_proxies)
    # 代理名去重（clash 要求 name 唯一）
    names, final = set(), []
    for p in proxies:
        n = str(p.get("name") or "proxy")
        i, base = 2, n
        while n in names:
            n = f"{base}-{i}"
            i += 1
        p["name"] = n
        names.add(n)
        final.append(p)
    proxy_names = [p["name"] for p in final]
    clash_cfg = {
        "mixed-port": 7890,
        "allow-lan": True,
        "mode": "rule",
        "log-level": "info",
        "proxies": final,
        "proxy-groups": [
            {"name": "自动选择", "type": "url-test", "proxies": proxy_names,
             "url": "http://www.gstatic.com/generate_204", "interval": 300},
            {"name": "手动选择", "type": "select", "proxies": proxy_names},
            {"name": "PROXY", "type": "select",
             "proxies": ["自动选择", "手动选择", "DIRECT"]},
        ],
        "rules": ["MATCH,PROXY"],
    }
    with open(outdir / "clash.yaml", "w", encoding="utf-8") as f:
        yaml.safe_dump(clash_cfg, f, allow_unicode=True, sort_keys=False)

    print(f"clash.yaml: {len(final)} 个代理（URI 转换 {len(final)-len(yaml_proxies)}，"
          f"订阅自带 {len(yaml_proxies)}，{skipped} 个 URI 协议暂不支持转换）")
    print(f"\n输出目录: {outdir}")
    print(f"  nodes.txt        原始节点链接（{len(uniq_uris)} 个）")
    print(f"  subscription.txt base64 订阅（可直接导入客户端）")
    print(f"  clash.yaml       Clash/Mihomo 配置（含自动选择/手动选择分组）")

    (outdir / "report.txt").write_text(
        f"抓取时间: {datetime.now():%Y-%m-%d %H:%M:%S}\n"
        f"去重后节点: {len(uniq_uris)}\nclash 代理: {len(final)}\n\n"
        + "\n".join(f"- {k}: {v}" for k, v in stats.items()),
        encoding="utf-8")

    # 订阅自带的 clash 代理另存（无 URI 形式），供 geoip_rename.py 配对使用
    import json as _json
    (outdir / "_yaml_proxies.json").write_text(
        _json.dumps(yaml_proxies, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
