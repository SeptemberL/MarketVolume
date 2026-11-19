#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
全市场量能监控(沪深京) - macOS 状态栏常驻
==========================================
- 状态栏标题: 每10秒刷新, 显示 沪/创/科 三大指数点位+涨跌幅(红涨绿跌,
  沪=上证指数 创=创业板指 科=科创50) + 全市场总成交额+刷新间量能箭头 + 量能差额 + 涨跌停家数 + 预测
  状态词口径: 「放量/缩量」汉字已从标题移除(2026-10-19, 只靠颜色表意: 放量=红 缩量=绿),
  差额数字前不再带状态字, 转换记录仍保留在菜单的"节奏转换历史"里
- 涨跌停家数(2026-10-19 新增): 涨停=红 跌停=绿, 数据来自东财涨停池/跌停池;
  接口失败或非交易日时该段自动省略(不影响量能主逻辑)
- 点击菜单: 详细数据(分市场成交额/点位/成交量对比/预测) + 分钟节奏与转换历史
- 状态机: 分钟级节奏跟踪, 本分钟增量 vs 上分钟增量纯对比(放量/缩量, 无平量死区), 节奏翻转即记录转换

启动: python3 volume_menu.py   (首次启动系统会询问权限)
退出: 点击菜单里的"退出"
"""
import os
import subprocess
import sys
import time
from datetime import datetime

import rumps
from AppKit import (NSColor, NSMutableAttributedString,
                    NSForegroundColorAttributeName, NSBackgroundColorAttributeName)

from volume_core import (analyze, fmt_amount, fmt_hand, index_brief, PaceTracker,
                         limit_segments, fmt_limits, fmt_limit_date, need_flash,
                         FLASH_SEC, STATE_NA, STATE_UP, STATE_DN,
                         LIMIT_SEG_UP, LIMIT_SEG_DN,
                         load_settings, settings_mtime, watch_snapshot, StockTracker,
                         stock_segments, fmt_delta_wan,
                         SEG_UP, SEG_DN, SEG_FLAT, SEG_DIM,
                         CYCLE_SEC_DEFAULT)

REFRESH_SEC = 10        # 状态栏刷新间隔
REFRESH_RETRY = 2       # 单次刷新内分析失败重试次数(analyze 内部已含 HTTP 重试)
REFRESH_RETRY_DELAY = 2 # 重试间隔(秒)
FAIL_STREAK_SLOW = 3    # 连续失败达到该次数后降频, 避免每10秒反复阻塞 UI
SLOW_REFRESH_SEC = 30   # 接口故障期刷新间隔(秒), 恢复成功后自动回到 REFRESH_SEC
COMPACT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            '.compact_mode')  # 简洁模式开关持久化(存在即开启)
SETTINGS_WIN = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'settings_win.py')   # 设置窗口(独立进程拉起)

UP_COLOR = NSColor.systemRedColor()      # 放量 -> 红
DN_COLOR = NSColor.systemGreenColor()    # 缩量 -> 绿
# 个股卡片里的次要字段(昨/预/量)用**亮黄 #FFD54A**: 原来是系统灰, 在状态栏上太淡看不清
DIM_COLOR = NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 0.835, 0.29, 1.0)
FLASH_FG = NSColor.whiteColor()          # 跌停告警闪烁态: 绿底白字
SEG_COLORS = {SEG_UP: UP_COLOR, SEG_DN: DN_COLOR,
              SEG_FLAT: None, SEG_DIM: DIM_COLOR}


class VolumeMenuApp(rumps.App):
    def __init__(self):
        super().__init__('量能监控', title='沪指 加载中…')
        self._pace = PaceTracker()    # 分钟级节奏(vs 1分钟前): 转换检测专用
        self._last_snapshot = None    # 最近一次成功快照
        self._last_err = None
        self._prev_amount_wan = None  # 上一次刷新的全市场成交额(万元), 用于箭头
        self._delta_wan = None        # 本次刷新较上次的成交额变化(万元)
        self._fail_streak = 0         # 连续失败次数(用于降频)
        self._last_try_ts = 0.0       # 上次真正尝试刷新的时间戳(降频用)
        self._fail_timer = None       # 失败倒计时1秒定时器(显示"还有几秒重试")
        # 简洁模式: 标题只留 点位+涨跌幅+量能差额+涨跌停数, 防止被宽菜单App挤出状态栏
        self._compact = os.path.exists(COMPACT_FILE)
        # 跌停告警闪烁: 跌停家数>10 时每分钟闪一次(见 volume_core.need_flash)
        self._flash_on = False        # 当前是否处于"闪烁"帧
        self._flash_minute = None     # 已经闪过的那一分钟(now//60), 保证每分钟只触发一次
        self._flash_until = 0.0       # 本次闪烁的结束时间戳
        # 监视股票轮播(设置页写 .settings.json): 指数视图 <-> 个股视图 循环
        self._settings = load_settings()
        self._settings_mtime = settings_mtime()
        self._stocks = {}             # code -> 个股快照(最近一轮)
        self._stock_tracker = StockTracker()   # 个股成交额"较上次刷新"的增减
        self._cycle_keys = self._build_cycle_keys()
        self._cycle_idx = 0
        self._view = 'index'          # 当前视图: 'index' 或 个股代码
        self._cycle_ts = time.time()
        self._settings_win = None     # 设置窗口控制器(AppKit, 需强引用)

        self.timer = rumps.Timer(self.refresh, REFRESH_SEC)
        self.timer.start()
        self.flash_timer = rumps.Timer(self._tick_flash, 1)   # 1秒心跳: 管闪烁
        self.flash_timer.start()
        self.cycle_timer = rumps.Timer(self._tick_cycle, 1)   # 1秒心跳: 管轮播/热加载
        self.cycle_timer.start()
        self.refresh(self)

    # ---------- 监视股票轮播 ----------
    def _cfg(self):
        """当前设置(离线自检时对象是 object.__new__ 造的, 没有 _settings)"""
        return getattr(self, '_settings', None) or {'stocks': [],
                                                    'cycle_sec': CYCLE_SEC_DEFAULT}

    def _build_cycle_keys(self):
        """轮播序列: 指数视图 + 每只监视股票(顺序即设置页里的顺序)"""
        return ['index'] + [s['code'] for s in self._cfg().get('stocks') or []]

    def _reload_settings_if_changed(self):
        """设置页保存后(.settings.json mtime 变化)热加载, 无需重启"""
        mt = settings_mtime()
        if mt == self._settings_mtime:
            return False
        self._settings_mtime = mt
        old = self._cycle_keys
        self._settings = load_settings()
        self._cycle_keys = self._build_cycle_keys()
        if self._cycle_keys != old:      # 股票列表变了才复位到指数视图
            self._cycle_idx = 0
            self._view = 'index'
            self._stocks = {}
            self._stock_tracker.reset()
        if self._last_snapshot and not self._last_err:
            self._set_colored_title(self._last_snapshot)
        return True

    def _tick_cycle(self, _):
        """每秒心跳: 到点切换视图(指数 <-> 个股), 顺带热加载设置"""
        self._reload_settings_if_changed()
        keys = self._cycle_keys
        if len(keys) <= 1:               # 只有指数: 不需切换
            return
        now = time.time()
        if now - self._cycle_ts < self._cycle_sec():
            return
        self._cycle_ts = now
        self._cycle_idx = (self._cycle_idx + 1) % len(keys)
        self._view = keys[self._cycle_idx]
        if self._last_snapshot and not self._last_err:
            self._set_colored_title(self._last_snapshot)
        self._rebuild_menu()

    def _cycle_sec(self):
        try:
            return max(1, int(self._cfg().get('cycle_sec') or CYCLE_SEC_DEFAULT))
        except (TypeError, ValueError):
            return 3

    def _current_stock(self):
        """当前视图对应的个股快照(不在个股视图/数据未到 -> None)"""
        if getattr(self, '_view', 'index') == 'index':
            return None
        return (getattr(self, '_stocks', None) or {}).get(self._view)

    def _fetch_stocks(self, snap):
        """抓一轮监视股票(含昨日基准/预测/增减); 失败不影响大盘标题"""
        codes = [s['code'] for s in self._cfg().get('stocks') or []]
        if not codes:
            self._stocks = {}
            return
        try:
            rows = watch_snapshot(codes, snap)
        except Exception as e:
            print('[%s] 个股行情获取失败: %s: %s'
                  % (datetime.now().strftime('%H:%M:%S'), type(e).__name__, e),
                  file=sys.stderr)
            return
        self._stock_tracker.update(rows)
        self._stocks = {r['code']: r for r in rows}

    # ---------- 跌停告警闪烁 ----------
    def _tick_flash(self, _):
        """每秒一次: 跌停家数超过阈值时, 每分钟让跌停数字闪一下(FLASH_SEC 秒后复原)

        不依赖 10 秒刷新节拍, 用独立心跳保证"每分钟一次"的节奏稳定;
        只在盘中/午休闪(收盘后/开盘前没意义)。
        """
        s = self._last_snapshot
        if not s or self._last_err or not need_flash(s):
            if getattr(self, '_flash_on', False):
                self._flash_on = False
                self._flash_minute = None
                if s:
                    self._set_colored_title(s)
            return
        now = time.time()
        minute = int(now // 60)
        if self._flash_minute != minute:      # 进入新的一分钟 -> 触发一次
            self._flash_minute = minute
            self._flash_on = True
            self._flash_until = now + FLASH_SEC
            self._set_colored_title(s)
        elif self._flash_on and now >= self._flash_until:
            self._flash_on = False
            self._set_colored_title(s)

    # ---------- 核心刷新 ----------
    def refresh(self, _):
        # 接口故障降频: 连续失败期间跳过中间周期, 避免每10秒反复长时间阻塞 UI
        gap = SLOW_REFRESH_SEC if self._fail_streak >= FAIL_STREAK_SLOW else REFRESH_SEC
        now = time.time()
        if now - self._last_try_ts < gap:
            return
        self._last_try_ts = now

        snap = None
        last_err = None
        for attempt in range(REFRESH_RETRY):
            try:
                snap = analyze()
                break
            except Exception as e:
                last_err = '%s: %s' % (type(e).__name__, e)
                if attempt < REFRESH_RETRY - 1:
                    time.sleep(REFRESH_RETRY_DELAY)

        if snap:
            self._fail_streak = 0
            if self._fail_timer is not None:
                self._fail_timer.stop()
                self._fail_timer = None
            self._last_snapshot = snap
            self._last_err = None
            # 与上一次刷新比较(10秒间隔): 正=放量, 负=缩量, 平量=None不显示箭头
            self._delta_wan = (snap['cur_amount_wan'] - self._prev_amount_wan
                               if self._prev_amount_wan is not None else None)
            self._pace.update(snap)
            self._fetch_stocks(snap)
            self._set_colored_title(snap)
            self._prev_amount_wan = snap['cur_amount_wan']
        else:
            # 所有重试均失败: 状态栏必须立即显示失败提示(否则被旧着色标题盖住,
            # 表现为"半天不更新"), 并保留上一次成功数据供菜单查看
            self._fail_streak += 1
            self._last_err = last_err
            # 注意: 不能用 traceback.print_exc()(此时已不在异常块内, 会打印 NoneType: None)
            print('[%s] 刷新失败(第%d次): %s'
                  % (datetime.now().strftime('%H:%M:%S'), self._fail_streak, last_err),
                  file=sys.stderr)
            self._set_fail_title()
            self._start_fail_countdown()
        self._rebuild_menu()

    # ---------- 失败倒计时(1秒刷新"还有几秒重试") ----------
    def _start_fail_countdown(self):
        if self._fail_timer is None:
            self._fail_timer = rumps.Timer(self._tick_fail_countdown, 1)
            self._fail_timer.start()

    def _tick_fail_countdown(self, _):
        if not self._last_err:
            return
        gap = SLOW_REFRESH_SEC if self._fail_streak >= FAIL_STREAK_SLOW else REFRESH_SEC
        remain = max(1, int(gap - (time.time() - self._last_try_ts)) + 1)
        self._set_fail_title(remain)

    # ---------- 失败提示(attributed title, 保证可见) ----------
    def _set_fail_title(self, remaining=None):
        """接口抽风: 灰色显示"更新失败"+ 旧数据时间(+还有几秒重试)
        必须走 setAttributedTitle_, 普通 title 会被上一次成功时的着色标题覆盖"""
        ts = '--:--'
        if self._last_snapshot and self._last_snapshot.get('update_time'):
            hhmmss = self._last_snapshot['update_time']   # yyyyMMddHHmmss
            ts = '%s:%s' % (hhmmss[8:10], hhmmss[10:12])
        text = '更新失败 %ds后重试 旧%s' % (remaining, ts) if remaining else '更新失败 旧%s' % ts
        self.title = text                                 # 同步 plain title(双保险)
        nsapp = getattr(self, '_nsapp', None)
        if nsapp is None:
            return
        attr = NSMutableAttributedString.alloc().initWithString_(text)
        gray = NSColor.systemGrayColor()
        attr.addAttribute_value_range_(NSForegroundColorAttributeName, gray, (0, len(text)))
        nsapp.nsstatusitem.button().setAttributedTitle_(attr)

    def _index_segments(self, s, price_dec=1, pct_dec=2):
        """沪/创/科 三大指数标题分段: 缩写+点位 后跟涨跌幅(红涨绿跌)

        指数取自快照 indices(上证指数/创业板指/科创50); 缺失时回退旧字段(仅沪指),
        保证接口只返回部分指数或旧快照时标题不至于空白。
        """
        idxs = s.get('indices')
        if not idxs:
            idxs = [{'abbr': '沪', 'price': s.get('price'),
                     'change_pct': s.get('change_pct')}]
        segs = []
        for idx in idxs:
            p_txt, c_txt, cval = index_brief(idx, price_dec, pct_dec)
            if segs:
                segs.append((' ', None))
            segs.append((p_txt, None))
            if c_txt:
                color = (UP_COLOR if cval > 0 else
                         DN_COLOR if cval < 0 else None)
                segs.append((c_txt, color))
        return segs

    # ---------- 标题(分段着色) ----------
    def _compose_title(self, s):
        """返回 [(文本段, 颜色[, 背景色])]; 指数涨跌幅红涨绿跌; 差额按 放量红/缩量绿;
        末尾是涨跌停家数(只出数字, 跌停超阈值时带绿底闪烁帧)

        轮播到个股时(见 _tick_cycle)整条标题换成该股卡片:
        名称 现价 涨跌幅 昨成交额 今成交额 较上次增减 预成交额 [成交量]
        """
        st = self._current_stock()
        if st is not None:
            return self._stock_title(st, s)
        ph = s['phase']
        if ph == 'weekend':
            return [('沪创科 休市', None)]
        if self._compact:
            return self._compose_compact(s)
        if ph == 'pre_open':
            return [('沪创科 未开盘 昨%s' % fmt_amount(s['prev_amount_wan'], abbr=True),
                     None)]
        parts = []
        if ph == 'lunch':
            parts.append(('午休 ', None))
        parts.extend(self._index_segments(s, price_dec=1, pct_dec=2))
        # 全市场总成交额(标题用缩写口径: 万亿 -> WY, 省状态栏宽度)
        parts.append((' %s' % fmt_amount(s['cur_amount_wan'], abbr=True), None))
        # 相比上一次刷新: 放量->红↑ 缩量->绿↓ (平量不显示)
        if self._delta_wan is not None and self._delta_wan > 0:
            parts.append((' ↑', UP_COLOR))
        elif self._delta_wan is not None and self._delta_wan < 0:
            parts.append((' ↓', DN_COLOR))
        if s['state'] != STATE_NA and s['amount_diff_wan'] is not None:
            # 状态词(分钟节奏口径, vs 1分钟前): 只显示当前节奏, 未启动时兜底 vs 昨日大盘状态
            # 2026-09-11 去掉"放量转缩量/缩量转放量"转换词;
            # 2026-10-19 起连「放量/缩量」两个汉字也去掉 —— 语义只靠颜色表达,
            # 给新增的涨跌停家数腾出状态栏宽度
            ref = self._pace.pace or s['state']
            color = (UP_COLOR if ref == STATE_UP else
                     DN_COLOR if ref == STATE_DN else None)
            yi = s['amount_diff_wan'] / 10000.0
            sgn = '+' if yi >= 0 else ''
            parts.append((' %s%.0f亿' % (sgn, yi), color))            # +1604亿
        if s['forecast_wan']:
            parts.append((' 预%s' % fmt_amount(s['forecast_wan'], abbr=True), None))
        # 涨跌停家数(东财, 只出数字): 放在"预测成交额"之后, 涨停红/跌停绿
        parts.extend(self._limit_parts(s))
        return parts

    def _stock_title(self, st, s):
        """个股视图标题段(文案由 volume_core.stock_segments 统一生成, 两端一致)"""
        return stock_segments(
            st,
            show_volume=bool(self._cfg().get('show_volume')),
            compact=self._compact,
            with_today=s['phase'] in ('trading', 'lunch', 'closed'))

    def _limit_parts(self, s):
        """涨跌停数字段 -> [(文本, 前景色, 背景色|None)]; 涨停红 / 跌停绿

        跌停家数 > FLASH_DT_THRESHOLD 时, 由 _tick_flash 每分钟置一次 _flash_on,
        把跌停数字画成"绿底白字"闪一下; 缺失为空段(标题自动省略)。
        """
        flash = bool(getattr(self, '_flash_on', False)) and need_flash(s)
        out = []
        for t, k in limit_segments(s):
            if k == LIMIT_SEG_UP:
                out.append((t, UP_COLOR, None))
            elif k == LIMIT_SEG_DN:
                out.append((t, FLASH_FG if flash else DN_COLOR,
                            DN_COLOR if flash else None))
            else:
                out.append((t, None, None))
        return out

    def _compose_compact(self, s):
        """简洁模式: `沪3722 +0.16% 创2743 -1.07% 科1336 -1.68%↑ +76亿 28/9`"""
        if s['phase'] == 'pre_open':
            return [('沪创科 未开盘', None)]
        if s['phase'] == 'lunch':
            return [('沪创科 午休', None)]
        parts = self._index_segments(s, price_dec=0, pct_dec=2)
        if self._delta_wan is not None and self._delta_wan > 0:
            parts.append(('↑', UP_COLOR))
        elif self._delta_wan is not None and self._delta_wan < 0:
            parts.append(('↓', DN_COLOR))
        if s['state'] != STATE_NA and s['amount_diff_wan'] is not None:
            # 简洁模式同样只显示差额数字(无「放量/缩量」汉字, 颜色表意)
            ref = self._pace.pace or s['state']
            color = (UP_COLOR if ref == STATE_UP else
                     DN_COLOR if ref == STATE_DN else None)
            yi = s['amount_diff_wan'] / 10000.0
            sgn = '+' if yi >= 0 else ''
            parts.append((' %s%.0f亿' % (sgn, yi), color))
        parts.extend(self._limit_parts(s))
        return parts

    def _toggle_compact(self, _):
        self._compact = not self._compact
        try:
            if self._compact:
                open(COMPACT_FILE, 'w').close()
            else:
                os.path.exists(COMPACT_FILE) and os.remove(COMPACT_FILE)
        except OSError:
            pass
        if self._last_snapshot:
            self._set_colored_title(self._last_snapshot)
        self._rebuild_menu()

    def _set_colored_title(self, s):
        segs = self._compose_title(s)
        text = ''.join(seg[0] for seg in segs)
        self.title = text
        nsapp = getattr(self, '_nsapp', None)
        if nsapp is None:
            return   # run() 前无状态栏, 仅存 plain title; Timer 首次触发后即有着色
        attr = NSMutableAttributedString.alloc().initWithString_(text)
        pos = 0
        for seg in segs:
            t, c = seg[0], seg[1]
            if isinstance(c, str):          # 个股段的语义色(SEG_UP/SEG_DN/…)转 NSColor
                c = SEG_COLORS.get(c)
            bg = seg[2] if len(seg) > 2 else None
            if c is not None:
                attr.addAttribute_value_range_(
                    NSForegroundColorAttributeName, c, (pos, len(t)))
            if bg is not None:   # 仅跌停告警闪烁帧用(绿底)
                attr.addAttribute_value_range_(
                    NSBackgroundColorAttributeName, bg, (pos, len(t)))
            pos += len(t)
        nsapp.nsstatusitem.button().setAttributedTitle_(attr)

    # ---------- 菜单 ----------
    def _rebuild_menu(self):
        s = self._last_snapshot
        if not s:
            self.menu = [rumps.MenuItem('暂无数据')]
            return

        ph = {'pre_open': '未开盘', 'trading': '交易中', 'lunch': '午休',
              'closed': '已收盘', 'weekend': '休市'}[s['phase']]
        items = []

        idxs = s.get('indices') or [{'abbr': '沪', 'name': '上证指数',
                                     'price': s['price'],
                                     'change_pct': s['change_pct']}]
        for n, idx in enumerate(idxs):
            label = '%s %.2f (%+.2f%%)' % (idx.get('abbr', '沪'), idx['price'],
                                           idx['change_pct'])
            if n == 0:
                label += '   %s' % ph
            items.append(rumps.MenuItem(label))
        items.append(None)  # 分隔线
        items.append(rumps.MenuItem('全市场成交额: %s' % fmt_amount(s['cur_amount_wan'])))
        if self._delta_wan is not None and self._delta_wan != 0:
            d = self._delta_wan / 10000.0
            sgn = '+' if d > 0 else ''
            arrow = '↑' if d > 0 else '↓'
            items.append(rumps.MenuItem('较上次刷新(10秒): %s%.2f亿 %s' % (sgn, d, arrow)))
        b = s.get('breakdown_wan') or {}
        if b:
            items.append(rumps.MenuItem('  沪 %s / 深 %s / 北 %s'
                                        % (fmt_amount(b.get('sh000001', 0)),
                                           fmt_amount(b.get('sz399106', 0)),
                                           fmt_amount(b.get('bj899050', 0)))))
        items.append(rumps.MenuItem('昨日全天(沪深京): %s' % fmt_amount(s['prev_amount_wan'])))
        if s.get('limit_up') is not None or s.get('limit_down') is not None:
            items.append(rumps.MenuItem('涨跌停(%s): %s'
                                        % (fmt_limit_date(s.get('limit_date')),
                                           fmt_limits(s))))
        if s['amount_diff_wan'] is not None:
            yi = s['amount_diff_wan'] / 10000.0
            sgn = '+' if yi >= 0 else ''
            label = s['state'] if s['state'] != STATE_NA else '--'
            items.append(rumps.MenuItem('较上一日同时刻: %s%s%.0f亿' % (label, sgn, yi)))
        if s['state'] != STATE_NA:
            items.append(rumps.MenuItem('量能状态(vs昨日): %s (量比 %sx)'
                                        % (s['state'],
                                           s['vol_ratio'] if s['vol_ratio'] is not None else '-')))
        # 分钟节奏(vs 1分钟前): 转换检测口径
        p = self._pace.pace
        if p or self._pace.last_inc is not None:
            items.append(rumps.MenuItem('分钟节奏(vs1分钟前): %s' % (p or '--')))
        if self._pace.last_inc is not None:
            items.append(rumps.MenuItem('  本分钟增量: %s / 上一分钟: %s'
                                        % (fmt_amount(self._pace.last_inc),
                                           fmt_amount(self._pace.prev_inc)
                                           if self._pace.prev_inc is not None else '-')))
        items.append(None)  # 分隔线
        if s['today_cum_hand']:
            items.append(rumps.MenuItem('当前累计量(沪深): %s' % fmt_hand(s['today_cum_hand'])))
        if s['yest_cum_hand'] is not None:
            items.append(rumps.MenuItem('昨日同时刻(沪深): %s' % fmt_hand(s['yest_cum_hand'])))
        if s['vol_diff_hand'] is not None:
            sgn = '+' if s['vol_diff_hand'] >= 0 else ''
            items.append(rumps.MenuItem('量差: %s%s' % (sgn, fmt_hand(abs(s['vol_diff_hand'])))))
        if s['yest_total_hand']:
            items.append(rumps.MenuItem('昨日全天量(沪深): %s' % fmt_hand(s['yest_total_hand'])))
        if s['forecast_vol_hand']:
            items.append(rumps.MenuItem('预测全天量(沪深): %s' % fmt_hand(s['forecast_vol_hand'])))
        if s['forecast_wan']:
            items.append(rumps.MenuItem('预测全天成交额: %s' % fmt_amount(s['forecast_wan'])))
        items.append(rumps.MenuItem('数据时间: %s' % s['update_time']))

        # ---- 监视股票(与指数轮播) ----
        stocks = self._cfg().get('stocks') or []
        items.append(None)
        if stocks:
            items.append(rumps.MenuItem('监视股票 %d 只 · 每 %d 秒切换一项'
                                        % (len(stocks), self._cycle_sec())))
            for st_cfg in stocks:
                st = (getattr(self, '_stocks', None) or {}).get(st_cfg['code'])
                cur = '▶ ' if getattr(self, '_view', 'index') == st_cfg['code'] else '  '
                if not st:
                    items.append(rumps.MenuItem('%s%s %s  取数中…'
                                                % (cur, st_cfg['code'],
                                                   st_cfg.get('name') or '')))
                    continue
                label = ''.join(t for t, _ in stock_segments(
                    st, show_volume=bool(self._cfg().get('show_volume')),
                    with_today=s['phase'] in ('trading', 'lunch', 'closed')))
                items.append(rumps.MenuItem(cur + label))
                extra = []
                if st.get('amount_diff_wan') is not None:
                    extra.append('较昨日同时刻 %s' % fmt_delta_wan(st['amount_diff_wan']))
                if st.get('prev_vol_hand'):
                    extra.append('昨量 %s' % fmt_hand(st['prev_vol_hand']))
                if extra:
                    items.append(rumps.MenuItem('      ' + ' · '.join(extra)))
        else:
            items.append(rumps.MenuItem('监视股票: 未添加（菜单 → 设置…）'))

        if self._pace.trans_log:
            items.append(None)
            items.append(rumps.MenuItem('节奏转换历史(vs1分钟前):'))
            for t in self._pace.trans_log:
                items.append(rumps.MenuItem('%s转%s  %s  本分钟%s vs 上分钟%s'
                                            % (t['from'], t['to'], t['time'],
                                               fmt_amount(t['inc_wan']),
                                               fmt_amount(t['prev_inc_wan']))))

        if self._last_err:
            items.append(None)
            items.append(rumps.MenuItem('最近错误: %s' % self._last_err))

        items.append(None)
        items.append(rumps.MenuItem('刷新间隔: %d 秒' % REFRESH_SEC))
        items.append(rumps.MenuItem('设置…（监视股票 / 切换间隔）',
                                    callback=self._open_settings))
        items.append(rumps.MenuItem(
            '%s简洁模式(标题减半, 防被宽菜单App挤出状态栏)'
            % ('✓ ' if self._compact else ''),
            callback=self._toggle_compact))
        items.append(rumps.MenuItem('退出', callback=rumps.quit_application))
        self.menu = items

    def _open_settings(self, _):
        """打开设置窗口

        macOS: 原生 AppKit 窗口, 在同一进程里(保存后立刻生效) —— 本机 Python 的
               tkinter 缺 Tcl 运行时, 起不了窗口, 详见 settings_mac.py 头部说明。
        其它平台: 拉起 tkinter 版 settings_win.py 独立进程。
        """
        if sys.platform == 'darwin':
            print('[设置] 打开设置窗口 %s' % datetime.now().strftime('%H:%M:%S'),
                  file=sys.stderr)
            try:
                import settings_mac
                settings_mac.show_settings(self)
            except Exception as e:
                print('[设置] 打开失败: %s: %s' % (type(e).__name__, e),
                      file=sys.stderr)
            return
        try:
            subprocess.Popen([sys.executable, SETTINGS_WIN],
                             cwd=os.path.dirname(SETTINGS_WIN))
        except Exception as e:
            print('[设置] 打开失败: %s: %s' % (type(e).__name__, e), file=sys.stderr)

    def apply_settings_now(self):
        """设置窗口点保存后立即生效: 重读配置 -> 重抓个股 -> 重绘标题/菜单"""
        self._settings_mtime = settings_mtime()
        old = self._cycle_keys
        self._settings = load_settings()
        self._cycle_keys = self._build_cycle_keys()
        if self._cycle_keys != old:
            self._cycle_idx = 0
            self._view = 'index'
            self._stocks = {}
            self._stock_tracker.reset()
        snap = self._last_snapshot
        if snap:
            self._fetch_stocks(snap)
            if not self._last_err:
                self._set_colored_title(snap)
            self._rebuild_menu()


if __name__ == '__main__':
    if not sys.platform == 'darwin':
        print('仅支持 macOS', file=sys.stderr)
        sys.exit(1)
    try:
        VolumeMenuApp().run()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
