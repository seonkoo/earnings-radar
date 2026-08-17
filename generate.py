#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A股财报舆情雷达 —— 服务端抓取脚本（仅标准库，无第三方依赖）
抓取东方财富「财报公告」→ 判利好/利空 → 聚合「个股参考权重指数」

设计原则（沿用 wolf-screener / market-radar 工程偏好）：
  - 配置化：HOST / 关键词 / 分页集中在顶部
  - 关键步骤统计日志：原始 N -> 财报 N -> 利好/利空 N，失败原因可见
  - 多域名兜底 + 重试；单源失败不影响整体
  - 拒绝无证据：抓不到就记失败，不伪造数据
  - 诚实边界：标题关键词判定是启发式，非预测；仅作「财报舆情参考权重」
"""
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

# ============================== 配置 ==============================
ANN_HOSTS = ["np-anotice-stock.eastmoney.com"]   # 东方财富公告（带股票代码，最适合做财报舆情）
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
REFERER = "https://data.eastmoney.com/notices/"
TIMEOUT = 15
RETRY = 3
PAGE_SIZE = 200                 # 抓取最新 N 条公告（从中筛财报）
ROOT = os.path.dirname(os.path.abspath(__file__))
CST = timezone(timedelta(hours=8))   # 北京时间

# 财报/业绩相关词（用于筛选公告是否属「财报舆情」）
FIN_KW = ["业绩", "净利", "营收", "中报", "年报", "季报", "半年报", "一季报", "三季报", "快报",
          "预增", "预减", "预盈", "首亏", "扭亏", "分红", "送转", "高送转", "利润分配", "财报",
          "盈利", "亏损", "财务数据", "经营数据", "业绩预告", "业绩快报", "业绩说明会"]

# 利好词（标题命中即计利好，净利好=利好数-利空数）
BULL_KW = ["预增", "扭亏", "扭亏为盈", "大增", "高增", "超预期", "增长", "创新高", "创纪录", "高送转",
           "送转", "分红", "利润分配", "派发", "现金分红", "高分红", "股息", "回购", "中标", "签约",
           "大单", "订单", "提价", "业绩亮眼", "向好", "改善", "盈利", "提升", "上修", "预盈", "翻倍",
           "亮眼", "提振", "利好", "扩产", "新签", "份额提升", "有望"]

# 利空词
BEAR_KW = ["预减", "首亏", "续亏", "下滑", "下降", "减少", "减值", "暴雷", "退市", "立案", "罚款",
           "商誉减值", "不及预期", "下调", "亏损扩大", "变脸", "下修", "警示", "停产", "诉讼", "暴跌",
           "腰斩", "业绩变脸", "利空", "风险警示", "终止", "违约", "查封", "承压", "弱化", "ST", "*ST",
           "亏损", "降幅", "减亏", "下挫", "处罚", "降级"]

CODE_RE = re.compile(r"^\d{6}$")


def log(msg):
    ts = datetime.now(CST).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# ============================== 请求层 ==============================
def http_get_json(url, timeout=TIMEOUT, retry=RETRY):
    last_err = None
    for attempt in range(1, retry + 1):
        try:
            req = urllib.request.Request(url)
            req.add_header("User-Agent", UA)
            req.add_header("Referer", REFERER)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "ignore")
            return json.loads(raw)
        except Exception as e:  # noqa: BLE001
            last_err = e
            log(f"    · 尝试{attempt}失败: {e}")
            if attempt < retry:
                time.sleep(1.5 * attempt)
    raise last_err or RuntimeError("未知错误")


def fetch_announcements(page_size=PAGE_SIZE):
    last = None
    for host in ANN_HOSTS:
        try:
            url = ("https://" + host + "/api/security/ann?sr=-1&page_size=" + str(page_size) +
                   "&page_index=1&client_source=web")
            d = http_get_json(url)
            data = d.get("data") or {}
            return data.get("list") or []
        except Exception as e:  # noqa: BLE001
            last = e
            log(f"    · 域名 {host} 公告失败: {e}")
    raise last or RuntimeError("全部公告域名失败")


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


def main():
    run_start = datetime.now(CST)
    today = run_start.strftime("%Y-%m-%d")
    log("=== 财报舆情抓取（北京 %s）===" % run_start.strftime("%Y-%m-%d %H:%M:%S"))

    try:
        raw = fetch_announcements()
    except Exception as e:  # noqa: BLE001
        log("❌ 抓取失败: " + str(e))
        raw = []
    log(f"    -> 原始公告 {len(raw)} 条")

    # 今日（北京时间）发布的财报公告
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
        items.append({
            "title": title,
            "stocks": stocks,
            "sentiment": classify(title),
            "date": item_date(it),
            "art_code": it.get("art_code") or "",
        })

    # 兜底：若今日过滤后为空（周末/节假日/时区差），放宽到最近财报公告
    if not items:
        log("    · 今日无匹配，放宽到最近财报公告")
        for it in raw:
            title = it.get("title") or it.get("title_ch") or ""
            if not title or not is_financial(title):
                continue
            stocks = extract_stocks(it)
            if not stocks:
                continue
            items.append({
                "title": title,
                "stocks": stocks,
                "sentiment": classify(title),
                "date": item_date(it),
                "art_code": it.get("art_code") or "",
            })
        items = items[:50]

    log(f"    -> 财报相关 {len(items)} 条")

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

    stocks = sorted(agg.values(), key=lambda x: (-x["net"], -x["total"]))
    overview = {
        "bull": sum(1 for i in items if i["sentiment"] == "bull"),
        "bear": sum(1 for i in items if i["sentiment"] == "bear"),
        "neutral": sum(1 for i in items if i["sentiment"] == "neutral"),
        "total": len(items),
    }
    ok = overview["total"] > 0
    result = {
        "updated": run_start.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "source": "server-snapshot",
        "status": "ok" if ok else "empty",
        "filter_date": today,
        "overview": overview,
        "stocks": stocks[:60],
        "items": items[:120],
    }

    with open(os.path.join(ROOT, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, separators=(",", ":"))

    stamp = run_start.strftime("%Y-%m-%d-%H%M")
    snap_dir = os.path.join(ROOT, "snapshots")
    os.makedirs(snap_dir, exist_ok=True)
    with open(os.path.join(snap_dir, stamp + ".json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, separators=(",", ":"))
    with open(os.path.join(snap_dir, stamp + ".overview.json"), "w", encoding="utf-8") as f:
        json.dump({"time": result["updated"], "overview": overview}, f,
                  ensure_ascii=False, separators=(",", ":"))

    idx_path = os.path.join(snap_dir, "index.json")
    index = []
    if os.path.exists(idx_path):
        try:
            with open(idx_path, encoding="utf-8") as f:
                index = json.load(f)
        except Exception:  # noqa: BLE001
            index = []
    index.insert(0, {"time": result["updated"], "status": result["status"],
                    "overview": overview, "file": "snapshots/" + stamp + ".json"})
    index = index[:90]
    with open(idx_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)

    log("✅ 完成 状态=%s 利好/利空/中性=%d/%d/%d 涉及个股=%d" % (
        result["status"], overview["bull"], overview["bear"], overview["neutral"], len(stocks)))
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
            "overview": {"bull": 0, "bear": 0, "neutral": 0, "total": 0},
            "stocks": [], "items": [],
        }
        with open(os.path.join(ROOT, "latest.json"), "w", encoding="utf-8") as f:
            json.dump(err, f, ensure_ascii=False, separators=(",", ":"))
        sys.exit(1)
