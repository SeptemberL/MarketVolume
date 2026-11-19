#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
上证指数 当日量能监控 - 核心计算模块
==========================================
功能(对标同花顺大盘量能, 全市场沪深京口径):
  1. 全市场总成交额(沪+深+北) vs 昨日同一时刻真实值 -> 放量 / 缩量 / 平量
  2. 分钟级节奏 PaceTracker(vs 1分钟前): 每分钟采样一次, 本分钟增量 vs 上分钟增量
     -> 盘中瞬时节奏(放量/缩量); 节奏翻转仅写入 trans_log 供详情面板查历史
  3. 预测当日全天成交额(按昨日真实量能分布外推)
  4. 成交量对比(沪深口径): 今日累计 vs 昨日同时刻累计(昨日1分钟线对齐) -> 放量倍数/差额(百万~千万手)
  5. 预测当日全天成交量(昨日分布法外推)
  上证指数点位作为"市场温度计"单独显示 —— 2026-09-11 起扩展为
  沪/创/科 三大指数(上证指数/创业板指/科创50)缩写展示, 仅取点位+涨跌幅

数据源(腾讯, 不封IP, 唯一来源保证口径一致):
  实时: https://qt.gtimg.cn/q=sh000001,sz399106,bj899050,sz399006,sh000688
  日K:  https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get (逐市场聚合)
  5日分时: https://web.ifzq.gtimg.cn/appstock/app/day/query (沪/深含累计成交额, 北证按量占比折算)
  1分钟: https://ifzq.gtimg.cn/appstock/app/kline/mkline (逐市场聚合, 北证无分钟线故沪深口径)

核心口径(2026-08-21 修正): "较昨日"对比一律用 昨日同一时刻真实累计成交额
  (5日分时直接读取), 取代原"昨日全天×线性时间进度"估算——早盘量能远超线性,
  线性估算导致 10:16 时虚高放量 +4000 亿(同花顺实为缩量 -970 亿)。

