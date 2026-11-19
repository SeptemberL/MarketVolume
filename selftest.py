#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线自检（不需要网络、不启动 GUI）

用法:
    ./venv/bin/python selftest.py          # 只跑断言
    ./venv/bin/python selftest.py --net    # 额外真实取一次数(需要能访问腾讯行情)

断言覆盖: 代码规整 / 金额格式化 / 个股卡片文案与语义色 / 涨跌停段 /
          配置读写与容错 / 成交额增减追踪。
全部通过时最后一行输出「全部通过 (N 项)」, 退出码 0; 有失败则退出码 1。
"""
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import volume_core as vc

FAIL = []
N = [0]


def check(cond, msg):
    N[0] += 1
    print(('  OK   ' if cond else '  FAIL ') + msg)
    if not cond:
        FAIL.append(msg)


def main():
    print('[1] 代码规整 normalize_code')
    check(vc.normalize_code('600000') == 'sh600000', "'600000' -> sh600000")
    check(vc.normalize_code('000001') == 'sz000001', "'000001' -> sz000001")
    check(vc.normalize_code('430047') == 'bj430047', "'430047' -> bj430047")
    check(vc.normalize_code('920001') == 'bj920001', "北证 920 -> bj920001")
    check(vc.normalize_code('SH600000') == 'sh600000', '大写前缀统一小写')
    check(vc.normalize_code('乱写') == '', '非法输入 -> 空串')

    print('[2] 金额/量格式化')
    check(vc.fmt_amount(119348972, abbr=True) == '1.19WY', '万亿在标题里压成 WY')
    check(vc.fmt_amount(119348972) == '1.19万亿', '菜单/日志仍是「万亿」全称')
    check(vc.fmt_amount(2520000, abbr=True) == '252.0亿', '不足万亿用「亿」')
    check(vc.fmt_hand(158375) == '15.8万手', '成交量格式化')
    check(vc.fmt_delta_wan(320) == '+320万', '增量: +320万')
    check(vc.fmt_delta_wan(-12500) == '-1.25亿', '增量: -1.25亿')

    print('[3] 个股卡片文案 stock_segments')
    stk = {'code': 'sh600000', 'name': '浦发银行', 'price': 10.67, 'change_pct': -0.47,
           'amount_wan': 16983.0, 'prev_amount_wan': 70513.6, 'forecast_wan': 74800.0,
           'vol_hand': 158375.0, 'delta_wan': 320.0}
    segs = vc.stock_segments(stk)
    txt = ''.join(t for t, _ in segs)
    print('      ' + txt)
    check(txt.startswith('浦发银行 10.67'), '名称+现价在最前')
    check('-0.47%' in txt, '含涨跌幅')
    check('昨7.1亿' in txt, '含昨日成交额')
    check('今1.7亿' in txt, '含今日成交额')
    check('+320万' in txt, '含较上次刷新增减')
    check('预7.5亿' in txt, '含预计成交额')
    check(txt.index('昨') < txt.index('今') < txt.index('+320万') < txt.index('预'),
          '字段顺序: 昨 -> 今 -> 增减 -> 预')
    check([k for t, k in segs if '-0.47%' in t] == [vc.SEG_DN], '下跌 -> 绿(SEG_DN)')
    check([k for t, k in segs if '昨' in t] == [vc.SEG_DIM], '昨/预/量 -> 次要色(SEG_DIM)')
    up = ''.join(t for t, _ in vc.stock_segments(dict(stk, change_pct=1.2, delta_wan=-200)))
    check('+1.20%' in up and '-200万' in up, '上涨/减少的文案')
    vol = ''.join(t for t, _ in vc.stock_segments(stk, show_volume=True))
    check('量15.8万手' in vol, '开关打开后含成交量')
    pre = ''.join(t for t, _ in vc.stock_segments(stk, with_today=False))
    check('今' not in pre and '预' not in pre and '昨7.1亿' in pre,
          '未开盘/休市: 只留昨日成交额')
    fail = ''.join(t for t, _ in vc.stock_segments({'code': 'sh600000', 'price': None}))
    check('--' in fail, '取数失败也能出占位文案')

    print('[4] 涨跌停段 limit_segments')
    segs = vc.limit_segments({'limit_up': 52, 'limit_down': 3})
    check(''.join(t for t, _ in segs) == ' 52/3', '默认文案 " 52/3"')
    check([k for t, k in segs if k] == [vc.LIMIT_SEG_UP, vc.LIMIT_SEG_DN], '前红后绿语义')
    check(vc.limit_segments({'limit_up': None, 'limit_down': None}) == [], '都缺失 -> 空段')
    check(vc.fmt_limits({'limit_up': 52, 'limit_down': 3}) == '涨停52 / 跌停3',
          '菜单文案保留中文')
    check(vc.need_flash({'limit_down': 11, 'phase': 'trading'}) is True,
          '跌停 > 阈值 且盘中 -> 告警')
    check(vc.need_flash({'limit_down': 11, 'phase': 'closed'}) is False, '收盘后不告警')

    print('[5] 配置读写 load_settings / save_settings')
    # 用项目目录下的临时文件夹: 某些受限环境没有可写的系统临时目录(tempfile 会直接报错)
    real = vc.SETTINGS_FILE
    tmpdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.selftest_tmp')
    shutil.rmtree(tmpdir, ignore_errors=True)
    os.makedirs(tmpdir, exist_ok=True)
    vc.SETTINGS_FILE = os.path.join(tmpdir, '.settings.json')
    try:
        check(vc.load_settings()['cycle_sec'] == vc.CYCLE_SEC_DEFAULT,
              '无配置文件 -> 默认 %d 秒' % vc.CYCLE_SEC_DEFAULT)
        vc.save_settings({'cycle_sec': 5, 'show_volume': True,
                          'stocks': [{'code': '600000', 'name': '浦发银行'},
                                     {'code': 'sh600000', 'name': '重复'},
                                     {'code': 'bj430047', 'name': '诺思兰德'}]})
        got = vc.load_settings()
        check(got['cycle_sec'] == 5 and got['show_volume'] is True, '往返: 间隔/成交量')
        check([x['code'] for x in got['stocks']] == ['sh600000', 'bj430047'],
              '往返: 去重 + 代码补市场前缀')
        vc.save_settings({'cycle_sec': 999, 'show_volume': False, 'stocks': []})
        check(vc.load_settings()['cycle_sec'] == vc.CYCLE_SEC_MAX, '间隔超上限被夹住')
        vc.save_settings({'cycle_sec': -3, 'show_volume': False, 'stocks': []})
        check(vc.load_settings()['cycle_sec'] == 1, '间隔为负按 1 处理')
        with open(vc.SETTINGS_FILE, 'w', encoding='utf-8') as f:
            f.write('{坏 JSON')
        check(vc.load_settings()['cycle_sec'] == vc.CYCLE_SEC_DEFAULT,
              '配置损坏 -> 回默认, 不抛异常')
        data = json.loads(json.dumps(vc.load_settings(), ensure_ascii=False))
        check(isinstance(data, dict), 'load_settings 返回可 JSON 序列化的 dict')
    finally:
        vc.SETTINGS_FILE = real
        shutil.rmtree(tmpdir, ignore_errors=True)

    print('[6] 成交额增减追踪 StockTracker')
    tr = vc.StockTracker()
    r = tr.update([{'code': 'sh600000', 'amount_wan': 100.0}])
    check(r[0]['delta_wan'] is None, '首轮无基线 -> None')
    r = tr.update([{'code': 'sh600000', 'amount_wan': 160.0}])
    check(r[0]['delta_wan'] == 60.0, '第二轮 delta = 60')
    r = tr.update([{'code': 'sh600000', 'amount_wan': None}])
    check(r[0]['delta_wan'] is None, '取数失败 -> None(不污染基线判断)')

    if '--net' in sys.argv:
        print('[7] 联网取数 analyze()')
        snap = vc.analyze()
        print('      %.0f 万元 / phase=%s / 指数 %d 个'
              % (snap['cur_amount_wan'], snap['phase'], len(snap.get('indices') or [])))
        check(snap['cur_amount_wan'] > 0, '全市场成交额 > 0')
        check(len(snap.get('indices') or []) >= 1, '至少一个指数点位')
        rows = vc.watch_snapshot(['sh600000'], snap)
        print('      %s %s 昨%s 今%s' % (rows[0]['code'], rows[0]['name'],
                                        rows[0]['prev_amount_wan'], rows[0]['amount_wan']))
        check(rows[0]['price'] is not None, '个股现价取到')

    print('')
    if FAIL:
        print('失败 %d 项:' % len(FAIL))
        for m in FAIL:
            print('  - ' + m)
        return 1
    print('全部通过 (%d 项)' % N[0])
    return 0


if __name__ == '__main__':
    sys.exit(main())
