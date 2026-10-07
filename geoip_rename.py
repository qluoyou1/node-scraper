#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
节点重命名：将节点名称改为「国家+AS服务商」格式，如：美国+Cloudflare
- 通过 DNS 解析服务器域名得到 IP（带缓存）
- 通过 ip-api.com 查询 IP 的国家与组织（带缓存，支持批量查询）
- 重写 output/clash.yaml 中的代理名，以及 output/nodes.txt 链接的 #备注
用法: python3 geoip_rename.py [-o output]
首次运行需查询约数百个 IP（约1分钟批量查询），结果缓存到 geoip_cache.json，
之后每天只查新增 IP，速度很快。
"""

import argparse
import base64
import binascii
import ipaddress
import json
import re
import socket
import time
import urllib.parse
from pathlib import Path

import requests
import yaml

HERE = Path(__file__).resolve().parent
CACHE_FILE = HERE / "geoip_cache.json"
IPAPI_BATCH = "http://ip-api.com/batch"
FIELDS = "status,message,country,countryCode,org,isp,query"

# 常见国家代码 -> 中文名（未列出的回退用英文原名）
COUNTRY_CN = {
    "US": "美国", "JP": "日本", "SG": "新加坡", "HK": "香港", "TW": "台湾",
    "KR": "韩国", "DE": "德国", "NL": "荷兰", "GB": "英国", "FR": "法国",
    "CA": "加拿大", "AU": "澳大利亚", "RU": "俄罗斯", "IN": "印度",
    "BR": "巴西", "ID": "印度尼西亚", "MY": "马来西亚", "TH": "泰国",
    "VN": "越南", "PH": "菲律宾", "TR": "土耳其", "AE": "阿联酋",
    "SA": "沙特", "UA": "乌克兰", "PL": "波兰", "SE": "瑞典", "FI": "芬兰",
    "NO": "挪威", "CH": "瑞士", "IT": "意大利", "ES": "西班牙", "MX": "墨西哥",
    "AR": "阿根廷", "CL": "智利", "CO": "哥伦比亚", "ZA": "南非", "EG": "埃及",
    "IL": "以色列", "KZ": "哈萨克斯坦", "UZ": "乌兹别克斯坦", "IR": "伊朗",
    "IQ": "伊拉克", "PK": "巴基斯坦", "BD": "孟加拉", "LK": "斯里兰卡",
    "NP": "尼泊尔", "KH": "柬埔寨", "LA": "老挝", "MM": "缅甸", "MN": "蒙古",
    "NZ": "新西兰", "IE": "爱尔兰", "AT": "奥地利", "BE": "比利时",
    "DK": "丹麦", "PT": "葡萄牙", "GR": "希腊", "CZ": "捷克", "HU": "匈牙利",
    "RO": "罗马尼亚", "BG": "保加利亚", "HR": "克罗地亚", "RS": "塞尔维亚",
    "LT": "立陶宛", "LV": "拉脱维亚", "EE": "爱沙尼亚", "IS": "冰岛",
    "LU": "卢森堡", "MC": "摩纳哥", "GI": "直布罗陀", "IM": "马恩岛",
    "PE": "秘鲁", "VE": "委内瑞拉", "EC": "厄瓜多尔", "BO": "玻利维亚",
    "UY": "乌拉圭", "PY": "巴拉圭", "CR": "哥斯达黎加", "PA": "巴拿马",
    "DO": "多米尼加", "JM": "牙买加", "GT": "危地马拉", "HN": "洪都拉斯",
    "NI": "尼加拉瓜", "SV": "萨尔瓦多", "CU": "古巴", "PR": "波多黎各",
    "MA": "摩洛哥", "DZ": "阿尔及利亚", "TN": "突尼斯", "NG": "尼日利亚",
    "KE": "肯尼亚", "GH": "加纳", "ET": "埃塞俄比亚", "MU": "毛里求斯",
    "SC": "塞舌尔", "QA": "卡塔尔", "KW": "科威特", "BH": "巴林",
    "OM": "阿曼", "JO": "约旦", "LB": "黎巴嫩", "CY": "塞浦路斯",
    "AZ": "阿塞拜疆", "GE": "格鲁吉亚", "AM": "亚美尼亚", "BY": "白俄罗斯",
    "MD": "摩尔多瓦", "KG": "吉尔吉斯斯坦", "TJ": "塔吉克斯坦", "TM": "土库曼斯坦",
    "CN": "中国",
}

CORP_SUFFIX = re.compile(
    r"\s+(Inc\.?|LLC|Ltd\.?|Limited|GmbH|SAS|Pte\.?|Co\.?|S\.?A\.?|B\.?V\.?|AG|AB|Oy|Sp\.?\s*z\s*o\.?o\.?)$",
    re.I)


def load_cache():
    if CACHE_FILE.exists():
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    return {"dns": {}, "geo": {}}


def save_cache(cache):
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=1),
                          encoding="utf-8")


def resolve_host(host, cache):
    if re.fullmatch(r"[\d.]+", host or ""):
        return host
    if host in cache["dns"]:
        return cache["dns"][host]
    try:
        ip = socket.gethostbyname(host)
    except Exception:  # noqa: BLE001
        ip = None
    cache["dns"][host] = ip
    return ip


def clean_org(org):
    org = (org or "").split(",")[0].strip()
    org = re.sub(r"\s*\(.*?\)\s*", " ", org).strip()  # 去掉 (eu-frankfurt-1) 这类机房备注
    org = CORP_SUFFIX.sub("", org).strip()
    return org


def is_routable_ip(ip):
    """公网可路由 IP 才可能是有效节点；保留地/私网/回环等必定连不上。"""
    try:
        a = ipaddress.ip_address(ip)
        return not (a.is_private or a.is_reserved or a.is_loopback
                    or a.is_multicast or a.is_link_local or a.is_unspecified)
    except ValueError:
        return False


def geo_lookup_batch(ips, cache):
    """批量查询，未命中缓存的走 ip-api.com batch（每次最多100个）。"""
    todo = [ip for ip in ips if ip and ip not in cache["geo"]]
    for i in range(0, len(todo), 100):
        chunk = todo[i:i + 100]
        ok = False
        for attempt in range(3):  # 失败重试
            try:
                r = requests.post(IPAPI_BATCH,
                                  json=[{"query": ip, "fields": FIELDS}
                                        for ip in chunk],
                                  timeout=30)
                r.raise_for_status()
                for item in r.json():
                    if item.get("status") == "success":
                        cache["geo"][item["query"]] = {
                            "country": item.get("country", ""),
                            "cc": item.get("countryCode", ""),
                            "org": clean_org(item.get("org") or item.get("isp")),
                        }
                    else:
                        cache["geo"][item.get("query", "")] = {
                            "country": "", "cc": "", "org": ""}
                ok = True
                break
            except Exception as e:  # noqa: BLE001
                print(f"  geo 批量查询失败(尝试{attempt + 1}/3): {e}")
                time.sleep(3)
        if ok:
            print(f"  geo 批量查询: {i + len(chunk)}/{len(todo)}")
        time.sleep(1.2)
    save_cache(cache)


def display_name(ip, cache):
    g = cache["geo"].get(ip) or {}
    cc = (g.get("cc") or "XX").upper()
    org = g.get("org") or "未知"
    return f"{cc}+{org}"


# 用正则从「CC+服务商」格式的节点名中匹配国家代码
COUNTRY_RE = re.compile(r"^([A-Z]{2})\+")

# 单独成组的国家/地区（代码 -> 中文展示名）
SPECIAL_REGIONS = {
    "HK": "香港", "TW": "台湾", "SG": "新加坡",
    "US": "美国", "KR": "韩国", "JP": "日本",
}

CONTINENT_CN = {"AS": "亚洲", "EU": "欧洲", "AF": "非洲",
                "AM": "美洲", "OC": "大洋洲"}

# 国家代码 -> 所属洲（SPECIAL_REGIONS 的六个不参与，仅列其余国家）
CC_CONTINENT = {
    # 亚洲
    **dict.fromkeys(["AF", "AM", "AZ", "BH", "BD", "BT", "BN", "KH", "CN",
                     "GE", "IN", "ID", "IR", "IQ", "IL", "JO", "KZ", "KW",
                     "KG", "LA", "LB", "MO", "MY", "MV", "MN", "MM", "NP",
                     "KP", "OM", "PK", "PS", "PH", "QA", "SA", "LK", "SY",
                     "TJ", "TH", "TR", "TM", "AE", "UZ", "VN", "YE"], "AS"),
    # 欧洲
    **dict.fromkeys(["AL", "AD", "AT", "BY", "BE", "BA", "BG", "HR", "CY",
                     "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IS",
                     "IE", "IT", "LV", "LI", "LT", "LU", "MT", "MD", "MC",
                     "ME", "NL", "MK", "NO", "PL", "PT", "RO", "RU", "SM",
                     "RS", "SK", "SI", "ES", "SE", "CH", "UA", "GB", "VA",
                     "GI", "IM", "AX"], "EU"),
    # 非洲
    **dict.fromkeys(["DZ", "AO", "BJ", "BW", "BF", "BI", "CM", "CV", "CF",
                     "TD", "KM", "CG", "CD", "CI", "DJ", "EG", "GQ", "ER",
                     "ET", "GA", "GM", "GH", "GN", "GW", "KE", "LS", "LR",
                     "LY", "MG", "MW", "ML", "MR", "MU", "MA", "MZ", "NA",
                     "NE", "NG", "RW", "ST", "SN", "SC", "SL", "SO", "ZA",
                     "SS", "SD", "SZ", "TZ", "TG", "TN", "UG", "ZM", "ZW",
                     "EH"], "AF"),
    # 美洲
    **dict.fromkeys(["AG", "BS", "BB", "BZ", "CA", "CR", "CU", "DM", "DO",
                     "SV", "GD", "GT", "HT", "HN", "JM", "MX", "NI", "PA",
                     "KN", "LC", "VC", "TT", "PR", "GL", "BM", "AW", "CW",
                     "BQ", "MQ", "GP", "AR", "BO", "BR", "CL", "CO", "EC",
                     "GY", "PY", "PE", "SR", "UY", "VE", "FK"], "AM"),
    # 大洋洲
    **dict.fromkeys(["AU", "FJ", "NZ", "PG", "WS", "VU", "SB", "NC", "PF",
                     "GU", "FM", "KI", "MH", "NR", "PW", "TV", "TO", "CK",
                     "NU", "TK", "WF"], "OC"),
}


def country_code_of(proxy_name):
    m = COUNTRY_RE.match(proxy_name or "")
    return m.group(1) if m else "XX"


# 服务策略组及其域名
SERVICE_DOMAINS = {
    "YouTube": ["youtube.com", "youtu.be", "googlevideo.com", "ytimg.com"],
    "Facebook": ["facebook.com", "fb.com", "fbcdn.net",
                 "whatsapp.com", "messenger.com", "fbcdn.com", "fbsbx.com"],
    "Instagram": ["instagram.com", "cdninstagram.com", "ig.me"],
    "X": ["x.com", "twitter.com", "twimg.com", "t.co"],
    "AI": ["openai.com", "chatgpt.com", "oaistatic.com", "oaiusercontent.com",
           "anthropic.com", "claude.ai", "perplexity.ai", "poe.com",
           "x.ai", "grok.com", "deepseek.com", "gemini.google.com",
           "bard.google.com", "aistudio.google.com", "copilot.microsoft.com",
           "midjourney.com", "huggingface.co", "character.ai"],
    "Cloudflare": ["cloudflare.com", "cloudflareinsights.com",
                   "cloudflarestream.com"],
    "GitHub": ["github.com", "githubusercontent.com", "githubassets.com",
               "ghcr.io"],
    "Telegram": ["telegram.org", "t.me", "telegram.me"],
    "Spotify": ["spotify.com", "scdn.co", "spotifycdn.com", "spoti.fi"],
    "Disney+": ["disneyplus.com", "disney-plus.net", "dssott.com"],
    "Netflix": ["netflix.com", "netflix.net", "nflximg.net", "nflxext.com",
                "nflxvideo.net"],
    "Apple": ["apple.com", "icloud.com", "mzstatic.com", "itunes.com",
              "apple-cloudkit.com", "cdn-apple.com"],
}

# 服务策略组（与 SERVICE_DOMAINS 的 key 对应，Google 走 RULE-SET）
SERVICE_GROUPS = ["YouTube", "Facebook", "AI", "Google", "X", "Instagram",
                  "Cloudflare", "GitHub", "Telegram", "Spotify",
                  "Disney+", "Netflix", "Apple"]


def extract_host(uri):
    """从节点 URI 里提取服务器 host。"""
    try:
        scheme, rest = uri.split("://", 1)
        body = rest.split("#", 1)[0].split("?", 1)[0]
        if scheme == "ss" and "@" not in body:
            b = body + "=" * (-len(body) % 4)
            dec = base64.b64decode(b).decode("utf-8", "ignore")
            m = re.search(r"@([^:]+)", dec)
            return m.group(1) if m else None
        if scheme == "vmess":
            b = body + "=" * (-len(body) % 4)
            return json.loads(base64.b64decode(b).decode("utf-8", "ignore")).get("add")
        parts = urllib.parse.urlsplit(uri)
        return parts.hostname
    except Exception:  # noqa: BLE001
        return None


def set_fragment(uri, name):
    enc = urllib.parse.quote(name, safe="")
    if "#" in uri:
        return uri.rsplit("#", 1)[0] + "#" + enc
    return uri + "#" + enc


def rename_vmess_ps(uri, name):
    """vmess 的备注同时写在 JSON 的 ps 字段里。"""
    try:
        head, frag = (uri.split("#", 1) + [""])[:2]
        b = head[8:] + "=" * (-len(head[8:]) % 4)
        data = json.loads(base64.b64decode(b).decode("utf-8"))
        data["ps"] = name
        new_b = base64.b64encode(
            json.dumps(data, ensure_ascii=False).encode()).decode()
        return "vmess://" + new_b + ("#" + frag if frag else "")
    except Exception:  # noqa: BLE001
        return uri


def build_proxy_groups(proxies):
    """国家/洲分组 + YouTube/Facebook/AI/Google 服务组 + 规则集。

    地区分组直接在配置里用正则写（Mihomo 原生支持 filter 正则 +
    include-all-proxies），以后新增节点会自动归组，无需重新生成名单。
    """
    # 各洲的国家代码（去掉单独成组的 6 个）
    continent_codes = {}
    for cont in ["AS", "EU", "AF", "AM", "OC"]:
        codes = sorted(cc for cc, c in CC_CONTINENT.items()
                       if c == cont and cc not in SPECIAL_REGIONS)
        continent_codes[CONTINENT_CN[cont]] = codes

    region_filters = {
        "香港": "^HK", "台湾": "^TW", "新加坡": "^SG",
        "美国": "^US", "韩国": "^KR", "日本": "^JP",
    }
    for region, codes in continent_codes.items():
        region_filters[region] = "^(" + "|".join(codes) + ")"
    ordered_regions = (["香港", "台湾", "新加坡", "美国", "韩国", "日本"]
                       + [CONTINENT_CN[c] for c in ["AS", "EU", "AF", "AM", "OC"]])

    groups = [
        {"name": "自动选择", "type": "url-test",
         "proxies": ordered_regions,
         "url": "http://www.gstatic.com/generate_204",
         "interval": 300, "tolerance": 50},
        {"name": "手动选择", "type": "select",
         "proxies": ordered_regions},
    ]
    for r in ordered_regions:
        groups.append({"name": r, "type": "url-test",
                       "include-all-proxies": True,
                       "filter": region_filters[r],
                       "url": "http://www.gstatic.com/generate_204",
                       "interval": 300, "tolerance": 50})
    # 服务策略组：成员直接为各国家/洲分组
    for svc in SERVICE_GROUPS:
        groups.append({"name": svc, "type": "select",
                       "proxies": ordered_regions + ["DIRECT"]})

    # 规则集：MetaCubeX meta-rules-dat geosite MRS（behavior+format 双字段）
    geosite_base = "https://cdn.jsdelivr.net/gh/MetaCubeX/meta-rules-dat@meta/geo/geosite"
    providers = {}
    for name in ["x", "youtube", "openai", "netflix", "disney", "spotify",
                 "github", "instagram", "apple", "google", "microsoft",
                 "facebook", "telegram", "cloudflare"]:
        providers[name] = {
            "type": "http", "behavior": "domain", "format": "mrs",
            "url": f"{geosite_base}/{name}.mrs",
            "path": f"./ruleset/{name}.mrs",
            "interval": 120,
        }

    # provider -> 策略组的分流
    rules = [
        "RULE-SET,openai,AI",
        "RULE-SET,netflix,Netflix",
        "RULE-SET,disney,Disney+",
        "RULE-SET,spotify,Spotify",
        "RULE-SET,cloudflare,Cloudflare",
        "RULE-SET,github,GitHub",
        "RULE-SET,telegram,Telegram",
        "RULE-SET,youtube,YouTube",
        "RULE-SET,google,Google",
        "RULE-SET,x,X",
        "RULE-SET,instagram,Instagram",
        "RULE-SET,facebook,Facebook",
        "RULE-SET,apple,Apple",
        "RULE-SET,microsoft,DIRECT",
    ]
    # AI / Cloudflare 保留手写域名补充（geosite 覆盖不全）
    for svc in ("AI", "Cloudflare"):
        rules.extend(f"DOMAIN-SUFFIX,{d},{svc}" for d in SERVICE_DOMAINS[svc])
    rules += [
        "GEOSITE,steam@cn,DIRECT",
        "GEOSITE,steam,手动选择",
        # 常见代理/下载软件进程直连
        "PROCESS-NAME,v2ray,DIRECT",
        "PROCESS-NAME,Surge,DIRECT",
        "PROCESS-NAME,ss-local,DIRECT",
        "PROCESS-NAME,privoxy,DIRECT",
        "PROCESS-NAME,trojan,DIRECT",
        "PROCESS-NAME,trojan-go,DIRECT",
        "PROCESS-NAME,naive,DIRECT",
        "PROCESS-NAME,CloudflareWARP,DIRECT",
        "PROCESS-NAME,Cloudflare WARP,DIRECT",
        "IP-CIDR,162.159.193.0/24,DIRECT,no-resolve",
        "PROCESS-NAME,p4pclient,DIRECT",
        "PROCESS-NAME,Thunder,DIRECT",
        "PROCESS-NAME,DownloadService,DIRECT",
        "PROCESS-NAME,qbittorrent,DIRECT",
        "PROCESS-NAME,fdm,DIRECT",
        "PROCESS-NAME,aria2c,DIRECT",
        "PROCESS-NAME,Folx,DIRECT",
        "PROCESS-NAME,NetTransport,DIRECT",
        "PROCESS-NAME,uTorrent,DIRECT",
        "PROCESS-NAME,WebTorrent,DIRECT",
        "GEOIP,LAN,DIRECT",
        "GEOIP,CN,DIRECT",
        "MATCH,手动选择",
    ]
    return groups, providers, rules, ordered_regions, region_filters


def region_of(proxy_name):
    """节点名 -> 地区分组展示名（与配置里的 filter 正则一致）。"""
    cc = country_code_of(proxy_name)
    if cc in SPECIAL_REGIONS:
        return SPECIAL_REGIONS[cc]
    return CONTINENT_CN.get(CC_CONTINENT.get(cc), "其他")


# 常见国家/地区的保底配额（按优先级，有多少取多少）
SPECIAL_QUOTAS = {
    "香港": 30, "台湾": 15, "新加坡": 25,
    "美国": 70, "韩国": 12, "日本": 25,
}


def stratified_sample(pairs, target, min_n=5, seed=42):
    """按地区分层抽样到 target 个：常见国家按配额优先保证，其余按比例。"""
    import random
    from collections import defaultdict
    groups = defaultdict(list)
    for pr in pairs:
        groups[region_of(pr[1]["name"])].append(pr)
    rnd = random.Random(seed)
    for g in groups.values():
        rnd.shuffle(g)

    quotas = {}
    for r, q in SPECIAL_QUOTAS.items():  # 常见国家优先
        if r in groups:
            quotas[r] = min(len(groups[r]), q)
    rest_target = target - sum(quotas.values())
    rest_groups = {r: g for r, g in groups.items() if r not in quotas}
    total_rest = sum(len(g) for g in rest_groups.values())
    for r, g in rest_groups.items():  # 其余按比例
        q = max(min_n, round(rest_target * len(g) / total_rest)) \
            if total_rest > 0 else 0
        quotas[r] = min(q, len(g))

    # 微调使总和恰好为 target（优先从配额最大的组增减）
    while sum(quotas.values()) > target:
        r = max(quotas, key=lambda r: quotas[r])
        if quotas[r] <= 1:
            break
        quotas[r] -= 1
    while sum(quotas.values()) < target:
        cand = [r for r in groups if quotas[r] < len(groups[r])]
        if not cand:
            break
        r = max(cand, key=lambda r: len(groups[r]) - quotas[r])
        quotas[r] += 1
    out = []
    for r, g in groups.items():
        out.extend(g[:quotas[r]])
    rnd.shuffle(out)
    return out


def main():
    ap = argparse.ArgumentParser(description="节点重命名为 CC+服务商，并按地区抽样限制数量")
    ap.add_argument("-o", "--output", default=str(HERE / "output"),
                    help="最终成品目录")
    ap.add_argument("--raw", default=str(HERE / "output" / "raw"),
                    help="scraper.py 的原始抓取结果目录（每次都从这里读全量数据）")
    ap.add_argument("--limit", type=int, default=400,
                    help="最终代理数量上限（默认 400）")
    args = ap.parse_args()
    outdir = Path(args.output)
    outdir.mkdir(parents=True, exist_ok=True)
    rawdir = Path(args.raw)
    cache = load_cache()
    socket.setdefaulttimeout(8)
    # 用新版 clean_org 统一清洗历史缓存里的服务商名
    for v in cache.get("geo", {}).values():
        if v.get("org"):
            v["org"] = clean_org(v["org"])

    uris = [l.strip() for l in
            (rawdir / "nodes.txt").read_text(encoding="utf-8").splitlines()
            if l.strip()]
    # 订阅自带的 clash 代理（无 URI 形式），由 scraper.py 另存
    yp_path = rawdir / "_yaml_proxies.json"
    yaml_proxies = (json.loads(yp_path.read_text(encoding="utf-8"))
                    if yp_path.exists() else [])
    # 统一成 (uri|None, proxy) 配对，后续重命名/抽样/写回都基于它
    from scraper import uri_to_clash
    pairs = []
    for u in uris:
        p = uri_to_clash(u)
        if p:
            pairs.append([u, p])
    for p in yaml_proxies:
        if isinstance(p, dict) and p.get("server"):
            p.pop("_source", None)  # 兼容旧抓取数据里的来源标记
            pairs.append([None, dict(p)])
    print(f"配对: {len(pairs)}（URI来源 {sum(1 for u, _ in pairs if u)}，"
          f"订阅来源 {sum(1 for u, _ in pairs if not u)}）")

    # 先按 服务器:端口:协议 去重（同一服务器的重复节点只保留一个），
    # 避免抽样配额被重复节点挤占
    seen_sp, deduped = set(), []
    for u, p in pairs:
        key = (p.get("server"), p.get("port"), p.get("type"))
        if key in seen_sp:
            continue
        seen_sp.add(key)
        deduped.append([u, p])
    pairs = deduped
    print(f"去重后: {len(pairs)} 个代理")

    # 收集 host -> 解析 IP
    hosts = {p.get("server") for _, p in pairs if p.get("server")}
    hosts.discard(None)
    host_ip = {}
    for h in hosts:
        ip = resolve_host(h, cache)
        if ip:
            host_ip[h] = ip
    print(f"解析成功 {len(host_ip)}/{len(hosts)} 个主机")
    save_cache(cache)

    # 过滤：服务器 IP 非公网可路由或无法解析的节点必定连不上，直接剔除
    def host_ok(h):
        ip = host_ip.get(h)
        return bool(ip) and is_routable_ip(ip)

    before = len(pairs)
    pairs = [[u, p] for u, p in pairs if host_ok(p.get("server"))]
    print(f"剔除无效节点: {before}->{len(pairs)}")

    # 批量 geo 查询
    geo_lookup_batch(sorted(set(host_ip.values())), cache)

    # 重命名为 CC+服务商（同名加序号）
    def new_name_for(host):
        ip = host_ip.get(host)
        return display_name(ip, cache) if ip else "XX+未知"

    used = set()

    def unique(base):
        n, i = base, 2
        while n in used:
            n = f"{base}-{i}"
            i += 1
        used.add(n)
        return n

    for _, p in pairs:
        p["name"] = unique(new_name_for(p.get("server")))

    # 按地区分层抽样，限制总数（常见国家按配额优先保证）
    pairs = stratified_sample(pairs, args.limit)
    print(f"分层抽样后: {len(pairs)} 个代理")

    # 去重后重新编号命名（去掉抽样前的大序号，如 -535）
    used.clear()
    for _, p in pairs:
        p["name"] = unique(new_name_for(p.get("server")))

    # 1) 写 nodes.txt / subscription.txt（仅抽样命中的 URI）
    new_uris = []
    for u, p in pairs:
        if not u:
            continue
        name = p["name"]
        if u.startswith("vmess://"):
            u = rename_vmess_ps(u, name)
        new_uris.append(set_fragment(u, name))
    (outdir / "nodes.txt").write_text("\n".join(new_uris) + "\n",
                                      encoding="utf-8")
    b64 = base64.b64encode("\n".join(new_uris).encode()).decode()
    (outdir / "subscription.txt").write_text(b64, encoding="utf-8")

    # 2) 写 clash.yaml：地区分组（配置内正则）+ 服务组 + 规则集
    proxies = [p for _, p in pairs]
    # 证书验证：reality 自带公钥校验、443 端口证书通常正常，都不跳过；
    # 其余全部跳过，避免自签/过期证书连不上
    for p in proxies:
        if "reality-opts" in p or str(p.get("port")) == "443":
            p.pop("skip-cert-verify", None)
        else:
            p["skip-cert-verify"] = True
    groups, providers, rules, ordered_regions, region_filters = \
        build_proxy_groups(proxies)
    cfg = {
        "mixed-port": 7890,
        "allow-lan": True,
        "mode": "rule",
        "log-level": "info",
        "proxies": proxies,
        "proxy-groups": groups,
        "rule-providers": providers,
        "rules": rules,
    }
    with open(outdir / "clash.yaml", "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)

    names = [p["name"] for p in proxies]
    print("命名示例:", names[:5])
    print("地区分组(正则匹配统计):",
          [(r, sum(1 for n in names if re.match(region_filters[r], n)))
           for r in ordered_regions])
    print(f"\n完成：{len(proxies)} 个代理（URI {len(new_uris)}），缓存见 {CACHE_FILE.name}")


if __name__ == "__main__":
    main()
