#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
财报舆情雷达 v2 —— 服务端抓取（仅标准库，无第三方依赖）
合并双引擎，输出同一个 latest.json：
  A. 板块资金流（东方财富 push2，复用 market-radar 验证代码）
       -> 指数 / 行业板块 / 概念板块 / 个股主力净流入 / 板块历史资金流
  B. 财报舆情（东方财富公告 np-anotice）
       -> 财报公告 -> 利好/利空/中性 -> 聚合「个股参考权重」
归档 snapshots/ + snapshots/index.json（含板块 stats 与财报 overview，供历史回看）。

设计原则（沿用 wolf-screener / market-radar 工程偏好）：
  - 配置化、关键步骤统计日志、多域名兜底 + 重试、单源失败不影响整体、抓不到记失败不伪造。
  - 诚实边界：标题关键词判定是启发式，非预测；仅作「财报舆情参考权重」。
"""
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone, timedelta

# ============================== 配置 ==============================
UT = "b2884a393a59ad64002292a3e90d46a5"
EM_HOSTS = ["push2.eastmoney.com", "push2delay.eastmoney.com"]          # 指数/板块/个股
EM_HIS_HOSTS = ["push2his.eastmoney.com", "push2delay.eastmoney.com"]   # 历史资金流
ANN_HOSTS = ["np-anotice-stock.eastmoney.com"]                          # 财报公告（带股票代码）
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
EM_REFERER = "https://quote.eastmoney.com/"
ANN_REFERER = "https://data.eastmoney.com/notices/"
TIMEOUT = 15
RETRY = 3
PAGE_SIZE = 200               # 抓取最新 N 条公告（从中筛财报）
HIST_TOP = 10                 # 行业/概念各取前 N 个拉历史
ROOT = os.path.dirname(os.path.abspath(__file__))
CST = timezone(timedelta(hours=8))   # 北京时间

# 东方财富概念板块里混入的"选股/统计"型板块（非真实行业/概念），应从板块净流出等核心视图剔除
JUNK_BOARD_RE = re.compile(r'^(昨日|最近)|首板$|连板$|多板$|高换手$|触板$|涨跌停$|破净股$|次新股$')

def filter_junk_boards(boards):
    return [b for b in boards if not JUNK_BOARD_RE.search(b.get("f14", ""))]


def log(msg):
    ts = datetime.now(CST).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# ============================== 请求层 ==============================
def _strip_jsonp(text, cb):
    s = text.strip()
    if s.startswith(cb + "(") or s.startswith(cb + " ("):
        s = s[s.index("(") + 1:]
        if s.endswith(");"):
            s = s[:-2]
        elif s.endswith(")"):
            s = s[:-1]
    return s


def http_get_json(url, referer=EM_REFERER, cb_name=None, timeout=TIMEOUT, retry=RETRY):
    last_err = None
    for attempt in range(1, retry + 1):
        try:
            req = urllib.request.Request(url)
            req.add_header("User-Agent", UA)
            req.add_header("Referer", referer)
            req.add_header("Accept", "*/*")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "ignore")
            if cb_name:
                raw = _strip_jsonp(raw, cb_name)
            return json.loads(raw)
        except Exception as e:  # noqa: BLE001
            last_err = e
            log(f"    · 尝试{attempt}失败: {e}")
            if attempt < retry:
                time.sleep(1.5 * attempt)
    raise last_err or RuntimeError("未知错误")


def clist_url(host, fs, pz, fields, po=1, pn=1):
    return ("https://" + host + "/api/qt/clist/get?pn=" + str(pn) + "&pz=" + str(pz) +
            "&po=" + str(po) + "&np=1&ut=" + UT + "&fltt=2&invt=2&fid=f62&fs=" +
            urllib.parse.quote(fs, safe="") + "&fields=" + fields)


def ulist_url(host, secids, fields):
    return ("https://" + host + "/api/qt/ulist.np/get?fltt=2&invt=2&fields=" +
            fields + "&secids=" + secids + "&ut=" + UT)


def his_url(host, secid):
    return ("https://" + host + "/api/qt/stock/fflow/daykline/get?lmt=30&klt=101&secid=" +
            secid + "&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63&ut=" + UT)


def norm_diff(d):
    if not d:
        return []
    data = d.get("data")
    if not data:
        return []
    diff = data.get("diff")
    if diff is None:
        return []
    return list(diff.values()) if isinstance(diff, dict) else (diff if isinstance(diff, list) else [])


def norm_klines(d):
    if not d:
        return None
    data = d.get("data")
    if not data:
        return None
    return data.get("klines")


# ============================== 板块资金流引擎 (A) ==============================
def fetch_boards(fs, pz, po=1):
    cb = "emcb_" + str(int(time.time() * 1000))
    for host in EM_HOSTS:
        try:
            url = clist_url(host, fs, pz, "f2,f3,f8,f10,f12,f14,f62,f66,f72,f78,f84,f104,f105,f184", po) + "&cb=" + cb
            return norm_diff(http_get_json(url, EM_REFERER, cb))
        except Exception as e:  # noqa: BLE001
            log(f"    · 域名 {host} 板块列表失败: {e}")
    return []


def fetch_indices():
    secids = "1.000001,0.399001,0.399006,1.000300,1.000905,1.000688"
    cb = "emcb_" + str(int(time.time() * 1000))
    for host in EM_HOSTS:
        try:
            url = ulist_url(host, secids, "f2,f3,f6,f12,f14") + "&cb=" + cb
            return norm_diff(http_get_json(url, EM_REFERER, cb))
        except Exception as e:  # noqa: BLE001
            log(f"    · 域名 {host} 指数失败: {e}")
    return []


def fetch_stocks(po=1):
    fs = ("m:0+t:6+f:!2,m:0+t:13+f:!2,m:0+t:80+f:!2,m:1+t:2+f:!2,"
          "m:1+t:23+f:!2,m:0+t:7+f:!2,m:1+t:3+f:!2")
    cb = "emcb_" + str(int(time.time() * 1000))
    for host in EM_HOSTS:
        try:
            url = clist_url(host, fs, 12, "f2,f3,f10,f12,f14,f62,f184", po) + "&cb=" + cb
            return norm_diff(http_get_json(url, EM_REFERER, cb))
        except Exception as e:  # noqa: BLE001
            log(f"    · 域名 {host} 个股失败: {e}")
    return []


# 净流出专用 host：优先 push2delay（沙箱/海外 python 实测 po=0 升序生效），
# 规避部分 host（push2）忽略 po 导致净流出抓取为空的问题。
OUT_HOSTS = ["push2delay.eastmoney.com", "push2.eastmoney.com"]
OUT_FIELDS = "f2,f3,f10,f12,f14,f62,f184"


def fetch_on(hosts, fs, pz, po=1, fields=OUT_FIELDS, pn=1):
    cb = "emcb_" + str(int(time.time() * 1000))
    for host in hosts:
        try:
            url = clist_url(host, fs, pz, fields, po, pn) + "&cb=" + cb
            return norm_diff(http_get_json(url, EM_REFERER, cb))
        except Exception as e:  # noqa: BLE001
            log(f"    · 域名 {host} 失败: {e}")
    return []


def fetch_stocks_on(hosts, po=1, pz=12):
    fs = ("m:0+t:6+f:!2,m:0+t:13+f:!2,m:0+t:80+f:!2,m:1+t:2+f:!2,"
          "m:1+t:23+f:!2,m:0+t:7+f:!2,m:1+t:3+f:!2")
    cb = "emcb_" + str(int(time.time() * 1000))
    for host in hosts:
        try:
            url = clist_url(host, fs, pz, OUT_FIELDS, po) + "&cb=" + cb
            return norm_diff(http_get_json(url, EM_REFERER, cb))
        except Exception as e:  # noqa: BLE001
            log(f"    · 域名 {host} 个股失败: {e}")
    return []


# ============================== ETF 主力资金流（A 引擎扩展） ==============================
# 东财 b:MK 系列板块分组：MK0021 沪深ETF / MK0022 跨境ETF / MK0023 商品ETF / MK0024 货币ETF。
# 注意：债券型 ETF 归属 MK0021（沪深ETF）组内；f62 为主力净流入（二级市场成交口径，单位元），
#       与同花顺 iFinD 的"申赎口径（份额×净值）"不同 —— 页面与 AI 解读均须标注口径。
ETF_GROUPS = [
    ("b:MK0021", "沪深ETF"),
    ("b:MK0022", "跨境ETF"),
    ("b:MK0023", "商品ETF"),
    ("b:MK0024", "货币ETF"),
]
ETF_FIELDS = "f2,f3,f12,f14,f62,f184,f66,f72"


def fetch_etf_flow(max_per_group=1500, page=100):
    """全量 ETF（沪深/跨境/商品/货币）主力净流入（f62, 单位元）。

    分页拉全量（push2delay 单页上限 100），避免只拿到 f62 降序前 100 而漏掉净流出。
    返回：
      available: 是否可用（盘前/盘中未回填时 f62 全 0 → False）
      total/groups: 抓到条数
      topIn/topOut: 净流入/净流出 TOP10（按 f62）
      byGroup: 各板块组 f62 合计（元）
      caveat: 口径说明
    """
    groups, all_rows = {}, []
    for fs, gname in ETF_GROUPS:
        rows, pn = [], 1
        while pn <= 20 and len(rows) < max_per_group:
            got = fetch_on(OUT_HOSTS, fs, page, 1, ETF_FIELDS, pn=pn)
            if not got:
                break
            rows += got
            if len(got) < page:
                break
            pn += 1
        for r in rows:
            if not r.get("f12"):
                continue
            all_rows.append({
                "code": r.get("f12"), "name": r.get("f14"),
                "price": r.get("f2"), "pct": r.get("f3"),
                "f62": r.get("f62"), "f184": r.get("f184"), "group": gname,
            })
        groups[gname] = len(rows)
    valid = [r for r in all_rows if isinstance(r.get("f62"), (int, float))]
    nonzero = [r for r in valid if r["f62"]]
    available = len(nonzero) >= 5          # 盘前 f62 未回填 → 全 0 → 标记不可用
    top_in, top_out, by_group = [], [], {}
    if available:
        valid.sort(key=lambda r: r["f62"], reverse=True)
        top_in = valid[:10]
        top_out = valid[-10:][::-1]
        for r in valid:
            g = r["group"]
            by_group[g] = by_group.get(g, 0) + (r["f62"] or 0)
        by_group = {g: round(v, 2) for g, v in by_group.items()}
    return {
        "available": available,
        "total": len(valid),
        "groups": groups,
        "topIn": top_in,
        "topOut": top_out,
        "byGroup": by_group,
        "caveat": "主力资金口径（二级市场成交），与 iFinD 申赎口径（份额×净值）不同",
    }


# ============================== 外盘指数（美股/港股/亚太/欧股） ==============================
# 东财 secid：100.=美股指数，100.HSI=恒生，100.KS11=韩国KOSPI，100.N225=日经，100.SX5E=欧洲斯托克50，100.GDAXI=德国DAX
# f3 = 涨跌幅（% 原值，如 -5.27 表示 -5.27%）；美股为前一日收盘，港股/亚太/欧股为最新交易日（亚盘实时）
OVERSEAS_INDICES = [
    ("100.DJIA",  "道琼斯"),
    ("100.SPX",   "标普500"),
    ("100.IXIC",  "纳斯达克"),
    ("100.HSI",   "恒生指数"),
    ("100.KS11",  "韩国KOSPI"),
    ("100.N225",  "日经225"),
    ("100.SX5E",  "欧洲斯托克50"),
    ("100.GDAXI", "德国DAX30"),
]


def fetch_overseas():
    """外盘 8 大指数涨跌幅（%）。供 brief.py 做外盘风险分级 + AI 解读引用。"""
    secids = ",".join(s for s, _ in OVERSEAS_INDICES)
    cb = "emcb_" + str(int(time.time() * 1000))
    for host in EM_HOSTS:
        try:
            url = ulist_url(host, secids, "f2,f3,f4,f12,f14") + "&cb=" + cb
            rows = norm_diff(http_get_json(url, EM_REFERER, cb))
            out = []
            for r in rows:
                code = r.get("f12")
                name = next((n for s, n in OVERSEAS_INDICES if s.split(".")[1] == code), None)
                out.append({
                    "code": code,
                    "name": name or r.get("f14"),
                    "price": r.get("f2"),
                    "pct": r.get("f3"),
                })
            return out
        except Exception as e:  # noqa: BLE001
            log(f"    · 域名 {host} 外盘失败: {e}")
    return []


# ============================== 海外宏观（美债收益率 / 美联储利率 · FRED） ==============================
# FRED 公开 CSV（无需 key）：DGS10=美债10年、DGS2=美债2年、DFF=有效联邦基金利率(日频)
# 弥补"外盘只抓指数涨跌"的盲区：美债收益率/利差/加息预期是 A股 风险偏好的先行指标
FRED_BASE = "https://fred.stlouisfed.org/graph/fredgraph.csv?id="
FRED_SERIES = {
    "us10y": "DGS10",   # 美国10年期国债收益率
    "us2y":  "DGS2",    # 美国2年期国债收益率
    "fed":   "DFF",     # 有效联邦基金利率（日频）
}


FRED_UA = "curl/8.0"   # 中性 UA：实测 FRED 对浏览器 UA(Chrome 系) 会限流/超时，curl UA 稳定（沙箱与 Actions 均验证）


def fetch_fred_csv(series):
    """取 FRED 单序列 CSV，返回 [(date, value_or_None), ...]（按日期升序）。带重试抗瞬时抖动。"""
    url = FRED_BASE + series
    raw = None
    last_err = None
    for attempt in range(1, 4):
        try:
            req = urllib.request.Request(url)
            req.add_header("User-Agent", FRED_UA)
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read().decode("utf-8", "ignore")
            break
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < 3:
                time.sleep(1.5 * attempt)
    if raw is None:
        log(f"    · FRED {series} 失败: {last_err}")
        return []
    rows = []
    for line in raw.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split(",")
        if len(parts) < 2:
            continue
        d, v = parts[0], parts[1].strip()
        if v in (".", "", "NaN"):
            rows.append((d, None))
        else:
            try:
                rows.append((d, float(v)))
            except ValueError:
                rows.append((d, None))
    return rows


def _latest_two(rows):
    """返回 (latest_val, prev_val, latest_date)，取最后两个非空值。"""
    nonnull = [(d, v) for d, v in rows if v is not None]
    if not nonnull:
        return None, None, None
    if len(nonnull) == 1:
        return nonnull[0][1], None, nonnull[0][0]
    return nonnull[-1][1], nonnull[-2][1], nonnull[-1][0]


def fetch_macro():
    """海外宏观：美债10Y/2Y收益率、10Y-2Y利差、联邦基金利率（FRED）。供 brief 宏观信号条 + AI 解读。"""
    out = {"available": False, "source": "FRED",
           "updated": datetime.now(CST).strftime("%Y-%m-%dT%H:%M:%S+08:00")}
    series_data = {}
    for key, sid in FRED_SERIES.items():
        rows = fetch_fred_csv(sid)
        v, prev, d = _latest_two(rows)
        if v is None:
            continue
        chg = round(v - prev, 4) if prev is not None else None
        series_data[key] = {"v": v, "chg": chg, "date": d}
    if not series_data:
        return out
    out["available"] = True
    out.update(series_data)
    if "us10y" in series_data and "us2y" in series_data:
        out["spread"] = round(series_data["us10y"]["v"] - series_data["us2y"]["v"], 4)
    return out


def fetch_history(secid):
    cb = "emcb_" + str(int(time.time() * 1000))
    for host in EM_HIS_HOSTS:
        try:
            url = his_url(host, secid) + "&cb=" + cb
            kl = norm_klines(http_get_json(url, EM_REFERER, cb, timeout=8, retry=1))
            if kl and len(kl) >= 2:
                return kl
            log(f"    · 域名 {host} 历史仅 {len(kl) if kl else 0} 天")
        except Exception as e:  # noqa: BLE001
            log(f"    · 域名 {host} 历史失败: {e}")
    return None


# ============================== 财报舆情引擎 (B) ==============================
FIN_KW = ["业绩", "净利", "营收", "中报", "年报", "季报", "半年报", "一季报", "三季报", "快报",
          "预增", "预减", "预盈", "首亏", "扭亏", "分红", "送转", "高送转", "利润分配", "财报",
          "盈利", "亏损", "财务数据", "经营数据", "业绩预告", "业绩快报", "业绩说明会"]

BULL_KW = ["预增", "扭亏", "扭亏为盈", "大增", "高增", "超预期", "增长", "创新高", "创纪录", "高送转",
           "送转", "分红", "利润分配", "派发", "现金分红", "高分红", "股息", "回购", "中标", "签约",
           "大单", "订单", "提价", "业绩亮眼", "向好", "改善", "盈利", "提升", "上修", "预盈", "翻倍",
           "亮眼", "提振", "利好", "扩产", "新签", "份额提升", "有望"]

BEAR_KW = ["预减", "首亏", "续亏", "下滑", "下降", "减少", "减值", "暴雷", "退市", "立案", "罚款",
           "商誉减值", "不及预期", "下调", "亏损扩大", "变脸", "下修", "警示", "停产", "诉讼", "暴跌",
           "腰斩", "业绩变脸", "利空", "风险警示", "终止", "违约", "查封", "承压", "弱化", "ST", "*ST",
           "亏损", "降幅", "减亏", "下挫", "处罚", "降级"]

CODE_RE = re.compile(r"^\d{6}$")


def is_financial(title):
    return any(k in title for k in FIN_KW)


def classify(title):
    bull = sum(1 for k in BULL_KW if k in title)
    bear = sum(1 for k in BEAR_KW if k in title)
    if bull > bear:
        return "bull"
    if bear > bull:
        return "bear"
    return "neutral"


def extract_stocks(item):
    out = []
    for c in item.get("codes") or []:
        code = c.get("stock_code") or ""
        if not CODE_RE.match(code):
            continue
        atype = c.get("ann_type") or ""
        if "A" not in atype and "SHA" not in atype and "SZA" not in atype:
            continue
        name = c.get("short_name") or ""
        out.append({"code": code, "name": name})
    seen = set()
    res = []
    for s in out:
        if s["code"] in seen:
            continue
        seen.add(s["code"])
        res.append(s)
    return res


def item_date(item):
    for key in ("display_time", "eiTime", "notice_date", "sort_date"):
        v = item.get(key)
        if v:
            return str(v)[:10]
    return ""


def fetch_announcements(page_size=PAGE_SIZE):
    last = None
    for host in ANN_HOSTS:
        try:
            url = ("https://" + host + "/api/security/ann?sr=-1&page_size=" + str(page_size) +
                   "&page_index=1&client_source=web")
            d = http_get_json(url, ANN_REFERER)
            data = d.get("data") or {}
            return data.get("list") or []
        except Exception as e:  # noqa: BLE001
            last = e
            log(f"    · 域名 {host} 公告失败: {e}")
    raise last or RuntimeError("全部公告域名失败")


def call_llm(system, user, api_key, base_url, model, timeout=50, max_tokens=1200):
    """OpenAI 兼容 chat/completions（仅标准库）。返回 (content, err)。"""
    if not api_key:
        return None, 'no api key'
    url = base_url.rstrip('/') + '/chat/completions'
    body = json.dumps({
        'model': model,
        'messages': [{'role': 'system', 'content': system},
                     {'role': 'user', 'content': user}],
        'temperature': 0.2,
        'max_tokens': max_tokens,
        'response_format': {'type': 'json_object'},
    }).encode('utf-8')
    req = urllib.request.Request(url, data=body, headers={
        'Content-Type': 'application/json',
        'Authorization': 'Bearer ' + api_key,
        'User-Agent': UA,
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode('utf-8', 'ignore'))
        return data['choices'][0]['message']['content'], None
    except Exception as e:  # noqa: BLE001
        return None, str(e)


def llm_classify_earnings_titles(items, api_key, base_url, model, batch=25):
    """智谱按公告标题语义批量判定利好/利空/中性。返回与 items 等长的 list[dict]；失败返回 None。"""
    if not items:
        return []
    out = [None] * len(items)
    for start in range(0, len(items), batch):
        chunk = items[start:start + batch]
        lines = ['%d. %s' % (start + k, (it.get('title') or '').strip()[:100]) for k, it in enumerate(chunk)]
        user = (
            '以下是 A股 上市公司公告标题，请按"实际含义"判断每条对业绩/经营的方向：\n%s\n\n'
            '要求：仅依据标题，不编造数字。输出严格 JSON（不要任何解释文字）：\n'
            '{"items":[{"i":0,"sentiment":"bull","reason":"一句话理由"},...]}\n'
            'sentiment=bull(利好)/bear(利空)/neutral(中性)。注意语义而非机械关键词：'
            '"扭亏为盈/拟回购/高分红/中标/大额订单/预增且幅度大"判利好；"预减/下修/亏损扩大/暴雷/风险警示/立案"判利空；'
            '"预增但幅度收窄/例行披露/股东大会通知"判中性。items 数组必须与输入条数相同、顺序一致。'
        ) % '\n'.join(lines)
        system = ('你是 A股 财报公告研判助手。只依据公告标题客观判断利好/利空/中性，不预测、不荐股。'
                  '输出必须为合法 JSON。')
        content, err = None, None
        for _t in range(2):   # 抗瞬时超时/截断：重试一次
            content, err = call_llm(system, user, api_key, base_url, model, timeout=90, max_tokens=2500)
            if content:
                break
        if not content:
            raise RuntimeError('智谱财报判定调用失败: %s' % (err or 'empty'))
        txt = content.strip()
        if txt.startswith('```'):
            txt = re.sub(r'^```[a-zA-Z]*\n?', '', txt)
            txt = txt.rstrip('`').strip()
        try:
            obj = json.loads(txt)
        except Exception:
            m = re.search(r'\{.*\}', content, re.S)
            if not m:
                raise RuntimeError('智谱财报判定输出非法 JSON: %s' % content[:200])
            try:
                obj = json.loads(m.group(0))
            except Exception:
                raise RuntimeError('智谱财报判定输出非法 JSON: %s' % content[:200])
        arr = obj.get('items') or []
        if not isinstance(arr, list) or len(arr) < len(chunk):
            raise RuntimeError('智谱财报判定条数不足: 期望≥%d 实得%d' % (len(chunk), len(arr)))
        for r in arr:
            i = r.get('i')
            if not isinstance(i, int) or not (0 <= i < len(items)):
                continue
            s = str(r.get('sentiment') or 'neutral').strip().lower()
            if s not in ('bull', 'bear', 'neutral'):
                continue
            out[i] = {'sentiment': s, 'reason': str(r.get('reason') or '').strip()}
        if any(x is None for x in out[start:start + batch]):
            raise RuntimeError('智谱财报判定部分条目缺失（批次 %d-%d）' % (start, start + batch))
    return out


def build_earnings():
    """引擎 B：财报舆情 -> 个股参考权重。返回 dict（overview/stocks/items）。"""
    try:
        raw = fetch_announcements()
    except Exception as e:  # noqa: BLE001
        log("❌ 财报公告抓取失败: " + str(e))
        raw = []
    log(f"    -> 原始公告 {len(raw)} 条")

    today = datetime.now(CST).strftime("%Y-%m-%d")
    items = []
    for it in raw:
        title = it.get("title") or it.get("title_ch") or ""
        if not title or not is_financial(title):
            continue
        stocks = extract_stocks(it)
        if not stocks:
            continue
        if item_date(it) != today:
            continue
        items.append({"title": title, "stocks": stocks,
                      "sentiment": classify(title), "date": item_date(it),
                      "art_code": it.get("art_code") or ""})

    # 兜底：今日为空（周末/节假日/时区差）放宽到最近财报公告
    if not items:
        log("    · 今日无匹配，放宽到最近财报公告")
        for it in raw:
            title = it.get("title") or it.get("title_ch") or ""
            if not title or not is_financial(title):
                continue
            stocks = extract_stocks(it)
            if not stocks:
                continue
            items.append({"title": title, "stocks": stocks,
                          "sentiment": classify(title), "date": item_date(it),
                          "art_code": it.get("art_code") or ""})
        items = items[:50]
    log(f"    -> 财报相关 {len(items)} 条")

    # ---- 方向判定：优先智谱按标题语义（读实际含义），失败/无 key 回退关键词 classify ----
    earn_judge = {'used': False, 'model': None}
    for envk, base, model in (('ZHIPU_API_KEY', 'https://open.bigmodel.cn/api/paas/v4', 'glm-4-flash'),
                              ('SILICONFLOW_API_KEY', 'https://api.siliconflow.cn/v1', 'Qwen/Qwen3-8B')):
        key = (os.environ.get(envk) or '').strip()
        if not key:
            continue
        try:
            judged = llm_classify_earnings_titles(items, key, base, model)
        except Exception as e:  # noqa: BLE001
            judged = None
            log(f"    · 智谱财报判定失败: {e}")
        if judged is not None and len(judged) == len(items):
            for i, it in enumerate(items):
                if judged[i]:
                    it['sentiment'] = judged[i]['sentiment']
                    it['reason'] = judged[i]['reason']
            earn_judge = {'used': True, 'model': model, 'error': None}
            log(f"    -> 财报方向由智谱判定({model}) 完成")
            break
        else:
            earn_judge = {'used': False, 'model': model, 'error': 'LLM 输出解析失败/超时，回退关键词'}
            log("    · 智谱财报判定不可用，回退关键词 classify")
    if not earn_judge['used'] and not earn_judge.get('error'):
        earn_judge = {'used': False, 'model': None, 'error': '未配置可用 LLM Key'}
        log("    -> 财报方向：关键词 classify（智谱未启用/失败）")

    # 聚合个股参考权重
    agg = {}
    for it in items:
        for s in it["stocks"]:
            k = s["code"]
            a = agg.setdefault(k, {"code": k, "name": s["name"],
                                   "bull": 0, "bear": 0, "neutral": 0, "items": []})
            a[it["sentiment"]] += 1
            a["items"].append({"t": it["title"], "s": it["sentiment"]})
    for k, a in agg.items():
        tot = a["bull"] + a["bear"] + a["neutral"]
        net = a["bull"] - a["bear"]
        a["net"] = net
        a["total"] = tot
        a["weight"] = round(50 + 50 * net / tot) if tot else 50
    stocks_sorted = sorted(agg.values(), key=lambda x: (-x["net"], -x["total"]))

    overview = {
        "bull": sum(1 for i in items if i["sentiment"] == "bull"),
        "bear": sum(1 for i in items if i["sentiment"] == "bear"),
        "neutral": sum(1 for i in items if i["sentiment"] == "neutral"),
        "total": len(items),
    }
    return {"overview": overview, "stocks": stocks_sorted[:60], "items": items[:120],
            "judge": earn_judge}


# ============================== 主流程 ==============================
def main():
    run_start = datetime.now(CST)
    log("=== 财报舆情雷达 v2 抓取（北京 %s）===" % run_start.strftime("%Y-%m-%d %H:%M:%S"))
    stats = {"indices": 0, "industry": 0, "concept": 0, "stocks": 0, "hist_ok": 0, "hist_fail": 0}

    # ---- 引擎 A：板块资金流 ----
    log("① 指数 ...")
    indices = fetch_indices()
    stats["indices"] = len(indices)
    log(f"    -> 指数 {stats['indices']} 条")

    log("② 行业板块 (fs=m:90+t:2) ...")
    industry = fetch_boards("m:90+t:2", 30)
    stats["industry"] = len(industry)
    log(f"    -> 行业 {stats['industry']} 条")

    log("③ 概念板块 (fs=m:90+t:3) ...")
    concept = filter_junk_boards(fetch_boards("m:90+t:3", 30))
    stats["concept"] = len(concept)
    log(f"    -> 概念 {stats['concept']} 条")

    log("④ 个股主力净流入 TOP12 ...")
    stocks = fetch_stocks()
    stats["stocks"] = len(stocks)
    log(f"    -> 个股 {stats['stocks']} 条")

    # ④b 净流出板块 / 个股（po=0 升序抓最负值；强制走 OUT_HOSTS 中尊重 po 的 host，
    #     规避部分 host 忽略 po 导致净流出维度缺失）
    log("④b 净流出板块 / 个股 ...")
    io = fetch_on(OUT_HOSTS, "m:90+t:2", 15, 0)
    co = filter_junk_boards(fetch_on(OUT_HOSTS, "m:90+t:3", 15, 0))
    combined = io + co
    combined.sort(key=lambda b: float(b.get("f62") or 0))
    out = [b for b in combined if float(b.get("f62") or 0) < 0][:15]
    outStocks = [s for s in fetch_stocks_on(OUT_HOSTS, po=0) if float(s.get("f62") or 0) < 0][:8]
    stats["out"] = len(out)
    stats["outStocks"] = len(outStocks)
    log(f"    -> 净流出板块 {stats['out']} 条，净流出个股 {stats['outStocks']} 条")

    # ④c ETF 主力资金流（b:MK 系列全量；盘前 f62 未回填则 available=False）
    log("④c ETF 主力资金流 ...")
    etf = fetch_etf_flow()
    stats["etf"] = etf["total"]
    stats["etfAvail"] = etf["available"]
    log(f"    -> ETF {stats['etf']} 只，available={stats['etfAvail']}，净流入/净流出榜 {len(etf['topIn'])}/{len(etf['topOut'])}")

    # ④d 外盘指数（美股/港股/亚太/欧股）——供 brief.py 外盘风险分级 + AI 解读
    log("④d 外盘指数 ...")
    overseas = fetch_overseas()
    stats["overseas"] = len(overseas)
    if overseas:
        log("    -> 外盘: " + "、".join(f"{o['name']} {o['pct']}%" for o in overseas[:8]))

    # ④e 海外宏观（美债10Y/2Y收益率、利差、联邦基金利率 · FRED）——弥补"只看指数涨跌"盲区
    log("④e 海外宏观（FRED 美债/利率）...")
    macro = fetch_macro()
    stats["macro"] = "ok" if macro.get("available") else "fail"
    if macro.get("available"):
        parts = []
        if "us10y" in macro:
            parts.append("美债10Y %.2f%%" % macro["us10y"]["v"])
        if "us2y" in macro:
            parts.append("2Y %.2f%%" % macro["us2y"]["v"])
        if macro.get("spread") is not None:
            parts.append("利差 %.2f%%" % macro["spread"])
        if "fed" in macro:
            parts.append("联邦基金 %.2f%%" % macro["fed"]["v"])
        log("    -> 宏观: " + "、".join(parts))

    hist = {}
    top_boards = [b for b in industry[:HIST_TOP]] + [b for b in concept[:HIST_TOP]]
    log(f"⑤ 历史资金流（{len(top_boards)} 个板块）...")
    for b in top_boards:
        code = b.get("f12")
        if not code:
            continue
        kl = fetch_history("90." + code)
        if kl:
            hist[code] = kl
            stats["hist_ok"] += 1
        else:
            stats["hist_fail"] += 1
    log(f"    -> 历史成功 {stats['hist_ok']}，失败 {stats['hist_fail']}")

    # ---- 引擎 B：财报舆情 ----
    log("⑥ 财报舆情 ...")
    earnings = build_earnings()
    log(f"    -> 财报 利好/利空/中性 = {earnings['overview']['bull']}/{earnings['overview']['bear']}/{earnings['overview']['neutral']}，涉及 {len(earnings['stocks'])} 只")

    boards_ok = stats["indices"] and (stats["industry"] or stats["concept"])
    earn_ok = earnings["overview"]["total"] > 0
    ok = boards_ok or earn_ok
    result = {
        "updated": run_start.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "source": "server-snapshot",
        "status": "ok" if ok else "partial",
        "stats": stats,
        "indices": indices,
        "industry": industry,
        "concept": concept,
        "stocks": stocks,
        "hist": hist,
        "out": out,
        "outStocks": outStocks,
        "etf": etf,
        "overseas": overseas,
        "macro": macro,
        "earnings": earnings,
    }

    latest_path = os.path.join(ROOT, "latest.json")
    with open(latest_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, separators=(",", ":"))
    log(f"✅ 写入 latest.json（{os.path.getsize(latest_path)} 字节）")

    # 归档快照
    stamp = run_start.strftime("%Y-%m-%d-%H%M")
    snap_dir = os.path.join(ROOT, "snapshots")
    os.makedirs(snap_dir, exist_ok=True)
    with open(os.path.join(snap_dir, stamp + ".json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, separators=(",", ":"))
    with open(os.path.join(snap_dir, stamp + ".overview.json"), "w", encoding="utf-8") as f:
        json.dump({"time": result["updated"], "earnings": earnings["overview"]}, f,
                  ensure_ascii=False, separators=(",", ":"))

    # 更新索引（含板块 stats + 财报 overview，供历史回看）
    idx_path = os.path.join(snap_dir, "index.json")
    index = []
    if os.path.exists(idx_path):
        try:
            with open(idx_path, encoding="utf-8") as f:
                index = json.load(f)
        except Exception:  # noqa: BLE001
            index = []
    index.insert(0, {
        "time": result["updated"],
        "status": result["status"],
        "stats": stats,
        "earnings": earnings["overview"],
        "file": "snapshots/" + stamp + ".json",
    })
    index = index[:90]
    with open(idx_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)

    log("=== 完成：板块=%s，财报=%s ===" % (
        (stats["indices"], stats["industry"], stats["concept"], stats["stocks"]),
        earnings["overview"]))
    return 0 if ok else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        log("❌ 致命错误: " + str(e))
        err = {
            "updated": datetime.now(CST).strftime("%Y-%m-%dT%H:%M:%S+08:00"),
            "source": "server-snapshot",
            "status": "error",
            "error": str(e),
            "stats": {"indices": 0, "industry": 0, "concept": 0, "stocks": 0,
                      "hist_ok": 0, "hist_fail": 0},
            "indices": [], "industry": [], "concept": [], "stocks": [], "hist": {},
            "out": [], "outStocks": [],
            "macro": {"available": False},
            "earnings": {"overview": {"bull": 0, "bear": 0, "neutral": 0, "total": 0},
                         "stocks": [], "items": []},
        }
        with open(os.path.join(ROOT, "latest.json"), "w", encoding="utf-8") as f:
            json.dump(err, f, ensure_ascii=False, separators=(",", ":"))
        sys.exit(1)