独立运行: python3 volume_core.py   -> 输出一行可读摘要
"""
import json
import os
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

# 清掉可能的沙箱/调试代理环境变量: 本工具直连腾讯行情, 走代理会 Connection refused
for _k in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy',
           'ALL_PROXY', 'all_proxy'):
    os.environ.pop(_k, None)

INDEX_CODE = 'sh000001'      # 主指数(兼容旧字段: price/change_pct 仍指沪指)
INDEX_NAME = '上证指数'
# 标题展示的三大指数(2026-09-11 新增创业板指/科创50, 仅用缩写 沪/创/科):
#   沪=上证指数 创=创业板指 科=科创50
# 注意: INDEX_CODES 只用于"点位+涨跌幅"展示, 绝不可并入 MARKET_CODES
# —— 创业板指/科创50 与沪/深成交额重叠, 并入会导致全市场总量重复计算
INDEX_CODES = ['sh000001', 'sz399006', 'sh000688']
INDEX_ABBR = {'sh000001': '沪', 'sz399006': '创', 'sh000688': '科'}
INDEX_NAMES = {'sh000001': '上证指数', 'sz399006': '创业板指', 'sh000688': '科创50'}
# 全市场成交额 = 上证指数(沪) + 深证综指(深) + 北证50(北)
MARKET_CODES = ['sh000001', 'sz399106', 'bj899050']
UA = {'User-Agent': 'Mozilla/5.0'}

# ---- A股交易时段(分钟) ----
AM_OPEN, AM_CLOSE = 9 * 60 + 30, 11 * 60 + 30   # 9:30-11:30
PM_OPEN, PM_CLOSE = 13 * 60, 15 * 60             # 13:00-15:00
TOTAL_MIN = (AM_CLOSE - AM_OPEN) + (PM_CLOSE - PM_OPEN)   # 240
AM_WEIGHT, PM_WEIGHT = 0.55, 0.45                # 盘中量能分布: 上午约55%/下午约45%

# ---- 量能判定阈值 (vs 昨日同时刻; ±3% 内为平量噪声区, 对标同花顺观感) ----
RATIO_UP = 1.03    # >= 1.03 放量
RATIO_DN = 0.97    # <= 0.97 缩量
MIN_PROGRESS = 0.03  # 进度过小(刚开盘)不判状态, 避免噪声
MINUTE_LIMIT = 800   # 1分钟线拉取条数(约4个交易日, 保证覆盖完整昨日)

STATE_UP, STATE_DN, STATE_FLAT, STATE_NA = '放量', '缩量', '平量', '--'

# ---- 涨跌停家数(2026-10-19 新增, 东财涨停池/跌停池) ----
# 与量能主口径(腾讯)互不依赖: 这里是另一家的数据源, 失败只让该段消失, 不影响量能
#   tc = 该日涨(跌)停池总家数; date 参数决定数据日期(非交易日返回空池 -> 视为无数据)
LIMIT_POOL_URL = {
    'zt': ('https://push2ex.eastmoney.com/getTopicZTPool'
           '?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt'
           '&Pageindex=0&pagesize=1&sort=fbt:asc&date=%s&_=%d'),
    'dt': ('https://push2ex.eastmoney.com/getTopicDTPool'
           '?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt'
           '&Pageindex=0&pagesize=1&sort=fund:asc&date=%s&_=%d'),
}
LIMIT_TIMEOUT = 4        # 涨跌停是"附加信息", 超时要比行情更短, 别拖慢状态栏
LIMIT_CACHE_SEC = 25     # 缓存时长: 刷新间隔10秒, 每 25 秒才真正打一次东财
_LIMIT_CACHE = {'key': None, 'ts': 0.0, 'data': None}

# 跌停告警: 跌停家数 > 该阈值时, 标题里的跌停数字每分钟闪烁一次(见 need_flash)
FLASH_DT_THRESHOLD = 10
FLASH_SEC = 0.5                  # 单次闪烁持续时长(秒), 太长像"变色"太短看不见
FLASH_PHASES = ('trading', 'lunch')   # 只在盘中/午休闪, 收盘后闪没意义


def judge(ratio):
    """量比 -> 状态"""
    return (STATE_UP if ratio >= RATIO_UP else
            STATE_DN if ratio <= RATIO_DN else STATE_FLAT)


HTTP_RETRY = 2           # HTTP 请求失败重试次数(致命请求, 如实时行情)
HTTP_RETRY_DELAY = 2    # 重试间隔(秒), 第n次等待 n*DELAY
HTTP_TIMEOUT = 5         # 单请求超时(秒): 状态栏工具宁快勿卡, 失败降频重试

# ---- 昨日同时刻真实成交额缓存(腾讯5日分时, 盘后不变, 按日期缓存) ----
_YEST_INTRA_CACHE = {'yest_date': None, 'data': None}   # data: {hhmm: {'amt_wan': 全市场累计万元}}
_BJ_AMT_CACHE = {'yest_date': None, 'amt_wan': None}    # 北证昨日全天成交额(万元, 日K)


def _http(url, decode='utf-8', retries=HTTP_RETRY, timeout=HTTP_TIMEOUT):
    """带重试的 HTTP GET: 失败重试 retries 次, 间隔递增
    致命请求(实时行情) retries=2 多一次机会; 非致命请求 retries=1 快速失败,
    避免接口抽风时状态栏 UI 长时间冻结"""
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            return urllib.request.urlopen(req, timeout=timeout).read().decode(decode, 'ignore')
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(HTTP_RETRY_DELAY * (attempt + 1))
    raise last_err


def fetch_realtime():
    """腾讯实时行情(沪深京 + 三大指数, 一次请求返回) -> dict
    price/change_pct 为主指数(上证)点位(兼容旧字段);
    indices 为 沪/创/科 三大指数点位与涨跌幅(顺序同 INDEX_CODES);
    vol_hand/amount_wan 为 MARKET_CODES(沪+深+北)聚合, 不含创业板指/科创50(口径不变, 避免重复计入)"""
    codes = list(MARKET_CODES)
    for c in INDEX_CODES:
        if c not in codes:
            codes.append(c)
    raw = _http('https://qt.gtimg.cn/q=%s' % ','.join(codes), 'gbk')
    rows = {}
    for line in raw.strip().split(';'):
        line = line.strip()
        if '="' not in line:
            continue
        var, body = line.split('="', 1)
        rows[var.rsplit('_', 1)[-1]] = body.rsplit('"', 1)[0].split('~')
    if INDEX_CODE not in rows:
        raise RuntimeError('腾讯实时行情返回异常(无 %s): %s' % (INDEX_CODE, raw[:120]))
    m = rows[INDEX_CODE]
    vol = sum(float(rows[c][6]) for c in MARKET_CODES if c in rows)
    amt = sum(float(rows[c][37]) for c in MARKET_CODES if c in rows)
    # 三大指数(沪/创/科): 任一缺失只跳过该项, 不影响量能主逻辑
    indices = []
    for c in INDEX_CODES:
        r = rows.get(c)
        if not r or len(r) <= 32:
            continue
        try:
            indices.append({
                'code': c,
                'name': INDEX_NAMES.get(c, c),
                'abbr': INDEX_ABBR.get(c, c),
                'price': float(r[3]),
                'change_pct': float(r[32]),
            })
        except (ValueError, IndexError):
            continue
    return {
        'price': float(m[3]),
        'prev_close': float(m[4]),
        'open': float(m[5]),
        'vol_hand': vol,                        # 全市场成交量(手)
        'time': m[30],                          # yyyyMMddHHmmss
        'change_pct': float(m[32]),
        'high': float(m[33]),
        'low': float(m[34]),
        'amount_wan': amt,                      # 全市场成交额(万元)
        'breakdown': {c: float(rows[c][37]) for c in MARKET_CODES if c in rows},
        'indices': indices,                     # 沪/创/科 三大指数(点位展示用)
    }


def _pool_count(kind, date_str):
    """东财涨停池/跌停池 -> 该日总家数(失败/无数据返回 None)

    Pageindex=0&pagesize=1 只为拿 tc 总数, 不拉明细(响应 < 1KB)
    """
    url = LIMIT_POOL_URL[kind] % (date_str, int(time.time() * 1000))
    try:
        node = json.loads(_http(url, retries=1, timeout=LIMIT_TIMEOUT)).get('data') or {}
        tc = node.get('tc')
        return int(tc) if tc is not None else None
    except Exception:
        return None


def fetch_limit_counts(date_str=None, cache_sec=LIMIT_CACHE_SEC):
    """涨跌停家数(东财涨停池/跌停池) -> {'limit_date','limit_up','limit_down'}

    date_str: 数据日期 'yyyy-mm-dd' 或 'yyyyMMdd'; 默认今天。
      必须是**交易日**(非交易日东财返回空池 tc=0 → 视为无数据, 返回 None),
      所以调用方应传"最新交易日"(日K末根), 而不是硬写今天。
    两池并发请求 + 短超时 + 缓存: 附加信息不该拖慢主刷新。
    """
    ds = (date_str or datetime.now().strftime('%Y%m%d')).replace('-', '')
    now = time.time()
    c = _LIMIT_CACHE
    if c['key'] == ds and now - c['ts'] < cache_sec:
        return dict(c['data'])
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_zt = ex.submit(_pool_count, 'zt', ds)
        f_dt = ex.submit(_pool_count, 'dt', ds)
        zt, dt = f_zt.result(), f_dt.result()
    data = {'limit_date': ds, 'limit_up': zt, 'limit_down': dt}
    if zt is not None or dt is not None:      # 全失败不缓存, 下次刷新立刻重试
        c.update(key=ds, ts=now, data=data)
    return dict(data)


def fetch_daily(n=5):
    """腾讯日K(逐市场聚合) -> [(date, vol_hand, amount_wan)] 最近n个交易日(升序)"""
    agg = {}
    for code in MARKET_CODES:
        try:
            url = ('https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get'
                   '?param=%s,day,,,%d,qfq' % (code, n))
            node = json.loads(_http(url, retries=1))['data'][code]
            kl = node.get('day') or node.get('qfqday')
            for r in kl:
                v, a = agg.get(r[0], (0.0, 0.0))
                agg[r[0]] = (v + float(r[5]), a + float(r[8]))
        except Exception:
            continue
    return [(d, v, a) for d, (v, a) in sorted(agg.items())]


def fetch_minute_kline(limit=MINUTE_LIMIT):
    """腾讯1分钟K线(逐市场聚合): 同分钟两市成交量相加, 返回升序bar列表
    北证无分钟线则自动跳过 -> 该函数产物为"沪深口径"累计量"""
    merged = {}
    for code in MARKET_CODES:
        try:
            url = ('https://ifzq.gtimg.cn/appstock/app/kline/mkline'
                   '?param=%s,m1,,%d' % (code, limit))
            for r in json.loads(_http(url, retries=1))['data'][code]['m1']:
                ts = r[0]
                if ts in merged:
                    merged[ts][5] = str(float(merged[ts][5]) + float(r[5]))
                else:
                    merged[ts] = list(r)
        except Exception:
            continue
    return [merged[k] for k in sorted(merged)]


def split_yesterday(bars):
    """从分钟线中切出"昨日"(最新交易日的前一个交易日)的全部bar
    返回 (yest_date_str, yest_bars); 数据不足2个交易日时 (None, [])"""
    days = sorted({r[0][:8] for r in bars})
    if len(days) < 2:
        return None, []
    yest = days[-2]
    return yest, [r for r in bars if r[0][:8] == yest]


def _bj_yest_amount_wan(yest_date):
    """北证昨日全天成交额(万元, 日K), 按日期缓存"""
    if _BJ_AMT_CACHE['yest_date'] == yest_date:
        return _BJ_AMT_CACHE['amt_wan']
    amt = None
    try:
        url = ('https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get'
               '?param=bj899050,day,,,5,qfq')
        node = json.loads(_http(url, retries=1))['data']['bj899050']
        kl = node.get('day') or node.get('qfqday')
        for r in kl:
            if r[0] == yest_date:
                amt = float(r[8])
                break
    except Exception:
        pass
    _BJ_AMT_CACHE['yest_date'] = yest_date
    _BJ_AMT_CACHE['amt_wan'] = amt
    return amt


def yest_cum_amount_wan(yest_date, hhmm):
    """昨日 hhmm 时刻的全市场累计成交额(万元, 沪深京)

    数据源: 腾讯5日分时 web.ifzq.gtimg.cn/appstock/app/day/query
      沪/深行格式 'HHMM 价 累计量(手) 累计额(元)'  -> 直接读累计额
      北证行无成交额字段 -> 用「量占比 × 北证昨日全天额(日K)」折算(占比<0.5%)
    按 yest_date 缓存, 盘中零重复请求; 失败返回 None(调用方回退线性估算)
    """
    if _YEST_INTRA_CACHE['yest_date'] != yest_date:
        agg = {}   # hhmm -> {'vols': {code: vol}, 'amt_wan': 沪深累计额(万元)}
        for code in MARKET_CODES:
            try:
                url = 'https://web.ifzq.gtimg.cn/appstock/app/day/query?code=%s' % code
                dd = json.loads(_http(url, retries=1))['data'][code]['data']
                for it in dd:
                    if it['date'] != yest_date.replace('-', ''):
                        continue
                    for row in it['data']:
                        p = row.split()
                        if len(p) < 3:
                            continue
                        rec = agg.setdefault(p[0], {'vols': {}, 'amt_wan': 0.0})
                        rec['vols'][code] = float(p[2])
                        if len(p) >= 4:
                            rec['amt_wan'] += float(p[3]) / 1e4   # 元 -> 万元
            except Exception:
                continue
        # 北证无成交额: 量占比 × 北证昨日全天额 折算补全
        if agg:
            bj_total = _bj_yest_amount_wan(yest_date)
            bj_day_vol = agg.get('1500', {}).get('vols', {}).get('bj899050', 0)
            if bj_total and bj_day_vol > 0:
                for rec in agg.values():
                    bj_vol = rec['vols'].get('bj899050', 0)
                    rec['amt_wan'] += bj_total * bj_vol / bj_day_vol
        _YEST_INTRA_CACHE['yest_date'] = yest_date
        _YEST_INTRA_CACHE['data'] = agg
    agg = _YEST_INTRA_CACHE['data'] or {}
    # 取 <= hhmm 的最近交易行(午休/收盘自动落到 1130/1500)
    best = None
    for h in sorted(agg):
        if h <= hhmm:
            best = agg[h]['amt_wan']
        else:
            break
    return best


def trading_minutes_passed(now):
    """今日已交易分钟数(真实时钟): 9:30-11:30 + 13:00-15:00, 午休停在120, 收盘=240"""
    t = now.hour * 60 + now.minute
    if t < AM_OPEN:
        return 0
    if t <= AM_CLOSE:
        return t - AM_OPEN
    if t < PM_OPEN:
        return AM_CLOSE - AM_OPEN
    if t <= PM_CLOSE:
        return (AM_CLOSE - AM_OPEN) + (t - PM_OPEN)
    return TOTAL_MIN


def minutes_from_open(now):
    """当前时间距开盘的交易分钟数; 未开盘为负, 午休停在上限, 收盘=240"""
    t = now.hour * 60 + now.minute
    if t < AM_OPEN:
        return -(AM_OPEN - t)
    if t <= AM_CLOSE:
        return t - AM_OPEN
    if t < PM_OPEN:
        return AM_CLOSE - AM_OPEN
    if t <= PM_CLOSE:
        return (AM_CLOSE - AM_OPEN) + (t - PM_OPEN)
    return TOTAL_MIN


def time_progress(mins):
    """交易分钟数 -> 全天量能进度(0~1), 上午55%下午45%的加权外推"""
    if mins <= 0:
        return 0.0
    if mins <= AM_CLOSE - AM_OPEN:
        return mins / 120.0 * AM_WEIGHT
    if mins >= TOTAL_MIN:
        return 1.0
    return AM_WEIGHT + (mins - 120) / 120.0 * PM_WEIGHT


def fmt_amount(wan, abbr=False):
    """成交额(万元) -> 可读字符串

    abbr=False(默认, 详情面板/菜单/日志): 1662.3亿 / 1.22万亿
    abbr=True (标题, 省状态栏宽度):        1662.3亿 / 1.22WY
              "万亿" 压成两字母 WY(W=万, Y=亿), 每个标题少 4 个全角字符宽;
              不足万亿仍用 "亿"(本身只 1 个汉字, 已是最短)
    """
    yi = wan / 10000.0
    if yi >= 10000:
        return ('%.2fWY' if abbr else '%.2f万亿') % (yi / 10000.0)
    return '%.1f亿' % yi


def index_brief(idx, price_dec=1, pct_dec=2):
    """单个指数 -> (点位段, 涨跌幅段|None, 涨跌幅数值|None), 供 UI 分段着色

    点位段 = 缩写 + 点位(无空格, 如 '沪3859.8' / 简洁模式 '沪3859');
    涨跌幅段含前导空格(如 ' -1.90%'), 上涨带 '+'。
    macOS 与 Windows 两端共用, 保证标题文案完全一致。
    """
    abbr = idx.get('abbr') or idx.get('name') or ''
    price = idx.get('price')
    p_txt = '%s%s' % (abbr, ('%.' + str(price_dec) + 'f') % price
                      if price is not None else '--')
    cp = idx.get('change_pct')
    if cp is None:
        return p_txt, None, None
    sgn = '+' if cp > 0 else ''
    return p_txt, ' %s%s%%' % (sgn, ('%.' + str(pct_dec) + 'f') % cp), cp


LIMIT_SEG_UP, LIMIT_SEG_DN = 'up', 'dn'     # 涨跌停着色语义: 涨停红 / 跌停绿


def limit_segments(s, sep='/', lead_space=True):
    """涨跌停家数 -> [(文本段, 'up'|'dn'|None)], macOS/Windows 两端共用

    2026-10-19 第二版: **不再带「涨停/跌停」汉字**, 只出数字, 靠颜色区分
    (前=涨停红, 后=跌停绿), 形如 ' 29/7'; 位置在"预测成交额"之后。
    任一数值缺失则只出另一项; 都缺(东财失败/非交易日)返回 [] -> 标题自动省略。
    """
    up, dn = s.get('limit_up'), s.get('limit_down')
    if up is None and dn is None:
        return []
    segs = [(' ', None)] if lead_space else []
    if up is not None:
        segs.append(('%d' % up, LIMIT_SEG_UP))
    if dn is not None:
        if up is not None:
            segs.append((sep, None))
        segs.append(('%d' % dn, LIMIT_SEG_DN))
    return segs


def need_flash(s):
    """标题里的跌停数字是否需要闪烁(跌停家数 > FLASH_DT_THRESHOLD 且处于盘中/午休)"""
    dn = s.get('limit_down')
    return (dn is not None and dn > FLASH_DT_THRESHOLD
            and s.get('phase') in FLASH_PHASES)


def fmt_limits(s):
    """涨跌停家数 -> 纯文本(菜单/详情面板/命令行), 这里保留中文便于阅读"""
    up, dn = s.get('limit_up'), s.get('limit_down')
    if up is None and dn is None:
        return '-'
    return '涨停%s / 跌停%s' % ('-' if up is None else up,
                               '-' if dn is None else dn)


def fmt_limit_date(d):
    """'20261019' -> '2026-10-19'(菜单展示用); 空值 -> '--'"""
    d = (d or '').replace('-', '')
    return '%s-%s-%s' % (d[:4], d[4:6], d[6:]) if len(d) == 8 else (d or '--')


def fmt_hand(hand):
    """成交量(手) -> 可读字符串, 百万/千万/亿手"""
    if hand is None:
        return '-'
    if hand >= 1e8:
        return '%.2f亿手' % (hand / 1e8)
    if hand >= 1e7:
        return '%.2f千万手' % (hand / 1e7)
    if hand >= 1e6:
        return '%.1f百万手' % (hand / 1e6)
    if hand >= 1e4:
        return '%.1f万手' % (hand / 1e4)
    return '%.0f手' % hand


# ---- 分钟级量能节奏(状态转换检测专用) ----
PACE_SAMPLE_SEC = 60          # 采样周期: 每分钟记录一次当前数据


class PaceTracker:
    """分钟级量能节奏: 当前分钟 vs 1 分钟前

    每分钟采样一次当前累计成交额, 相邻两次采样之差即该分钟成交增量:
      本分钟增量 > 上分钟增量 -> 放量(节奏升)
      本分钟增量 < 上分钟增量 -> 缩量(节奏降)
      完全相等 -> 沿用上一节奏(不设"平量"死区, 该状态不直观已移除)
    节奏在 放量/缩量 间翻转 -> 追加一条转换记录到 trans_log
    (2026-09-11: 标题不再显示"放量转缩量/缩量转放量"转换词,
     转换记录仅保留在详情面板/菜单的"节奏转换历史"中)

    与 analyze() 快照的 state 口径互补:
      state: 今日累计 vs 昨日同时刻(同花顺大盘口径, "今天比昨天量大吗")
      pace : 本分钟 vs 上一分钟(盘中瞬时节奏, "此刻量能在加速还是衰减")
    """

    def __init__(self, sample_sec=PACE_SAMPLE_SEC, log_max=30):
        self.sample_sec = sample_sec
        self.log_max = log_max
        self.pace = None          # 当前节奏: 放量/缩量/None(未启动)
        self.trans_log = []       # [{'ts','time','from','to','inc_wan','prev_inc_wan'}]
        self.last_inc = None      # 本分钟成交增量(万元, 展示用)
        self.prev_inc = None      # 上一分钟成交增量(万元, 展示用)
        self._last_ts = None      # 上次采样时间戳
        self._last_cum = None     # 上次采样时累计成交额(万元)

    def update(self, snap, now=None):
        """每次刷新调用; 内部按 sample_sec 节流, 仅交易时段采样"""
        cum = snap.get('cur_amount_wan')
        if cum is None or snap.get('phase') != 'trading':
            return
        now = now or time.time()
        if self._last_ts is not None:
            gap = now - self._last_ts
            if gap < self.sample_sec:
                return                          # 未满一个采样周期
            inc = cum - self._last_cum
            if inc < 0 or gap > self.sample_sec * 2:
                # 累计倒退(隔日)或超长间隔(午休/挂起): 重置基线, 本轮不计增量
                self._reset(cum, now)
                return
            if self.last_inc is not None:
                if inc > self.last_inc:
                    pace = STATE_UP
                elif inc < self.last_inc:
                    pace = STATE_DN
                else:
                    pace = self.pace            # 增量完全相等: 沿用上一节奏
                if pace is not None:
                    if self.pace is not None and self.pace != pace:
                        self.trans_log.append({
                            'ts': now,
                            'time': datetime.now().strftime('%H:%M:%S'),
                            'from': self.pace, 'to': pace,
                            'inc_wan': round(inc, 0),
                            'prev_inc_wan': round(self.last_inc, 0),
                        })
                        self.trans_log = self.trans_log[-self.log_max:]
                    self.pace = pace
                self.prev_inc = self.last_inc
            self.last_inc = inc
        self._last_cum = cum
        self._last_ts = now

    def _reset(self, cum, now):
        self._last_cum = cum
        self._last_ts = now
        self.last_inc = None
        self.prev_inc = None


# ============================================================
# 监视股票轮播(2026-11-19 新增)
#   设置页 settings_win.py 写 .settings.json; 两端 App 轮询 mtime 热加载。
#   标题在「指数视图」与「个股视图」之间按 cycle_sec 秒循环播放。
# ============================================================
SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             '.settings.json')
CYCLE_SEC_DEFAULT = 10         # 指数 <-> 个股 默认切换间隔(秒)
CYCLE_SEC_MIN, CYCLE_SEC_MAX = 1, 120
DEFAULT_SETTINGS = {'cycle_sec': CYCLE_SEC_DEFAULT, 'show_volume': False,
                    'stocks': []}

# 标题分段语义色(两端各自映射成自己的颜色对象)
SEG_FLAT, SEG_UP, SEG_DN, SEG_DIM = 'flat', 'up', 'dn', 'dim'

STOCK_DAY_TIMEOUT = 5          # 个股日K/分时超时(秒)
STOCK_DAY_RETRY_SEC = 60       # 取数不全时的重试间隔(秒), 避免每10秒反复打接口
_STOCK_DAY_CACHE = {}          # code -> {'today','ts','complete',...}


def normalize_code(raw):
    """股票代码规整 -> 'sh600000' / 'sz000001' / 'bj430047'

    纯数字时按前缀补市场(与项目口径一致):
      920/43/83/87 -> bj(北证), 5/6/9 -> sh, 其余(0/1/2/3) -> sz
    已是 sh/sz/bj 前缀的原样返回(统一小写)
    """
    s = str(raw or '').strip().lower().replace('.', '')
    if not s:
        return ''
    for pre in ('sh', 'sz', 'bj'):
        if s.startswith(pre):
            digits = s[len(pre):]
            return pre + digits if digits.isdigit() else ''
    if not s.isdigit():
        return ''
    if s.startswith('920') or s.startswith(('43', '83', '87')):
        return 'bj' + s
    if s[0] in '569':
        return 'sh' + s
    return 'sz' + s


def load_settings():
    """读 .settings.json -> dict(缺失/损坏一律回默认, 绝不让主程序起不来)"""
    s = {'cycle_sec': CYCLE_SEC_DEFAULT, 'show_volume': False, 'stocks': []}
    try:
        with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
            raw = json.load(f)
    except Exception:
        return s
    if not isinstance(raw, dict):
        return s
    try:
        s['cycle_sec'] = min(CYCLE_SEC_MAX, max(CYCLE_SEC_MIN,
                                               int(raw.get('cycle_sec',
                                                           CYCLE_SEC_DEFAULT))))
    except (TypeError, ValueError):
        s['cycle_sec'] = CYCLE_SEC_DEFAULT
    s['show_volume'] = bool(raw.get('show_volume', False))
    seen = set()
    for it in raw.get('stocks') or []:
        if not isinstance(it, dict):
            continue
        code = normalize_code(it.get('code'))
        if not code or code in seen:
            continue
        seen.add(code)
        s['stocks'].append({'code': code, 'name': str(it.get('name') or code)})
    return s


def save_settings(s):
    """写 .settings.json(先写临时文件再替换, 避免主程序读到半截 JSON)"""
    cs = s.get('cycle_sec')
    data = {'cycle_sec': CYCLE_SEC_DEFAULT if cs is None else int(cs),
            'show_volume': bool(s.get('show_volume')),
            'stocks': [{'code': normalize_code(x.get('code')),
                        'name': str(x.get('name') or x.get('code'))}
                       for x in (s.get('stocks') or [])
                       if normalize_code(x.get('code'))]}
    tmp = SETTINGS_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, SETTINGS_FILE)
    return data


def settings_mtime():
    """设置文件 mtime(不存在为 0), 供两端热加载判断"""
    try:
        return os.path.getmtime(SETTINGS_FILE)
    except OSError:
        return 0.0


def fetch_stock_quotes(codes):
    """腾讯实时行情(多只一次请求) -> {code: {name,price,prev_close,change_pct,...}}

    字段口径同指数: p[1]名 p[3]现价 p[4]昨收 p[6]量(手) p[30]时间
    p[32]涨跌% p[37]成交额(万元) —— 个股与指数在腾讯是同一套字段
    """
    codes = [c for c in codes if c]
    if not codes:
        return {}
    raw = _http('https://qt.gtimg.cn/q=%s' % ','.join(codes), 'gbk')
    out = {}
    for line in raw.strip().split(';'):
        line = line.strip()
        if '="' not in line:
            continue
        var, body = line.split('="', 1)
        code = var.rsplit('_', 1)[-1].lower()
        p = body.rsplit('"', 1)[0].split('~')
        if len(p) < 38:
            continue
        try:
            out[code] = {
                'code': code,
                'name': p[1] or code,
                'price': float(p[3]),
                'prev_close': float(p[4]),
                'vol_hand': float(p[6]),
                'time': p[30],
                'change_pct': float(p[32]),
                'amount_wan': float(p[37]),
            }
        except (ValueError, IndexError):
            continue
    return out


def resolve_stock(keyword):
    """代码或名称 -> (code, name); 找不到返回 (None, None)

    纯代码走实时行情取名(最可靠); 中文/拼音走腾讯 smartbox 联想, 只取沪深北A股。
    """
    kw = str(keyword or '').strip()
    if not kw:
        return None, None
    code = normalize_code(kw)
    if code:
        q = fetch_stock_quotes([code]).get(code)
        return (code, q['name']) if q else (code, None)
    try:
        url = ('https://smartbox.gtimg.cn/s3/?q=%s&t=all'
               % urllib.parse.quote(kw))
        raw = _http(url, 'gbk', retries=1)
        body = raw.split('="', 1)[-1].rsplit('"', 1)[0]
        for item in body.split('^'):
            f = item.split('~')
            if len(f) < 3 or f[0] not in ('sh', 'sz', 'bj'):
                continue
            name = f[2].encode('latin-1', 'ignore').decode('unicode_escape')
            return normalize_code(f[0] + f[1]), name
    except Exception:
        pass
    return None, None


def _stock_prev_day(code, today_str):
    """个股「上一交易日」(日K) -> {'date','amount_wan','vol_hand'} 或 None

    日K末根盘中就是今天(半截数据), 所以取**最后一个 date < today** 的行:
    开盘前没有今天这根, 末根即昨日; 盘中/收盘后自然跳过今天。
    """
    try:
        url = ('https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get'
               '?param=%s,day,,,5,qfq' % code)
        node = json.loads(_http(url, retries=1, timeout=STOCK_DAY_TIMEOUT))['data'][code]
        kl = node.get('day') or node.get('qfqday') or []
        best = None
        for r in kl:
            if r[0] < today_str:
                best = r
        if not best:
            return None
        return {'date': best[0], 'vol_hand': float(best[5]),
                'amount_wan': float(best[8])}       # r[8] = 成交额(万元)
    except Exception:
        return None


def _stock_yest_intraday(code, today_str):
    """个股昨日分时(腾讯5日分时) -> {'date','rows':[(hhmm,cum_amt_wan,cum_vol)],'total_wan','total_vol'}

    行格式 'HHMM 价 累计量(手) 累计额(元)'; 同一 hhmm 出现多次时取累计值最大者
    (累计值单调不减, 重复行是分段/竞价行)。全无数据返回 None。
    """
    try:
        url = 'https://web.ifzq.gtimg.cn/appstock/app/day/query?code=%s' % code
        dd = json.loads(_http(url, retries=1, timeout=STOCK_DAY_TIMEOUT))['data'][code]['data']
        pick = None
        for it in dd:
            if it.get('date', '') < today_str.replace('-', ''):
                if pick is None or it['date'] > pick['date']:
                    pick = it
        if not pick:
            return None
        acc = {}
        for row in pick.get('data') or []:
            f = row.split()
            if len(f) < 4:
                continue
            hhmm = f[0]
            amt_wan = float(f[3]) / 1e4
            vol = float(f[2])
            old = acc.get(hhmm)
            if old is None or amt_wan > old[0]:
                acc[hhmm] = (amt_wan, vol)
        if not acc:
            return None
        rows = [(h, acc[h][0], acc[h][1]) for h in sorted(acc)]
        return {'date': pick['date'], 'rows': rows,
                'total_wan': rows[-1][1], 'total_vol': rows[-1][2]}
    except Exception:
        return None


def stock_day_info(code, today_str, now_ts=None):
    """个股「昨日基准」(全天额/量 + 分时累计曲线), 按 (code, 自然日) 缓存

    取数不全时 60 秒后才重试, 避免接口抽风时每 10 秒反复打两条请求拖慢刷新。
    """
    now_ts = now_ts or time.time()
    c = _STOCK_DAY_CACHE.get(code)
    if c and c['today'] == today_str and (c.get('complete')
                                         or now_ts - c['ts'] < STOCK_DAY_RETRY_SEC):
        return c
    prev = _stock_prev_day(code, today_str)
    intra = _stock_yest_intraday(code, today_str)
    info = {
        'today': today_str,
        'ts': now_ts,
        'complete': bool(prev and intra),
        'prev_date': (prev or {}).get('date') or (intra or {}).get('date'),
        'prev_amount_wan': (prev or {}).get('amount_wan')
                            or (intra or {}).get('total_wan'),
        'prev_vol_hand': (prev or {}).get('vol_hand')
                         or (intra or {}).get('total_vol'),
        'rows': (intra or {}).get('rows') or [],
    }
    _STOCK_DAY_CACHE[code] = info
    return info


def _cum_at(rows, hhmm):
    """累计曲线中 <= hhmm 的最近一行 -> (cum_amt_wan, cum_vol) ; 无 -> (None, None)"""
    best = (None, None)
    for h, a, v in rows:
        if h <= hhmm:
            best = (a, v)
        else:
            break
    return best


def fmt_delta_wan(wan):
    """成交额增量(万元) -> '+320万' / '-1.25亿'(带符号, 缩到状态栏宽度)"""
    if wan is None:
        return '-'
    sgn = '+' if wan >= 0 else '-'
    v = abs(wan)
    if v >= 10000:
        return '%s%.2f亿' % (sgn, v / 10000.0)
    return '%s%.0f万' % (sgn, v)


def watch_snapshot(codes, snap=None, now=None):
    """监视股票快照 -> [{code,name,price,change_pct,amount_wan,prev_amount_wan,
                        forecast_wan,vol_hand,prev_vol_hand,forecast_vol_hand}]

    snap: analyze() 快照, 用它的 phase/progress 保证与大盘口径同一时刻;
          不传则按当前时钟粗判(设置页/单测用)。
    delta_wan(较上次刷新的增减)由 StockTracker 填, 这里置 None。
    """
    now = now or datetime.now()
    today_str = now.strftime('%Y-%m-%d')
    hhmm = now.strftime('%H%M')
    phase = (snap or {}).get('phase', '')
    if not phase:
        if now.weekday() >= 5:
            phase = 'weekend'
        else:
            t = now.hour * 60 + now.minute
            phase = ('pre_open' if t < AM_OPEN else
                     'trading' if (t < AM_CLOSE or PM_OPEN <= t < PM_CLOSE) else
                     'lunch' if t < PM_OPEN else 'closed')
    progress = (snap or {}).get('progress')
    if progress is None:
        progress = time_progress(minutes_from_open(now))

    quotes = fetch_stock_quotes(codes)
    out = []
    for code in codes:
        q = quotes.get(code)
        row = {'code': code, 'name': (q or {}).get('name') or code,
               'price': (q or {}).get('price'),
               'change_pct': (q or {}).get('change_pct'),
               'amount_wan': (q or {}).get('amount_wan'),
               'vol_hand': (q or {}).get('vol_hand'),
               'prev_amount_wan': None, 'prev_vol_hand': None,
               'prev_cum_amount_wan': None, 'amount_diff_wan': None,
               'forecast_wan': None, 'forecast_vol_hand': None,
               'delta_wan': None, 'phase': phase}
        if not q:
            out.append(row)
            continue
        info = stock_day_info(code, today_str)
        row['prev_amount_wan'] = info['prev_amount_wan']
        row['prev_vol_hand'] = info['prev_vol_hand']
        amt, vol = q['amount_wan'], q['vol_hand']
        prev_amt, prev_vol = info['prev_amount_wan'], info['prev_vol_hand']
        rows = info['rows']
        if phase == 'closed':
            row['forecast_wan'], row['forecast_vol_hand'] = amt, vol
            if prev_amt:
                row['amount_diff_wan'] = amt - prev_amt       # 收盘=实际全天差
        elif phase in ('trading', 'lunch'):
            cum_amt, cum_vol = _cum_at(rows, hhmm) if rows else (None, None)
            if cum_amt:
                row['prev_cum_amount_wan'] = cum_amt
                row['amount_diff_wan'] = amt - cum_amt         # 较昨日同时刻差额
            if cum_amt and prev_amt:                 # 昨日同时刻占比外推
                row['forecast_wan'] = amt / (cum_amt / prev_amt)
                if cum_vol and prev_vol:
                    row['forecast_vol_hand'] = vol / (cum_vol / prev_vol)
            elif progress >= MIN_PROGRESS:           # 兜底: 线性时间进度
                row['forecast_wan'] = amt / progress
        out.append(row)
    return out


class StockTracker:
    """监视股票「今日成交额较上次刷新」的增减(仅内存态, 与 PaceTracker 同理)

    两端 UI 共用: 每轮刷新 update(rows) 后 rows 里就带上 delta_wan; 首次为 None。
    """

    def __init__(self):
        self.prev = {}       # code -> 上次成交额(万元)
        self.first_ts = None

    def update(self, rows):
        for r in rows:
            amt = r.get('amount_wan')
            if amt is None:
                r['delta_wan'] = None
                continue
            p = self.prev.get(r['code'])
            r['delta_wan'] = None if p is None else round(amt - p, 0)
            self.prev[r['code']] = amt
        return rows

    def reset(self):
        self.prev.clear()


def stock_segments(st, show_volume=False, compact=False, with_today=True):
    """个股标题分段 -> [(文本, SEG_*)]; macOS/Windows 两端共用

    字段顺序: 名称 现价 涨跌幅 昨成交额 今成交额 较上次增减 [预成交额] [成交量]
    compact=True 时名称截到 3 字、金额保留 1 位小数(状态栏宽, 防止被挤出屏幕)
    with_today=False(未开盘/休市): 只留 昨成交额 —— 此时腾讯返回的"今日成交额"
    其实是上一个交易日的全天值, 照实显示会让人误以为今天已经成交了这么多。
    """
    name = st.get('name') or st.get('code') or '--'
    if compact and len(name) > 3:
        name = name[:3]
    segs = [(name, SEG_FLAT)]
    px, cp = st.get('price'), st.get('change_pct')
    if px is None:
        segs.append((' --', SEG_DIM))
        return segs
    segs.append((' %.2f' % px, SEG_FLAT))
    if cp is not None:
        segs.append((' %s%.2f%%' % ('+' if cp > 0 else '', cp),
                     SEG_UP if cp > 0 else SEG_DN if cp < 0 else SEG_FLAT))
    if st.get('prev_amount_wan'):
        segs.append((' 昨%s' % fmt_amount(st['prev_amount_wan'], abbr=True), SEG_DIM))
    if with_today and st.get('amount_wan') is not None:
        segs.append((' 今%s' % fmt_amount(st['amount_wan'], abbr=True), SEG_FLAT))
        d = st.get('delta_wan')
        if d:
            segs.append((' %s' % fmt_delta_wan(d), SEG_UP if d > 0 else SEG_DN))
        if st.get('forecast_wan'):
            segs.append((' 预%s' % fmt_amount(st['forecast_wan'], abbr=True), SEG_DIM))
        if show_volume and st.get('vol_hand'):
            segs.append((' 量%s' % fmt_hand(st['vol_hand']), SEG_DIM))
    return segs


def analyze(now=None):
    """主计算入口(无状态, 每次调用返回当前快照) -> dict

    字段:
      phase          pre_open/trading/lunch/closed/weekend
      indices        沪/创/科 三大指数 [{code,name,abbr,price,change_pct}]
      state          放量/缩量/平量/-- (金额口径: 今日总成交额 vs 昨日同时进度)
      vol_ratio      金额口径量比: 今日总额 / (昨日全天 × 时间进度), 收盘=实际全天比
      amount_diff_wan 较上一日同时刻成交额差额(万元), 收盘=实际差
      vol_ratio_min  成交量(手)口径量比(沪深1分钟线对齐), 菜单参考
      forecast_wan   预测全天成交额(万元), 收盘后=实际值, 非交易日 None
      forecast_vol_hand  预测全天成交量(手, 昨日分布法)
      limit_up/limit_down  涨跌停家数(东财涨停池/跌停池, 失败为 None 不显示)
    """
    now = now or datetime.now()
    rt = fetch_realtime()
    daily = fetch_daily(5)
    last_date = daily[-1][0]
    today_str = now.strftime('%Y-%m-%d')

    prev = daily[-2] if len(daily) >= 2 else daily[-1]   # 昨日(前一根)
    prev_amount = prev[2]                                # 昨日全天成交额(万元)

    cur_amount = rt['amount_wan']
    today_cum = rt['vol_hand']                           # 今日累计成交量(手, 实时即累计)
    mins = minutes_from_open(now)
    is_trading_day = (last_date == today_str)
    is_weekend = now.weekday() >= 5

    # ---- 时段判定(用真实时钟, 避免午休边界错位) ----
    if not is_trading_day:
        phase = 'weekend' if is_weekend else 'pre_open'
    else:
        t = now.hour * 60 + now.minute
        if t < AM_OPEN:
            phase = 'pre_open'
        elif t < AM_CLOSE:       # 9:30-11:30
            phase = 'trading'
        elif t < PM_OPEN:        # 11:30-13:00 午休
            phase = 'lunch'
        elif t < PM_CLOSE:       # 13:00-15:00
            phase = 'trading'
        else:
            phase = 'closed'

    # ---- 昨日同时刻累计量(1分钟线对齐, 沪深口径) ----
    yest_cum, yest_total, vol_ratio_min, vol_diff = None, None, None, None
    minute_ok = False
    try:
        yest, ybars = split_yesterday(fetch_minute_kline())
        if ybars:
            yest_total = sum(float(r[5]) for r in ybars)
            n = trading_minutes_passed(now)
            if n > 0:
                yest_cum = sum(float(r[5]) for r in ybars[:n])
                if yest_cum > 0:
                    vol_ratio_min = today_cum / yest_cum
                    vol_diff = today_cum - yest_cum
                    minute_ok = True
    except Exception:
        pass  # 分钟线失败不致命, 状态回退金额口径

    # ---- 昨日同时刻真实累计成交额(腾讯5日分时, 全市场) ----
    # 取代原"昨日全天×线性时间进度"估算(早盘量能远超线性, 会严重虚高放量)
    yest_cum_amt = None
    yest_date = None
    if is_trading_day and len(daily) >= 2:
        yest_date = prev[0]                       # 'yyyy-mm-dd'
        try:
            yest_cum_amt = yest_cum_amount_wan(yest_date, now.strftime('%H%M'))
        except Exception:
            yest_cum_amt = None                   # 失败回退线性估算

    # ---- 量比 + 状态 (金额口径, 对标同花顺"当前 vs 昨日同时刻") ----
    progress = time_progress(mins)
    vol_ratio, state = None, STATE_NA
    if phase in ('trading', 'lunch'):
        if yest_cum_amt and yest_cum_amt > 0:
            vol_ratio = cur_amount / yest_cum_amt
            state = judge(vol_ratio)
        elif progress >= MIN_PROGRESS and prev_amount > 0:   # 兜底: 分时不可用
            vol_ratio = cur_amount / (prev_amount * progress)
            state = judge(vol_ratio)
    elif phase == 'closed':
        if prev_amount > 0:
            vol_ratio = cur_amount / prev_amount
            state = judge(vol_ratio)

    # 较上一日同时刻成交额差额(万元, 同花顺口径); 收盘=实际全天差
    amount_diff = None
    if phase in ('trading', 'lunch') and yest_cum_amt is not None:
        amount_diff = cur_amount - yest_cum_amt
    elif phase == 'closed' and prev_amount > 0:
        amount_diff = cur_amount - prev_amount
    elif progress >= MIN_PROGRESS and prev_amount > 0:        # 兜底: 线性估算
        amount_diff = cur_amount - prev_amount * progress

    # ---- 预测全天成交额 ----
    # 用昨日真实量能分布: 预测 = 今日累计 / (昨日同时刻占全天比例)
    # (线性时间进度模型在早盘严重失真, 已废弃)
    forecast = None
    if phase == 'closed' and is_trading_day:
        forecast = cur_amount                       # 收盘后=实际值
    elif phase in ('trading', 'lunch') and yest_cum_amt and prev_amount > 0:
        forecast = cur_amount / (yest_cum_amt / prev_amount)
    elif is_trading_day and progress >= MIN_PROGRESS:
        forecast = cur_amount / progress            # 兜底: 线性外推
    forecast_vol = None   # 昨日分布法: 今日累计 / (昨日同时占全天比例)
    if minute_ok and yest_total:
        forecast_vol = today_cum / (yest_cum / yest_total)
    if phase == 'closed':
        forecast_vol = today_cum   # 收盘后即为实际值

    # ---- 涨跌停家数(东财, 附加信息: 失败/非交易日为 None, 不显示) ----
    # 日期用"最新交易日"(日K末根): 盘中=今天, 开盘前/休市=上一交易日
    try:
        lc = fetch_limit_counts(last_date)
    except Exception:
        lc = {}

    return {
        'name': INDEX_NAME,
        'code': INDEX_CODE,
        'price': rt['price'],
        'change_pct': rt['change_pct'],
        'indices': rt.get('indices') or [],     # 沪/创/科 三大指数(标题点位展示)
        'phase': phase,
        'state': state,
        'limit_up': lc.get('limit_up'),          # 涨停家数(东财涨停池 tc)
        'limit_down': lc.get('limit_down'),      # 跌停家数(东财跌停池 tc)
        'limit_date': lc.get('limit_date'),      # 涨跌停数据日期(yyyyMMdd)
        'vol_ratio': round(vol_ratio, 2) if vol_ratio else None,
        'amount_diff_wan': round(amount_diff, 0) if amount_diff is not None else None,
        'breakdown_wan': {c: round(v, 0) for c, v in rt['breakdown'].items()},
        'vol_ratio_min': round(vol_ratio_min, 2) if vol_ratio_min else None,
        'cur_amount_wan': round(cur_amount, 0),
        'prev_amount_wan': round(prev_amount, 0),
        'forecast_wan': round(forecast, 0) if forecast else None,
        'today_cum_hand': round(today_cum, 0),
        'yest_cum_hand': round(yest_cum, 0) if yest_cum is not None else None,
        'yest_total_hand': round(yest_total, 0) if yest_total else None,
        'vol_diff_hand': round(vol_diff, 0) if vol_diff is not None else None,
        'forecast_vol_hand': round(forecast_vol, 0) if forecast_vol else None,
        'progress': round(progress, 3),
        'latest_date': last_date,
        'now': now.strftime('%Y-%m-%d %H:%M:%S'),
        'update_time': rt['time'],
    }


def main():
    r = analyze()
    ph = {'pre_open': '未开盘', 'trading': '交易中', 'lunch': '午休',
          'closed': '已收盘', 'weekend': '休市'}[r['phase']]
    idxs = r.get('indices') or [{'abbr': '沪', 'price': r['price'],
                                 'change_pct': r['change_pct']}]
    head = ' '.join('%s%.1f %+.2f%%' % (i['abbr'], i['price'], i['change_pct'])
                    for i in idxs)
    parts = ['[%s] %s' % (ph, head)]
    if r['phase'] in ('trading', 'lunch', 'closed'):
        parts.append('全市场 %s' % fmt_amount(r['cur_amount_wan']))
        parts.append('昨 %s' % fmt_amount(r['prev_amount_wan']))
        if r['state'] != STATE_NA and r['amount_diff_wan'] is not None:
            yi = r['amount_diff_wan'] / 10000.0
            sgn = '+' if yi >= 0 else ''
            parts.append('%s%s%.0f亿' % (r['state'], sgn, yi))
        if r['forecast_wan']:
            parts.append('预测全天 %s' % fmt_amount(r['forecast_wan']))
        if r.get('limit_up') is not None or r.get('limit_down') is not None:
            parts.append(fmt_limits(r))
        if r['today_cum_hand'] and r['yest_cum_hand'] is not None:
            parts.append('量(沪深) 今%s vs 昨同时%s' % (
                fmt_hand(r['today_cum_hand']), fmt_hand(r['yest_cum_hand'])))
            if r['vol_diff_hand'] is not None:
                sgn = '+' if r['vol_diff_hand'] >= 0 else ''
                parts.append('(%s%s)' % (sgn, fmt_hand(abs(r['vol_diff_hand']))))
        if r['forecast_vol_hand']:
            parts.append('预测全天量 %s' % fmt_hand(r['forecast_vol_hand']))
    elif r['phase'] == 'pre_open':
        parts.append('昨 %s' % fmt_amount(r['prev_amount_wan']))
    print(' | '.join(parts))
    print(json.dumps(r, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main()
