#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
brief.py — 每日要闻简报生成器（服务端，GitHub Actions 运行）
抓取最新财经新闻 → 词库匹配筛选 → 按"进攻/平缓/避险"分组 → 输出 brief.json
数据源（多源广度，按可用情况叠加）：
  同花顺快讯（主·优先） + 财联社电报（主） + 新浪财经 7x24 滚动（主·综合补充） + 东方财富快讯（备）
无第三方依赖（仅标准库 urllib / json / re / datetime）。
"""
import json, os, re, time, urllib.request, urllib.error, datetime

# 北京时间（用于时间戳转换与生成时间标注）
BJ = datetime.timezone(datetime.timedelta(hours=8))
TODAY = datetime.datetime.now(BJ)

SINA_URL = "https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2509&num=60&page=1"
THS_URL  = "https://news.10jqka.com.cn/tapp/news/push/stock/?num=30"
CLS_URL  = "https://www.cls.cn/api/cache?name=refreshTenTelegraph&lastTime=%d"
EM_URL   = "https://newsapi.eastmoney.com/api/idx/get?type=1&page=1&page_size=60&callback=emcb"
UA_SINA = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"}
UA_THS   = {"User-Agent": "Mozilla/5.0", "Referer": "https://news.10jqka.com.cn/"}
UA_CLS   = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Referer": "https://www.cls.cn/telegraph"}
UA_EM    = {"User-Agent": "Mozilla/5.0", "Referer": "https://newsapi.eastmoney.com/"}


def http_get(url, timeout=15, headers=None):
    req = urllib.request.Request(url, headers=headers or UA_SINA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def strip_jsonp(raw, cb):
    s = raw.strip()
    if s.startswith('try{'):
        s = s[5:]
    if cb and s.startswith(cb + '('):
        s = s[len(cb) + 1:]
    dec = json.JSONDecoder()
    idx = s.find('{')
    if idx < 0:
        return '{}'
    obj, _ = dec.raw_decode(s, idx)
    return json.dumps(obj, ensure_ascii=False)


def fmt_ts(s):
    try:
        ts = int(str(s))
        if ts <= 0:
            return ''
        return datetime.datetime.fromtimestamp(ts, BJ).strftime('%m-%d %H:%M')
    except Exception:
        return str(s or '')


def parse_sina(raw):
    s = strip_jsonp(raw, 'cb123')
    try:
        obj = json.loads(s)
    except Exception:
        return []
    data = (obj.get('result') or {}).get('data') or []
    out = []
    for d in data:
        title = str(d.get('title', '') or '')
        intro = str(d.get('intro') or d.get('content') or d.get('summary') or '')
        text = (title + ' ' + intro).strip()
        if text:
            out.append({'title': title, 'brief': intro[:120], 'text': text,
                        'src': '新浪财经', 'time': fmt_ts(d.get('intime') or d.get('ctime') or '')})
    return out


def parse_em(raw):
    s = strip_jsonp(raw, 'emcb')
    try:
        obj = json.loads(s)
    except Exception:
        return []
    data = (obj.get('data') or {}).get('list') or []
    out = []
    for d in data:
        title = str(d.get('title') or d.get('name') or '')
        brief = str(d.get('summary') or d.get('content') or '')
        text = (title + ' ' + brief).strip()
        if text:
            out.append({'title': title, 'brief': brief[:120], 'text': text,
                        'src': '东方财富', 'time': str(d.get('datetime') or d.get('date') or '')})
    return out


def parse_ths(raw):
    """同花顺快讯：{code,msg,time,data:{list:[{title,digest,...}]}}"""
    try:
        obj = json.loads(raw)
    except Exception:
        return []
    data = (obj.get('data') or {}).get('list') or []
    out = []
    for d in data:
        if not isinstance(d, dict):
            continue
        title = str(d.get('title') or '')
        digest = str(d.get('digest') or '')
        text = (title + ' ' + digest).strip()
        if text:
            out.append({'title': title, 'brief': digest[:120], 'text': text,
                        'src': '同花顺', 'time': str(d.get('date') or d.get('time') or '')})
    return out


def parse_cls(raw):
    """财联社电报：服务端缓存代理 /api/cache?name=refreshTenTelegraph → {errno,data:{l:{id:{...}}}}"""
    try:
        obj = json.loads(raw)
    except Exception:
        return []
    data = obj.get('data') or {}
    l = data.get('l') or {}
    if not isinstance(l, dict):
        l = {}
    out = []
    for it in l.values():
        if not isinstance(it, dict):
            continue
        title = str(it.get('title') or '')
        content = str(it.get('content') or it.get('brief') or '')
        brief = str(it.get('brief') or content)
        text = ((title + ' ' + content).strip()) if title else content.strip()
        if not text:
            continue
        ctime = it.get('ctime') or it.get('time') or 0
        out.append({'title': (title or brief[:40]), 'brief': brief[:120], 'text': text,
                    'src': '财联社', 'time': fmt_ts(ctime)})
    return out


def dedup_items(items):
    """跨源去重：标题完全相同，或一条标题包含另一条标题(>=12字) 视为同一事件，保留先到源。"""
    keep = []
    for it in items:
        t = it['title'].strip()
        dup = False
        for k in keep:
            kt = k['title'].strip()
            if t and kt and (t == kt or (len(t) >= 12 and t in kt) or (len(kt) >= 12 and kt in t)):
                dup = True
                break
        if not dup:
            keep.append(it)
    return keep


def call_llm(system, user, api_key, base_url, model, timeout=50, max_tokens=1200):
    """OpenAI 兼容 chat/completions，仅用标准库（无第三方依赖）。"""
    if not api_key:
        return None, 'no api key'
    url = base_url.rstrip('/') + '/chat/completions'
    body = json.dumps({
        'model': model,
        'messages': [{'role': 'system', 'content': system},
                     {'role': 'user', 'content': user}],
        'temperature': 0.3,
        'max_tokens': max_tokens,
        'response_format': {'type': 'json_object'}
    }).encode('utf-8')
    req = urllib.request.Request(url, data=body, headers={
        'Content-Type': 'application/json',
        'Authorization': 'Bearer ' + api_key,
        'User-Agent': 'mr-brief/1.0'
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode('utf-8', 'ignore'))
        return data['choices'][0]['message']['content'], None
    except Exception as e:
        return None, str(e)


def llm_summarize(top, regime_label, summary_kw, api_key, base_url, model, etf_flow=None, overseas=None, macro=None):
    """用 LLM 把 top 新闻压缩成综述 + 逐条要点；只基于给定新闻，不预测不荐股。"""
    if not top:
        return {'used': False, 'error': 'empty items'}
    lines = []
    for i, m in enumerate(top, 1):
        secs = '、'.join((m.get('secs') or [])[:4]) or '—'
        lines.append('%d. [%s] %s | 摘要:%s | 涉及:%s' % (
            i, m.get('macro_dir', ''), m.get('title', ''), m.get('brief', ''), secs))
    user = ('当前市场基调（词库判定）：%s\n词库综述：%s\n\n'
            '以下是当日高权重新闻（按重要性排序），请仅基于这些条目做摘要：\n%s\n\n'
            '要求：只依据上述新闻，不预测后市涨跌、不给出任何买卖/仓位建议、不编造新闻之外内容。\n'
            '输出严格 JSON：{"summary":"一句总览，须与给定基调方向一致并点出主线",'
            '"points":["第1条一句话市场解读(含利好/利空/中性方向、涉及板块；若新闻指向明确的行业主线，'
            '可附 1-3 个代表性个股与相关ETF，格式：代表个股：简称(代码)、…；相关ETF：简称(代码)，'
            '不明确则省略个股/ETF 部分)","第2条...",...]}\n'
            'points 数组长度须与新闻条数相同、顺序一致。') % (regime_label, summary_kw, '\n'.join(lines))
    if etf_flow and etf_flow.get('available'):
        def _yi(v):
            try:
                return f"{float(v) / 1e8:+.2f}亿"
            except Exception:  # noqa: BLE001
                return '—'
        byg = '；'.join(f"{g} {_yi(etf_flow['byGroup'].get(g, 0))}" for g in etf_flow.get('byGroup', {}))
        top_out = '、'.join(f"{r['name']}({r['code']}){_yi(r.get('f62'))}" for r in etf_flow.get('topOut', [])[:3])
        top_in = '、'.join(f"{r['name']}({r['code']}){_yi(r.get('f62'))}" for r in etf_flow.get('topIn', [])[:3])
        user += (
            '\n\n补充数据·ETF 主力资金（口径：二级市场主力净流入，非申赎；单位亿元）：\n'
            f'板块汇总：{byg}。\n'
            f'净流出榜 TOP3：{top_out}。\n'
            f'净流入榜 TOP3：{top_in}。\n'
            '请在总览 summary 中提及 ETF 资金面取向（例如资金由股转债 / 撤离宽基 / 加仓跨境等，'
            '仅基于以上数字，不得编造具体金额）。'
        )
    if overseas:
        ov_parts = []
        us_up = []
        for it in overseas:
            if it.get('pct') is None:
                continue
            ov_parts.append('%s %+.2f%%' % (it.get('name') or it.get('code'), it.get('pct')))
            if it.get('name') in ('道琼斯', '标普500', '纳斯达克') and it.get('pct', 0) >= 1:
                us_up.append('%s %+.2f%%' % (it.get('name'), it.get('pct')))
        if ov_parts:
            user += (
                '\n\n补充数据·外盘行情（美股为前一日收盘，港股/亚太/欧股为最新交易日）：\n'
                '，'.join(ov_parts) + '。\n'
                '若外盘明显走弱（亚太指数跌超 3% 或美股跌超 1.5%）或已现熔断，请在 summary 中提示外部风险对 A 股的传导；'
                '若美股明显走强（≥+1%，如 ' + ('、'.join(us_up) if us_up else '道指/标普/纳指上涨') + '）也可提示外部提振，'
                '但不得凭空编造外盘数字。'
            )
    if macro and macro.get('available'):
        parts = []
        if macro.get('us10y'):
            m = macro['us10y']
            parts.append('美债10Y %.2f%%（日变动 %+.2f%%）' % (m['v'], m.get('chg') or 0))
        if macro.get('us2y'):
            m = macro['us2y']
            parts.append('美债2Y %.2f%%（日变动 %+.2f%%）' % (m['v'], m.get('chg') or 0))
        if macro.get('spread') is not None:
            inv = '（倒挂）' if macro['spread'] < 0 else ''
            parts.append('10Y-2Y 利差 %.2f%%%s' % (macro['spread'], inv))
        if macro.get('fed'):
            m = macro['fed']
            parts.append('联邦基金利率 %.2f%%（日变动 %+.2f%%）' % (m['v'], m.get('chg') or 0))
        if parts:
            user += (
                '\n\n补充数据·海外宏观（来源 FRED）：\n' + '；'.join(parts) + '。\n'
                '请在 summary 中点评美债/利率取向：美债收益率上行或利差走阔通常压制成长股估值、偏 risk-off；'
                '10Y-2Y 利差倒挂是衰退预警；美联储加息/缩表偏收紧。仅基于以上数字，不得编造。'
            )
    system = ('你是 A股 市场新闻摘要助手。把给定的财经新闻条目用中文压缩成客观、可追溯的每日综述与逐条要点。'
              '规则：1) 只能基于提供的新闻，不得编造；2) 不得预测后市涨跌、不得给出任何买卖/仓位建议；'
              '3) 逐条要点须指出该新闻对市场的方向(利好/利空/中性)及主要涉及板块；'
              '4) 若新闻指向明确的行业主线，可附 1-3 个代表性个股与相关ETF（格式：代表个股：简称(代码)、…；'
              '相关ETF：简称(代码)）；只列真实存在且高度确信的标的，新闻未提及或不确定就省略，'
              '严禁编造个股名称或代码；'
              '5) 总览句须与给定的市场基调一致。输出必须是合法 JSON，不要任何解释文字。')
    content, err = call_llm(system, user, api_key, base_url, model)
    if not content:
        return {'used': False, 'error': err or 'empty'}
    txt = content.strip()
    if txt.startswith('```'):
        txt = re.sub(r'^```[a-zA-Z]*\n?', '', txt)
        txt = txt.rstrip('`').strip()
    try:
        obj = json.loads(txt)
    except Exception:
        m = re.search(r'\{.*\}', content, re.S)
        if not m:
            return {'used': False, 'error': 'json parse fail'}
        try:
            obj = json.loads(m.group(0))
        except Exception as e:
            return {'used': False, 'error': 'json parse fail: %s' % e}
    summary = (obj.get('summary') or '').strip()
    points = obj.get('points') or []
    if not isinstance(points, list):
        points = []
    for i, m in enumerate(top):
        note = points[i] if i < len(points) else ''
        if note and isinstance(note, str):
            m['ai_note'] = note.strip()
    return {'used': bool(summary or points), 'model': model, 'summary': summary, 'error': None}


# 熔断/重挫阈值：韩国 KOSPI side-1 熔断为 -8%（side-2 -15% / side-3 -20%）；
# 日本为波动熔断，这里以 -8% 标注"日股重挫"。触及时视为外部系统性风险。
CIRCUIT_KOSPI = -8.0
CIRCUIT_NIKKEI = -8.0


def llm_classify_items(items, api_key, base_url, model, batch=10):
    """智谱按实际内容（标题+摘要）批量判定新闻方向。返回与 items 等长的 list[dict]。

    字段：dir(on/off/flat)、strength(1-5)、secs(板块)、cat(类别)、reason(一句理由)。
    单批缺失 ≤ 20% 时按中性补齐（宽容）；超过则 raise RuntimeError（调用方整体降级词库并记录原因）。
    """
    if not items:
        return []
    out = [None] * len(items)
    for start in range(0, len(items), batch):
        chunk = items[start:start + batch]
        lines = []
        for k, it in enumerate(chunk):
            title = (it.get('title') or '').strip().replace('\n', ' ')[:80]
            brief = (it.get('brief') or it.get('text') or '').strip().replace('\n', ' ')[:100]
            lines.append('%d. %s｜%s' % (start + k, title, brief))
        user = (
            '以下是当日 A股财经新闻（编号. 标题｜摘要），请逐条判断其对 A股市场的方向与影响：\n%s\n\n'
            '要求：仅依据给定文本，不预测涨跌、不荐股、不编造。输出严格 JSON（不要任何解释文字）：\n'
            '{"items":[{"i":0,"dir":"on","strength":3,"secs":["半导体"],"cat":"产业","reason":"一句话理由"},...]}\n'
            '字段说明：dir=on(利好/偏多) 或 off(利空/偏空) 或 flat(中性)；strength=影响强度 1-5；'
            'secs=涉及的 A股板块名（最多 2 个，中文）；cat=类别（产业/政策/宏观/海外/资金/风险/外围/其他）；'
            'reason=判断依据（不超过 20 字）。注意语义而非关键词：如"不及预期""承压""待落地"应判利空或中性，'
            '"回购/中标/超预期/放量"判利好。若新闻主体是境外公司（如海力士/英伟达/三星/台积电/特斯拉等）'
            '对 A股 的联动，cat 用"外围"。务必逐条输出、与输入条数完全一致、一条都不能少，每条 JSON 保持紧凑。'
        ) % '\n'.join(lines)
        system = ('你是 A股 市场新闻研判助手。只依据给定新闻文本客观判断利好/利空/中性及影响，'
                  '不预测、不荐股。输出必须为合法 JSON。')
        content, err = None, None
        for _t in range(2):   # 抗瞬时超时/截断：重试一次
            content, err = call_llm(system, user, api_key, base_url, model, timeout=90, max_tokens=4000)
            if content:
                break
        if not content:
            raise RuntimeError('智谱判定调用失败: %s' % (err or 'empty'))
        txt = content.strip()
        if txt.startswith('```'):
            txt = re.sub(r'^```[a-zA-Z]*\n?', '', txt)
            txt = txt.rstrip('`').strip()
        try:
            obj = json.loads(txt)
        except Exception:
            m = re.search(r'\{.*\}', content, re.S)
            if not m:
                raise RuntimeError('智谱判定输出非法 JSON: %s' % content[:200])
            try:
                obj = json.loads(m.group(0))
            except Exception:
                raise RuntimeError('智谱判定输出非法 JSON: %s' % content[:200])
        arr = obj.get('items') or []
        if not isinstance(arr, list):
            raise RuntimeError('智谱判定 items 非数组: %s' % content[:300])
        for r in arr:
            i = r.get('i')
            if not isinstance(i, int) or not (0 <= i < len(items)):
                continue
            d = str(r.get('dir') or 'flat').strip().lower()
            if d not in ('on', 'off', 'flat'):
                continue
            secs = [str(s).strip() for s in (r.get('secs') or []) if str(s).strip()][:2]
            cat = str(r.get('cat') or '其他').strip() or '其他'
            try:
                strength = max(1, min(5, int(r.get('strength') or 3)))
            except Exception:  # noqa: BLE001
                strength = 3
            out[i] = {'dir': d, 'strength': strength, 'secs': secs, 'cat': cat,
                      'reason': str(r.get('reason') or '').strip()}
        if any(x is None for x in out[start:start + batch]):
            # 宽容补齐：缺失 ≤ 20% 按中性处理；超过则整体失败
            missing = [i for i in range(start, start + batch) if out[i] is None]
            if len(missing) <= max(1, int(len(chunk) * 0.2)):
                for mi in missing:
                    out[mi] = {'dir': 'flat', 'strength': 1, 'secs': [], 'cat': '其他',
                               'reason': '模型未输出，按中性处理'}
            else:
                raise RuntimeError('智谱判定缺失过多: 期望≥%d 实得%d | 输出:%s'
                                   % (len(chunk), len(chunk) - len(missing), content[:300]))
    return out


def build_matched_from_judged(items, judged):
    """把智谱判定结果映射为 matched（与词库 item_scan 输出同构），按强度降序。"""
    matched = []
    for i, it in enumerate(items):
        j = judged[i] if i < len(judged) else None
        if not j:
            continue
        d = j['dir']
        st = j['strength']
        matched.append({
            'title': it.get('title', ''), 'brief': it.get('brief', ''),
            'src': it.get('src', ''), 'time': it.get('time', ''),
            'weight': st, 'on': st if d == 'on' else 0, 'off': st if d == 'off' else 0,
            'macro_dir': d, 'secs': j['secs'], 'kws': [],
            'cats': {j['cat']: (st if d != 'flat' else 0)},
            'judge_note': j.get('reason', ''),
        })
    matched.sort(key=lambda x: x['weight'], reverse=True)
    return matched


def oversea_risk_level(indices):
    """外盘风险分级 + 熔断检测。

    熔断(3)：韩国KOSPI ≤ -8%（side-1 熔断）或 日经225 ≤ -8%（重挫）
    强(2)：韩国KOSPI 或 日经225 ≤ -3%（但未触熔断）
    弱(1)：美股三大任一 ≤ -1.5%，或 恒生 ≤ -2%，或 欧股 ≤ -2%
    返回 (level, hits, circuit, circuit_name)
    """
    if not indices:
        return 0, [], False, None
    level, hits = 0, []
    circuit = False
    circuit_name = None
    for it in indices:
        name = it.get('name') or ''
        pct = it.get('pct')
        if pct is None:
            continue
        if name == '韩国KOSPI' and pct <= CIRCUIT_KOSPI:
            level = 3
            circuit = True
            circuit_name = '韩股熔断（一级）'
            hits.append('%s %+.2f%%' % (name, pct))
        elif name == '日经225' and pct <= CIRCUIT_NIKKEI:
            level = 3
            circuit = True
            circuit_name = '日股重挫'
            hits.append('%s %+.2f%%' % (name, pct))
        elif name in ('韩国KOSPI', '日经225') and pct <= -3:
            level = max(level, 2)
            hits.append('%s %+.2f%%' % (name, pct))
        elif name in ('道琼斯', '标普500', '纳斯达克') and pct <= -1.5:
            level = max(level, 1)
            hits.append('%s %+.2f%%' % (name, pct))
        elif name == '恒生指数' and pct <= -2:
            level = max(level, 1)
            hits.append('%s %+.2f%%' % (name, pct))
        elif name in ('欧洲斯托克50', '德国DAX30') and pct <= -2:
            level = max(level, 1)
            hits.append('%s %+.2f%%' % (name, pct))
    return level, hits[:5], circuit, circuit_name


def indices_of(text, kw):
    out = []
    i = 0
    lc = text.lower()
    k = kw.lower() if re.match(r'^[a-z0-9\s]+$', kw, re.I) else kw
    if not kw:
        return out
    while True:
        i = lc.find(k, i)
        if i < 0:
            break
        out.append(i)
        i += max(1, len(k))
    return out


def item_scan(items, lex):
    neg = lex.get('negators', [])
    den = lex.get('deniers', [])
    neg_terms = lex.get('negTerms', [])
    strong_neg = set(lex.get('strongNeg', []))
    matched = []
    for it in items:
        text = it['text']
        # 每个关键词只计一次（取首次出现）；否定词在关键词之前出现即翻转方向
        seen = {}
        for k in lex.get('keywords', []):
            pps = indices_of(text, k['kw'])
            if not pps:
                continue
            pp = pps[0]
            before = text[:pp]
            dirn = k['dir']
            if dirn > 0 and any(n in before for n in den):
                continue  # 利好被否定（暂未/否认…）→ 该关键词整条失效
            if dirn > 0 and any(n in before for n in neg):
                dirn = -dirn
            if k['kw'] in seen:
                continue
            seen[k['kw']] = {'dir': dirn, 'macro': k.get('macro', 0),
                             'w': k.get('w', 1), 'secs': k.get('secs', []), 'cat': k.get('cat', '')}
        hits = list(seen.values())
        if not hits:
            continue
        pos = sum(h['w'] for h in hits if h['dir'] > 0)
        negw = sum(h['w'] for h in hits if h['dir'] < 0)
        wsum = pos + negw
        neg_count = sum(1 for t in neg_terms if t in text)
        net = pos - negw - 2 * neg_count
        md = 'on' if net > 0 else ('off' if net < 0 else 'flat')
        # 强利空事件词（退市/暴雷/制裁…）：除非被 denier 否定，否则直接判避险
        denied = any(n in text for n in den)
        if any(t in text for t in strong_neg) and not denied:
            md = 'off'
        secs = []
        for h in hits:
            for s in h['secs']:
                if s not in secs:
                    secs.append(s)
        # 按词库 cat 维度聚合（政策/海外/宏观/产业…），用于前端展示多维影响因素
        cats = {}
        for h in hits:
            c = h.get('cat') or '其他'
            cats[c] = cats.get(c, 0) + h['dir'] * h['w']
        kws = list(seen.keys())
        matched.append({'title': it['title'], 'brief': it['brief'], 'src': it['src'],
                        'time': it.get('time', ''), 'weight': wsum, 'on': pos, 'off': negw,
                        'macro_dir': md, 'secs': secs, 'kws': kws, 'cats': cats})
    matched.sort(key=lambda x: x['weight'], reverse=True)
    return matched


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    lex_path = os.path.join(here, 'lexicon.json')
    if os.path.exists(lex_path):
        lex = json.load(open(lex_path, encoding='utf-8'))
    else:
        lex = {"negators": ["严查", "打击", "收紧"], "deniers": ["暂未", "未落地", "落空"],
               "negatorWindow": 10, "keywords": []}

    # ETF 主力资金流 + 外盘指数 + 海外宏观（来自同目录 latest.json，由 generate.py 先跑生成；供 AI 解读/regime 参考）
    etf_flow = None
    overseas = None
    macro = None
    latest_path = os.path.join(here, 'latest.json')
    if os.path.exists(latest_path):
        try:
            lj = json.load(open(latest_path, encoding='utf-8'))
            etf_flow = lj.get('etf')
            overseas = lj.get('overseas')
            macro = lj.get('macro')
        except Exception:  # noqa: BLE001
            etf_flow, overseas, macro = None, None, None

    items = []
    src_stat = []
    # 主源 1：同花顺快讯（A股靶向，优先）
    try:
        raw = http_get(THS_URL, headers=UA_THS)
        ths = parse_ths(raw)
        items = items + ths
        src_stat.append('同花顺 %d 条' % len(ths))
    except Exception as e:
        src_stat.append('同花顺 失败: %s' % e)
    # 主源 2：财联社电报（服务端缓存代理，无需签名）
    try:
        import time as _time
        raw = http_get(CLS_URL % int(_time.time()), headers=UA_CLS)
        cls = parse_cls(raw)
        items = items + cls
        src_stat.append('财联社 %d 条' % len(cls))
    except Exception as e:
        src_stat.append('财联社 失败: %s' % e)
    # 主源 3：新浪财经 7x24（综合广度补充）
    try:
        raw = http_get(SINA_URL, headers=UA_SINA)
        sina = parse_sina(raw)
        items = items + sina
        src_stat.append('新浪财经 %d 条' % len(sina))
    except Exception as e:
        src_stat.append('新浪财经 失败: %s' % e)
    # 跨源去重（避免同一事件被多源重复加权）
    before = len(items)
    items = dedup_items(items)
    src_stat.append('去重 %d→%d' % (before, len(items)))
    # 备源：东方财富（主源偏少时补）
    if len(items) < 30:
        try:
            raw = http_get(EM_URL, headers=UA_EM)
            em = parse_em(raw)
            items = items + em
            src_stat.append('东方财富 %d 条' % len(em))
        except Exception as e:
            src_stat.append('东方财富 失败: %s' % e)

    matched = None
    top = None

    # ---- 方向判定：优先智谱（读实际内容语义），失败 / 无 key 回退词库 ----
    llm_judge = {'used': False, 'model': None, 'error': None}
    judge_providers = [
        ('ZHIPU_API_KEY', 'https://open.bigmodel.cn/api/paas/v4', 'glm-4-flash'),
        ('SILICONFLOW_API_KEY', 'https://api.siliconflow.cn/v1', 'Qwen/Qwen3-8B'),
    ]
    judged = None
    judge_err = '未配置可用 LLM Key'
    for envk, base, model in judge_providers:
        key = (os.environ.get(envk) or '').strip()
        if not key:
            continue
        try:
            judged = llm_classify_items(items, key, base, model)
            judge_err = 'LLM 判定输出解析失败/超时，回退词库'
        except Exception as e:  # noqa: BLE001
            judged = None
            judge_err = str(e)
            llm_judge = {'used': False, 'model': model, 'error': str(e)}
        if judged is not None and len(judged) == len(items):
            llm_judge = {'used': True, 'model': model, 'error': None}
            break
    if not llm_judge['used']:
        llm_judge = {'used': False, 'model': llm_judge['model'], 'error': judge_err}
    if llm_judge['used']:
        # 智谱判定：全部新闻按方向 + 强度进入 matched（kws 留空，由 secs 驱动综述）
        matched = build_matched_from_judged(items, judged)
        judge_suffix = ' · 🤖智谱判定(%s)' % llm_judge['model']
        log(f"    -> 方向由智谱判定（{len(matched)} 条，on={sum(m['on'] for m in matched)} / "
            f"off={sum(m['off'] for m in matched)}）")
    else:
        matched = item_scan(items, lex)
        judge_suffix = ''
    top = matched[:14]

    on = sum(m['on'] for m in matched)
    off = sum(m['off'] for m in matched)
    total = on + off
    net = on - off
    ratio = net / total if total > 0 else 0
    if total < 3:
        regime, label, strength = 'flat', '平缓（信号不足）', 0
    elif ratio > 0.15:
        regime, label, strength = 'on', '进攻', min(1, ratio)
    elif ratio < -0.15:
        regime, label, strength = 'off', '避险 / 下跌风险', min(1, abs(ratio))
    else:
        regime, label, strength = 'flat', '平缓', abs(ratio)

    # ---- 外盘风险加权：词库判定后，把外盘大跌作为风险项折算进 off ----
    # 弱风险(+25%)：美股跌≥1.5% 或 恒生/欧股跌≥2%；强风险(+50% 且进攻封顶为平缓)：KOSPI/日经跌≥3%
    # 设计理由：外盘风险是"给进攻信心打折"而非"直接反转"，只有词库本身偏空才可能翻避险；
    # 强风险下禁止判"进攻"（亚太系统性风险日），最多给"平缓 · 外盘风险"。
    risk_level, risk_hits, circuit, circuit_name = oversea_risk_level(overseas)
    risk_note = '；'.join(risk_hits)
    if circuit:
        # 熔断/重挫：外部系统性风险，无视词库基调，直接判避险
        regime, label, strength = 'off', (circuit_name or '外盘熔断') + ' · 强制避险', 1.0
        off = on + off
        on = 0
    elif risk_level >= 1:
        bias = 0.25 if risk_level == 1 else 0.50
        off += (on + off) * bias
        total = on + off
        net = on - off
        ratio = net / total if total > 0 else 0
        if ratio > 0.15:
            regime, label, strength = 'on', '进攻', min(1, ratio)
        elif ratio < -0.15:
            regime, label, strength = 'off', '避险 / 下跌风险', min(1, abs(ratio))
        else:
            regime, label, strength = 'flat', '平缓', abs(ratio)
        if risk_level >= 2 and regime == 'on':
            regime, label = 'flat', '平缓 · 外盘风险'

    # 综述：汇总 top drivers（智谱模式按板块聚合，词库模式按关键词宏观轴聚合）
    if llm_judge['used']:
        on_secs, off_secs = {}, {}
        for m in matched:
            bucket = on_secs if m['on'] > 0 else (off_secs if m['off'] > 0 else None)
            if bucket is None:
                continue
            for s in m['secs']:
                bucket[s] = bucket.get(s, 0) + 1
        on_drivers = sorted(on_secs, key=on_secs.get, reverse=True)[:5]
        off_drivers = sorted(off_secs, key=off_secs.get, reverse=True)[:5]
        sp = []
        if on_drivers:
            sp.append('进攻板块：' + '、'.join(on_drivers))
        if off_drivers:
            sp.append('避险板块：' + '、'.join(off_drivers))
        summary_kw = '；'.join(sp) if sp else '智谱判定：当日新闻未见显著方向。'
    else:
        on_kw, off_kw = {}, {}
        for m in matched:
            for k in m['kws']:
                mk = [x for x in lex.get('keywords', []) if x['kw'] == k]
                mac = mk[0].get('macro', 0) if mk else 0
                if mac == 1:
                    on_kw[k] = on_kw.get(k, 0) + 1
                elif mac == -1:
                    off_kw[k] = off_kw.get(k, 0) + 1
        on_drivers = sorted(on_kw, key=on_kw.get, reverse=True)[:5]
        off_drivers = sorted(off_kw, key=off_kw.get, reverse=True)[:5]
        sp = []
        if on_drivers:
            sp.append('进攻线索：' + '、'.join(on_drivers))
        if off_drivers:
            sp.append('避险线索：' + '、'.join(off_drivers))
        summary_kw = '；'.join(sp) if sp else '当日新闻未触发显著关键词信号。'
    summary = summary_kw

    # 多维影响因素（政策 / 海外 / 宏观 / 产业 …）按 cat 聚合
    factors = {}
    for m in matched:
        for c, score in m.get('cats', {}).items():
            rec = factors.setdefault(c, {'on': 0.0, 'off': 0.0, 'net': 0.0})
            if score > 0:
                rec['on'] += score
            else:
                rec['off'] += abs(score)
            rec['net'] += score
    for rec in factors.values():
        total = rec['on'] + rec['off']
        rec['on'] = round(rec['on'], 1)
        rec['off'] = round(rec['off'], 1)
        rec['net'] = round(rec['net'], 1)
        rec['total'] = round(total, 1)
        rec['strength'] = round(abs(rec['net']) / total, 2) if total > 0 else 0
        rec['dir'] = 'on' if rec['net'] > 0 else ('off' if rec['net'] < 0 else 'flat')

    # ---- LLM 辅助简报（可选；无 key / 失败自动回退词库，绝不阻塞主流程） ----
    llm = {'used': False, 'model': None, 'error': None}
    providers = [
        ('ZHIPU_API_KEY', 'https://open.bigmodel.cn/api/paas/v4', 'glm-4-flash'),
        ('SILICONFLOW_API_KEY', 'https://api.siliconflow.cn/v1', 'Qwen/Qwen3-8B'),
    ]
    for envk, base, model in providers:
        key = (os.environ.get(envk) or '').strip()
        if not key:
            continue
        res = None
        # 抗瞬时超时：失败后重试一次（LLM 是增强项，失败自动回退词库，绝不阻塞）
        for _attempt in range(2):
            try:
                res = llm_summarize(top, label, summary_kw, key, base, model, etf_flow, overseas, macro)
            except Exception as e:
                res = {'used': False, 'error': str(e)}
            if res and res.get('used'):
                break
            time.sleep(3)
        if res and res.get('used'):
            llm = {'used': True, 'model': model, 'error': res.get('error')}
            if res.get('summary'):
                summary = res['summary']
            break
        else:
            llm = {'used': False, 'model': model, 'error': (res or {}).get('error')}
    src_suffix = (' · 🤖AI辅助(%s)' % llm['model']) if llm['used'] else ''
    src_suffix = judge_suffix + src_suffix
    if not (llm_judge['used'] or llm['used']):
        src_suffix = ' · 词库生成'

    # 全球个股联动（词库 cat=外围 命中，如海力士/英伟达/三星/台积电 → A股板块）
    global_items = []
    for m in top:
        if '外围' in (m.get('cats') or {}):
            global_items.append({'title': m['title'], 'brief': m['brief'], 'dir': m['macro_dir'],
                                 'secs': m['secs'], 'kws': m['kws']})

    brief = {
        'generated_at': TODAY.strftime('%Y-%m-%d %H:%M'),
        'date': TODAY.strftime('%Y-%m-%d'),
        'source': ' | '.join(src_stat) + src_suffix,
        'regime': {'regime': regime, 'label': label, 'onScore': round(on, 1),
                   'offScore': round(off, 1), 'strength': round(strength, 2)},
        'overseaRisk': {'level': risk_level, 'hits': risk_hits, 'note': risk_note,
                        'circuit': circuit, 'circuit_name': circuit_name},
        'macro': macro,
        'globalItems': global_items,
        'summary': summary,
        'summary_kw': summary_kw,
        'factors': factors,
        'llm': llm,
        'llm_judge': llm_judge,
        'items': [{'title': m['title'], 'brief': m['brief'], 'dir': m['macro_dir'],
                   'secs': m['secs'], 'kws': m['kws'], 'src': m['src'], 'time': m['time'],
                   'weight': m['weight'], 'ai_note': m.get('ai_note', ''),
                   'judge_note': m.get('judge_note', '')} for m in top]
    }
    out_path = os.path.join(here, 'brief.json')
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(brief, f, ensure_ascii=False, indent=1)

    # 统计日志（GitHub Actions 可见）
    print('=== 每日要闻简报生成 ===')
    print('源：%s' % ' | '.join(src_stat))
    print('新闻总数 %d → 命中关键词 %d 条 → 取 top %d' % (len(items), len(matched), len(top)))
    print('进攻分 %.1f / 避险分 %.1f → 判定：%s (强度 %.0f%%)' % (on, off, label, strength * 100))
    print('综述：%s' % summary)
    for i, m in enumerate(top[:8], 1):
        print('  %d. [%s] %s — %s' % (i, m['macro_dir'], m['title'][:50], '、'.join(m['kws'][:4])))


if __name__ == '__main__':
    main()
