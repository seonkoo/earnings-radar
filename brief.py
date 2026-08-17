#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
brief.py — 每日要闻简报生成器（服务端，GitHub Actions 运行）
抓取最新财经新闻 → 词库匹配筛选 → 按"进攻/平缓/避险"分组 → 输出 brief.json
数据源（多源广度，按可用情况叠加）：
  同花顺快讯（主·优先） + 财联社电报（主） + 新浪财经 7x24 滚动（主·综合补充） + 东方财富快讯（备）
无第三方依赖（仅标准库 urllib / json / re / datetime）。
"""
import json, os, re, urllib.request, urllib.error, datetime

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


def call_llm(system, user, api_key, base_url, model, timeout=50):
    """OpenAI 兼容 chat/completions，仅用标准库（无第三方依赖）。"""
    if not api_key:
        return None, 'no api key'
    url = base_url.rstrip('/') + '/chat/completions'
    body = json.dumps({
        'model': model,
        'messages': [{'role': 'system', 'content': system},
                     {'role': 'user', 'content': user}],
        'temperature': 0.3,
        'max_tokens': 1200,
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


def llm_summarize(top, regime_label, summary_kw, api_key, base_url, model):
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
            '"points":["第1条一句话市场解读(含利好/利空/中性方向与涉及板块)","第2条...",...]}\n'
            'points 数组长度须与新闻条数相同、顺序一致。') % (regime_label, summary_kw, '\n'.join(lines))
    system = ('你是 A股 市场新闻摘要助手。把给定的财经新闻条目用中文压缩成客观、可追溯的每日综述与逐条要点。'
              '规则：1) 只能基于提供的新闻，不得编造；2) 不得预测后市涨跌、不得给出任何买卖/仓位建议；'
              '3) 逐条要点须指出该新闻对市场的方向(利好/利空/中性)及主要涉及板块；'
              '4) 总览句须与给定的市场基调一致。输出必须是合法 JSON，不要任何解释文字。')
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
        kws = list(seen.keys())
        matched.append({'title': it['title'], 'brief': it['brief'], 'src': it['src'],
                        'time': it.get('time', ''), 'weight': wsum, 'on': pos, 'off': negw,
                        'macro_dir': md, 'secs': secs, 'kws': kws})
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

    matched = item_scan(items, lex)
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

    # 综述：汇总 top drivers（按 macro 归类）
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
        try:
            res = llm_summarize(top, label, summary_kw, key, base, model)
        except Exception as e:
            res = {'used': False, 'error': str(e)}
        if res and res.get('used'):
            llm = {'used': True, 'model': model, 'error': res.get('error')}
            if res.get('summary'):
                summary = res['summary']
            break
        else:
            llm = {'used': False, 'model': model, 'error': (res or {}).get('error')}
    src_suffix = (' · 🤖AI辅助(%s)' % llm['model']) if llm['used'] else ' · 词库生成'

    brief = {
        'generated_at': TODAY.strftime('%Y-%m-%d %H:%M'),
        'date': TODAY.strftime('%Y-%m-%d'),
        'source': ' | '.join(src_stat) + src_suffix,
        'regime': {'regime': regime, 'label': label, 'onScore': round(on, 1),
                   'offScore': round(off, 1), 'strength': round(strength, 2)},
        'summary': summary,
        'summary_kw': summary_kw,
        'llm': llm,
        'items': [{'title': m['title'], 'brief': m['brief'], 'dir': m['macro_dir'],
                   'secs': m['secs'], 'kws': m['kws'], 'src': m['src'], 'time': m['time'],
                   'weight': m['weight'], 'ai_note': m.get('ai_note', '')} for m in top]
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
