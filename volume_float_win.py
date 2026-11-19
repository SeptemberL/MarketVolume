#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
全市场量能监控(沪深京) - Windows 悬浮框
==========================================
- 无边框置顶悬浮窗: 始终在最前, 可拖拽移动
- 悬浮标题: 沪/创/科 三大指数点位+涨跌幅(红涨绿跌, 上证指数/创业板指/科创50)
  + 全市场成交额 + 量能差额 + 涨跌停家数 + 预测
  「放量/缩量」汉字已从标题移除(2026-10-19, 只靠颜色表意), 转换记录仍在展开详情的"节奏转换历史"里
- 涨跌停家数(2026-10-19 新增): 涨停=红 跌停=绿, 数据来自东财涨停池/跌停池;
  接口失败或非交易日时该段自动省略(不影响量能主逻辑)
- 点击展开详情面板: 分市场明细/成交量对比/分钟节奏与转换历史/详细数据
- 每10秒自动刷新, 失败倒计时重试, 连续失败自动降频
- 位置记忆: 拖拽后位置写入 .win_position 文件, 重启恢复
- 右键菜单: 展开/收起、立即刷新、简洁模式、退出

启动: python volume_float_win.py
退出: 右键菜单 -> 退出, 或点击右上角 ✕
"""
import os
import subprocess
import sys
import time
from datetime import datetime
import tkinter as tk
from tkinter import font as tkfont

# ---- 复用 macOS 版同一核心逻辑(纯标准库, 跨平台) ----
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from volume_core import (analyze, fmt_amount, fmt_hand, index_brief, PaceTracker,
                         limit_segments, fmt_limits, fmt_limit_date, need_flash,
                         FLASH_SEC, STATE_NA, STATE_UP, STATE_DN,
                         LIMIT_SEG_UP, LIMIT_SEG_DN,
                         load_settings, settings_mtime, watch_snapshot, StockTracker,
                         stock_segments, fmt_delta_wan,
                         SEG_UP, SEG_DN, SEG_FLAT, SEG_DIM,
                         CYCLE_SEC_DEFAULT)

# ============ 配置 ============
REFRESH_SEC = 10
REFRESH_RETRY = 2
REFRESH_RETRY_DELAY = 2
FAIL_STREAK_SLOW = 3
SLOW_REFRESH_SEC = 30
POS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.win_position')
COMPACT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.compact_mode')
SETTINGS_WIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'settings_win.py')

# 颜色(中国股市惯例: 涨/放量=红, 跌/缩量=绿)
C_UP = '#FF4444'
C_DN = '#00C853'
C_BG = '#1A1A2E'
C_BG_HOVER = '#252540'
C_BG_DETAIL = '#222234'
C_FG = '#E8E8E8'
C_FG_DIM = '#8A8A9A'
C_TITLE_DIM = '#FFD54A'  # 个股卡片次要字段(昨/预/量): 亮黄, 比灰色显眼
C_WARN = '#FFAA00'
C_FLASH_FG = '#FFFFFF'   # 跌停告警闪烁态: 绿底白字
# 个股标题分段的语义色(volume_core.stock_segments 只给语义, 颜色由两端各自映射)
SEG_COLORS = {SEG_UP: C_UP, SEG_DN: C_DN, SEG_FLAT: C_FG, SEG_DIM: C_TITLE_DIM}


class VolumeFloatWin:
    """Windows 悬浮框量能监控"""

    def __init__(self):
        self.root = tk.Tk()
        self.root.title('量能监控')
        self.root.overrideredirect(True)         # 无边框
        self.root.attributes('-topmost', True)   # 置顶
        self.root.attributes('-alpha', 0.96)     # 轻微半透明
        self.root.configure(bg=C_BG)

        # ---- 运行状态 ----
        self._pace = PaceTracker()   # 分钟级节奏(vs 1分钟前): 转换检测专用
        self._last_snapshot = None
        self._last_err = None
        self._prev_amount_wan = None
        self._delta_wan = None
        self._fail_streak = 0
        self._last_try_ts = 0.0
        self._expanded = False
        self._compact = os.path.exists(COMPACT_FILE)
        # 跌停告警闪烁: 跌停家数>10 时每分钟闪一次(见 volume_core.need_flash)
        self._flash_on = False
        self._flash_minute = None
        self._flash_until = 0.0
        self._drag_data = {'x': 0, 'y': 0}
        # 监视股票轮播(设置页写 .settings.json): 指数视图 <-> 个股视图 循环
        self._settings = load_settings()
        self._settings_mtime = settings_mtime()
        self._stocks = {}                      # code -> 个股快照
        self._stock_tracker = StockTracker()   # 个股成交额"较上次刷新"的增减
        self._cycle_keys = self._build_cycle_keys()
        self._cycle_idx = 0
        self._view = 'index'
        self._cycle_ts = time.time()

        # ---- 字体 ----
        try:
            self.font_main = tkfont.Font(family='Microsoft YaHei', size=11)
            self.font_small = tkfont.Font(family='Microsoft YaHei', size=9)
        except Exception:
            self.font_main = tkfont.Font(size=11)
            self.font_small = tkfont.Font(size=9)

        # ---- UI ----
        self._build_ui()
        self._restore_position()

        # ---- 拖拽/菜单(全局绑定, 用 x_root 屏幕坐标避免事件目标漂移) ----
        self.root.bind_all('<Button-1>', self._on_drag_start)
        self.root.bind_all('<B1-Motion>', self._on_drag_motion)
        self.root.bind_all('<Button-3>', self._show_context_menu)

        # ---- 启动 ----
        self.refresh()
        self.root.after(1000, self._tick_flash)   # 1秒心跳: 管跌停告警闪烁
        self.root.after(1000, self._tick_cycle)   # 1秒心跳: 管轮播/设置热加载
        self.root.mainloop()

    # ============ 监视股票轮播 ============
    def _cfg(self):
        """当前设置(离线自检时对象是 object.__new__ 造的, 没有 _settings)"""
        return getattr(self, '_settings', None) or {'stocks': [],
                                                    'cycle_sec': CYCLE_SEC_DEFAULT}

    def _build_cycle_keys(self):
        """轮播序列: 指数视图 + 每只监视股票(顺序即设置页里的顺序)"""
        return ['index'] + [s['code'] for s in self._cfg().get('stocks') or []]

    def _cycle_sec(self):
        try:
            return max(1, int(self._cfg().get('cycle_sec') or CYCLE_SEC_DEFAULT))
        except (TypeError, ValueError):
            return 3

    def _reload_settings_if_changed(self):
        """设置页保存后(.settings.json mtime 变化)热加载, 无需重启"""
        mt = settings_mtime()
        if mt == self._settings_mtime:
            return
        self._settings_mtime = mt
        old = self._cycle_keys
        self._settings = load_settings()
        self._cycle_keys = self._build_cycle_keys()
        if self._cycle_keys != old:
            self._cycle_idx = 0
            self._view = 'index'
            self._stocks = {}
            self._stock_tracker.reset()
        if self._last_snapshot and not self._last_err:
            self._update_title(self._last_snapshot)
            self._update_detail(self._last_snapshot)

    def _tick_cycle(self):
        """每秒心跳: 到点切换视图(指数 <-> 个股), 顺带热加载设置"""
        try:
            self._reload_settings_if_changed()
            keys = self._cycle_keys
            if len(keys) > 1:
                now = time.time()
                if now - self._cycle_ts >= self._cycle_sec():
                    self._cycle_ts = now
                    self._cycle_idx = (self._cycle_idx + 1) % len(keys)
                    self._view = keys[self._cycle_idx]
                    if self._last_snapshot and not self._last_err:
                        self._update_title(self._last_snapshot)
                        self._update_detail(self._last_snapshot)
        finally:
            self.root.after(1000, self._tick_cycle)

    def _current_stock(self):
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
                  % (datetime.now().strftime('%H:%M:%S'), type(e).__name__, str(e)[:100]),
                  file=sys.stderr)
            return
        self._stock_tracker.update(rows)
        self._stocks = {r['code']: r for r in rows}

    # ============ UI 构建 ============
    def _build_ui(self):
        # 主标题行: 左侧彩色分段 + 右侧 ✕
        self.title_row = tk.Frame(self.root, bg=C_BG)
        self.title_row.pack(fill='x', padx=8, pady=(6, 2))

        self.lbl_close = tk.Label(self.title_row, text=' ✕ ', font=self.font_small,
                                  bg=C_BG, fg=C_FG_DIM, cursor='hand2')
        self.lbl_close.pack(side='right')
        self.lbl_close.bind('<Button-1>', self._on_close_click)
        self.lbl_close.bind('<Enter>', lambda e: self.lbl_close.config(fg=C_UP))
        self.lbl_close.bind('<Leave>', lambda e: self.lbl_close.config(fg=C_FG_DIM))

        # 标题分段容器(重建时清空子控件)
        self.seg_frame = tk.Frame(self.title_row, bg=C_BG)
        self.seg_frame.pack(side='left')

        # 详情面板(默认隐藏)
        self.detail_frame = tk.Frame(self.root, bg=C_BG_DETAIL)
        self.detail_text = tk.Label(self.detail_frame, text='', font=self.font_small,
                                    bg=C_BG_DETAIL, fg=C_FG, justify='left', anchor='w')
        self.detail_text.pack(fill='both', expand=True, padx=8, pady=(0, 6))

        # 底部状态行
        self.lbl_status = tk.Label(self.root, text='', font=self.font_small,
                                   bg=C_BG, fg=C_FG_DIM)
        self.lbl_status.pack(fill='x', padx=8, pady=(0, 4))

        # 右键菜单
        self.ctx_menu = tk.Menu(self.root, tearoff=0, bg=C_BG_DETAIL,
                                fg=C_FG, activebackground=C_BG_HOVER,
                                activeforeground=C_FG, font=self.font_small)
        self.ctx_menu.add_command(label='展开/收起详情', command=self._toggle_expand)
        self.ctx_menu.add_command(label='立即刷新', command=self.refresh)
        self.ctx_menu.add_separator()
        self.ctx_menu.add_command(label='设置…（监视股票 / 切换间隔）',
                                  command=self._open_settings)
        self.ctx_menu.add_command(label='简洁模式', command=self._toggle_compact)
        self.ctx_menu.add_separator()
        self.ctx_menu.add_command(label='退出', command=self._quit)

    def _set_title_segments(self, parts):
        """重建标题分段: [(文本, 前景色[, 背景色])], 每段一个 Label

        第 3 项仅跌停告警闪烁帧用(绿底白字); 缺省用窗口底色。
        """
        for w in self.seg_frame.winfo_children():
            w.destroy()
        for seg in parts:
            text, color = seg[0], seg[1]
            bg = seg[2] if len(seg) > 2 and seg[2] else C_BG
            lbl = tk.Label(self.seg_frame, text=text, font=self.font_main,
                           bg=bg, fg=color)
            lbl.pack(side='left')

    # ============ 跌停告警闪烁 ============
    def _tick_flash(self):
        """每秒心跳: 跌停家数超过阈值时, 每分钟让跌停数字闪一下(FLASH_SEC 秒后复原)"""
        try:
            s = self._last_snapshot
            if not s or self._last_err or not need_flash(s):
                if self._flash_on:
                    self._flash_on = False
                    self._flash_minute = None
                    if s and not self._last_err:
                        self._update_title(s)
                return
            now = time.time()
            minute = int(now // 60)
            if self._flash_minute != minute:      # 进入新的一分钟 -> 触发一次
                self._flash_minute = minute
                self._flash_on = True
                self._flash_until = now + FLASH_SEC
                self._update_title(s)
            elif self._flash_on and now >= self._flash_until:
                self._flash_on = False
                self._update_title(s)
        finally:
            self.root.after(1000, self._tick_flash)

    # ============ 刷新逻辑 ============
    def refresh(self):
        """主刷新: 调 analyze() 取快照 -> 更新 UI; 降频/失败重试与 macOS 版一致"""
        gap = SLOW_REFRESH_SEC if self._fail_streak >= FAIL_STREAK_SLOW else REFRESH_SEC
        now = time.time()
        if now - self._last_try_ts < gap:
            self._schedule_next()
            return
        self._last_try_ts = now

        snap, last_err = None, None
        for attempt in range(REFRESH_RETRY):
            try:
                snap = analyze()
                break
            except Exception as e:
                last_err = '%s: %s' % (type(e).__name__, str(e)[:100])
                if attempt < REFRESH_RETRY - 1:
                    time.sleep(REFRESH_RETRY_DELAY)

        if snap:
            self._fail_streak = 0
            self._last_err = None
            self._last_snapshot = snap
            self._delta_wan = (snap['cur_amount_wan'] - self._prev_amount_wan
                               if self._prev_amount_wan is not None else None)
            self._pace.update(snap)
            self._fetch_stocks(snap)
            self._update_title(snap)
            self._update_detail(snap)
            self._update_status(snap)
            self._prev_amount_wan = snap['cur_amount_wan']
        else:
            self._fail_streak += 1
            self._last_err = last_err
            self._set_fail_ui()

        self._schedule_next()

    def _schedule_next(self):
        if self._last_err:
            self.root.after(1000, self._tick_countdown)
        else:
            self.root.after(REFRESH_SEC * 1000, self.refresh)

    def _tick_countdown(self):
        """失败倒计时: 每秒更新标题剩余秒数"""
        if not self._last_err:
            self.refresh()
            return
        gap = SLOW_REFRESH_SEC if self._fail_streak >= FAIL_STREAK_SLOW else REFRESH_SEC
        remain = max(1, int(gap - (time.time() - self._last_try_ts)) + 1)
        ts = '--:--'
        if self._last_snapshot and self._last_snapshot.get('update_time'):
            hhmmss = self._last_snapshot['update_time']
            ts = '%s:%s' % (hhmmss[8:10], hhmmss[10:12])
        self._set_title_segments([('更新失败 %ds后重试 旧%s' % (remain, ts), C_WARN)])
        self.root.after(1000, self._tick_countdown)

    # ============ 标题更新 ============
    def _index_segments(self, s, price_dec=1, pct_dec=2):
        """沪/创/科 三大指数标题分段: 缩写+点位 后跟涨跌幅(红涨绿跌)

        来自快照 indices(上证指数/创业板指/科创50); 缺失时回退旧字段(仅沪指)。
        """
        idxs = s.get('indices')
        if not idxs:
            idxs = [{'abbr': '沪', 'price': s.get('price'),
                     'change_pct': s.get('change_pct')}]
        segs = []
        for idx in idxs:
            p_txt, c_txt, cval = index_brief(idx, price_dec, pct_dec)
            if segs:
                segs.append((' ', C_FG))
            segs.append((p_txt, C_FG))
            if c_txt:
                color = (C_UP if cval > 0 else C_DN if cval < 0 else C_FG)
                segs.append((c_txt, color))
        return segs

    def _update_title(self, s):
        st = self._current_stock()
        if st is not None:                      # 轮播到个股: 整条换成该股卡片
            self._set_title_segments(self._stock_title_parts(st, s))
            return
        ph = s['phase']
        if ph == 'weekend':
            self._set_title_segments([('沪创科 休市', C_FG_DIM)])
            return
        if ph == 'pre_open':
            self._set_title_segments([('沪创科 未开盘 昨%s'
                                       % fmt_amount(s['prev_amount_wan'], abbr=True),
                                       C_FG_DIM)])
            return

        if self._compact:
            self._update_title_compact(s)
            return

        parts = []
        if ph == 'lunch':
            parts.append(('午休 ', C_FG_DIM))
        parts.extend(self._index_segments(s, price_dec=1, pct_dec=2))
        # 全市场总成交额(标题用缩写口径: 万亿 -> WY, 省宽度)
        parts.append((' %s' % fmt_amount(s['cur_amount_wan'], abbr=True), C_FG))
        if self._delta_wan is not None and self._delta_wan > 0:
            parts.append((' ↑', C_UP))
        elif self._delta_wan is not None and self._delta_wan < 0:
            parts.append((' ↓', C_DN))
        if s['state'] != STATE_NA and s['amount_diff_wan'] is not None:
            # 状态词(分钟节奏口径, vs 1分钟前): 只显示当前节奏, 未启动时兜底 vs 昨日大盘状态
            # 2026-09-11 去掉"放量转缩量/缩量转放量"转换词;
            # 2026-10-19 起连「放量/缩量」汉字也去掉, 语义只靠颜色, 给涨跌停家数腾宽度
            ref = self._pace.pace or s['state']
            color = C_UP if ref == STATE_UP else (C_DN if ref == STATE_DN else C_FG)
            yi = s['amount_diff_wan'] / 10000.0
            sgn = '+' if yi >= 0 else ''
            parts.append((' %s%.0f亿' % (sgn, yi), color))
        if s['forecast_wan']:
            parts.append((' 预%s' % fmt_amount(s['forecast_wan'], abbr=True), C_FG_DIM))
        # 涨跌停家数(东财, 只出数字): 放在"预测成交额"之后, 涨停红/跌停绿
        parts.extend(self._limit_parts(s))
        self._set_title_segments(parts)

    def _stock_title_parts(self, st, s):
        """个股视图标题段(文案由 volume_core.stock_segments 统一生成, 两端一致)"""
        return [(t, SEG_COLORS.get(k, C_FG)) for t, k in stock_segments(
            st,
            show_volume=bool(self._cfg().get('show_volume')),
            compact=self._compact,
            with_today=s['phase'] in ('trading', 'lunch', 'closed'))]

    def _limit_parts(self, s):
        """涨跌停数字段 -> [(文本, 前景色, 背景色)]; 涨停红 / 跌停绿

        跌停家数 > FLASH_DT_THRESHOLD 时, 由 _tick_flash 每分钟置一次 _flash_on,
        把跌停数字画成"绿底白字"闪一下; 缺失为空段(标题自动省略)。
        """
        flash = bool(getattr(self, '_flash_on', False)) and need_flash(s)
        out = []
        for t, k in limit_segments(s):
            if k == LIMIT_SEG_UP:
                out.append((t, C_UP, C_BG))
            elif k == LIMIT_SEG_DN:
                out.append((t, C_FLASH_FG, C_DN) if flash else (t, C_DN, C_BG))
            else:
                out.append((t, C_FG, C_BG))
        return out

    def _update_title_compact(self, s):
        """简洁模式: `沪3722 +0.16% 创2743 -1.07% 科1336 -1.68%↑ +76亿 28/9`"""
        ph = s['phase']
        if ph == 'lunch':
            self._set_title_segments([('沪创科 午休', C_FG_DIM)])
            return
        parts = self._index_segments(s, price_dec=0, pct_dec=2)
        if self._delta_wan is not None and self._delta_wan > 0:
            parts.append(('↑', C_UP))
        elif self._delta_wan is not None and self._delta_wan < 0:
            parts.append(('↓', C_DN))
        if s['state'] != STATE_NA and s['amount_diff_wan'] is not None:
            # 简洁模式同样只显示差额数字(无「放量/缩量」汉字, 颜色表意)
            ref = self._pace.pace or s['state']
            color = C_UP if ref == STATE_UP else (C_DN if ref == STATE_DN else C_FG)
            yi = s['amount_diff_wan'] / 10000.0
            sgn = '+' if yi >= 0 else ''
            parts.append((' %s%.0f亿' % (sgn, yi), color))
        parts.extend(self._limit_parts(s))
        self._set_title_segments(parts)

    # ============ 详情/状态更新 ============
    def _update_detail(self, s):
        if not self._expanded or not s:
            return
        ph_map = {'pre_open': '未开盘', 'trading': '交易中', 'lunch': '午休',
                  'closed': '已收盘', 'weekend': '休市'}
        ph = ph_map.get(s['phase'], s['phase'])
        L = []

        idxs = s.get('indices') or [{'abbr': '沪', 'name': '上证指数',
                                     'price': s['price'],
                                     'change_pct': s['change_pct']}]
        for n, idx in enumerate(idxs):
            L.append('%s %s %.2f (%+.2f%%)%s' % (
                idx.get('abbr', '沪'), idx.get('name', ''),
                idx['price'], idx['change_pct'],
                '  %s' % ph if n == 0 else ''))
        L.append('─' * 34)
        L.append('全市场成交额: %s' % fmt_amount(s['cur_amount_wan']))
        if self._delta_wan is not None and self._delta_wan != 0:
            d = self._delta_wan / 10000.0
            sgn = '+' if d > 0 else ''
            L.append('  较上次刷新(%ds): %s%.2f亿 %s' % (REFRESH_SEC, sgn, d,
                                                      '↑' if d > 0 else '↓'))
        b = s.get('breakdown_wan') or {}
        if b:
            L.append('  沪 %s / 深 %s / 北 %s' % (
                fmt_amount(b.get('sh000001', 0)),
                fmt_amount(b.get('sz399106', 0)),
                fmt_amount(b.get('bj899050', 0))))
        L.append('昨日全天(沪深京): %s' % fmt_amount(s['prev_amount_wan']))
        if s.get('limit_up') is not None or s.get('limit_down') is not None:
            L.append('涨跌停(%s): %s'
                     % (fmt_limit_date(s.get('limit_date')), fmt_limits(s)))
        if s['amount_diff_wan'] is not None:
            yi = s['amount_diff_wan'] / 10000.0
            sgn = '+' if yi >= 0 else ''
            label = s['state'] if s['state'] != STATE_NA else '--'
            L.append('较上一日同时刻: %s%s%.0f亿' % (label, sgn, yi))
        if s['state'] != STATE_NA:
            L.append('量能状态(vs昨日): %s (量比 %sx)' % (
                s['state'], s['vol_ratio'] if s['vol_ratio'] is not None else '-'))
        # 分钟节奏(vs 1分钟前): 转换检测口径
        p = self._pace.pace
        if p or self._pace.last_inc is not None:
            L.append('分钟节奏(vs1分钟前): %s' % (p or '--'))
        if self._pace.last_inc is not None:
            L.append('  本分钟增量: %s / 上一分钟: %s' % (
                fmt_amount(self._pace.last_inc),
                fmt_amount(self._pace.prev_inc) if self._pace.prev_inc is not None else '-'))
        L.append('─' * 34)
        if s['today_cum_hand']:
            L.append('当前累计量(沪深): %s' % fmt_hand(s['today_cum_hand']))
        if s['yest_cum_hand'] is not None:
            L.append('昨日同时刻(沪深): %s' % fmt_hand(s['yest_cum_hand']))
        if s['vol_diff_hand'] is not None:
            sgn = '+' if s['vol_diff_hand'] >= 0 else ''
            L.append('  量差: %s%s' % (sgn, fmt_hand(abs(s['vol_diff_hand']))))
        if s['yest_total_hand']:
            L.append('昨日全天量(沪深): %s' % fmt_hand(s['yest_total_hand']))
        if s['forecast_vol_hand']:
            L.append('预测全天量(沪深): %s' % fmt_hand(s['forecast_vol_hand']))
        if s['forecast_wan']:
            L.append('预测全天成交额: %s' % fmt_amount(s['forecast_wan']))
        L.append('数据时间: %s' % s.get('update_time', '--'))
        # ---- 监视股票(与指数轮播) ----
        stocks = self._cfg().get('stocks') or []
        L.append('─' * 34)
        if stocks:
            L.append('监视股票 %d 只 · 每 %d 秒切换一项'
                     % (len(stocks), self._cycle_sec()))
            for st_cfg in stocks:
                st = (getattr(self, '_stocks', None) or {}).get(st_cfg['code'])
                cur = '▶ ' if getattr(self, '_view', 'index') == st_cfg['code'] else '  '
                if not st:
                    L.append('%s%s %s  取数中…' % (cur, st_cfg['code'],
                                                  st_cfg.get('name') or ''))
                    continue
                L.append(cur + ''.join(t for t, _ in stock_segments(
                    st, show_volume=bool(self._cfg().get('show_volume')),
                    with_today=s['phase'] in ('trading', 'lunch', 'closed'))))
                if st.get('prev_vol_hand') or st.get('amount_diff_wan') is not None:
                    L.append('    较昨日同时刻 %s / 昨量 %s / 预测量 %s / 较上次 %s'
                             % (fmt_delta_wan(st.get('amount_diff_wan')),
                                fmt_hand(st['prev_vol_hand'])
                                if st.get('prev_vol_hand') else '-',
                                fmt_hand(st['forecast_vol_hand'])
                                if st.get('forecast_vol_hand') else '-',
                                fmt_delta_wan(st.get('delta_wan'))))
        else:
            L.append('监视股票: 未添加（右键 → 设置…）')
        if self._pace.trans_log:
            L.append('─' * 34)
            L.append('节奏转换历史(vs1分钟前):')
            for t in self._pace.trans_log:
                L.append('  %s转%s  %s  本分钟%s vs 上分钟%s' % (
                    t['from'], t['to'], t['time'],
                    fmt_amount(t['inc_wan']), fmt_amount(t['prev_inc_wan'])))
        if self._last_err:
            L.append('─' * 34)
            L.append('最近错误: %s' % self._last_err)
        self.detail_text.config(text='\n'.join(L))

    def _update_status(self, s):
        parts = []
        if s.get('phase') in ('trading', 'lunch'):
            parts.append('刷新间隔 %ds' % REFRESH_SEC)
        keys = getattr(self, '_cycle_keys', None) or []
        if len(keys) > 1:
            parts.append('轮播 %d 项/%ds' % (len(keys), self._cycle_sec()))
        if self._compact:
            parts.append('简洁模式')
        parts.append(datetime.now().strftime('%H:%M:%S'))
        self.lbl_status.config(text=' | '.join(parts))

    def _set_fail_ui(self):
        self._set_title_segments([('更新失败, 重试中…', C_WARN)])
        self.lbl_status.config(text='连续失败 %d 次' % self._fail_streak)
        if self._last_snapshot:
            self._update_detail(self._last_snapshot)

    # ============ 交互 ============
    def _on_close_click(self, _):
        self._quit()
        return 'break'   # 阻断 bind_all 的拖拽

    def _toggle_expand(self):
        self._expanded = not self._expanded
        if self._expanded:
            self.detail_frame.pack(fill='both', expand=True, padx=2, pady=(2, 0))
            if self._last_snapshot:
                self._update_detail(self._last_snapshot)
        else:
            self.detail_frame.pack_forget()

    def _toggle_compact(self):
        self._compact = not self._compact
        try:
            if self._compact:
                open(COMPACT_FILE, 'w').close()
            else:
                os.path.exists(COMPACT_FILE) and os.remove(COMPACT_FILE)
        except OSError:
            pass
        if self._last_snapshot:
            self._update_title(self._last_snapshot)
        self._update_status(self._last_snapshot or {})

    def _show_context_menu(self, event):
        # 索引 4 = 简洁模式项(0 展开 1 刷新 2 分隔 3 设置 4 简洁 5 分隔 6 退出)
        self.ctx_menu.entryconfig(4, label='✓ 简洁模式' if self._compact else '简洁模式')
        try:
            self.ctx_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.ctx_menu.grab_release()

    def _open_settings(self):
        """拉起设置窗口(独立进程: tkinter 窗口与悬浮框各跑各的事件循环)"""
        try:
            subprocess.Popen([sys.executable, SETTINGS_WIN],
                             cwd=os.path.dirname(SETTINGS_WIN))
        except Exception as e:
            print('[设置] 打开失败: %s: %s' % (type(e).__name__, e), file=sys.stderr)

    def _on_drag_start(self, event):
        self._drag_data['x'] = event.x_root
        self._drag_data['y'] = event.y_root

    def _on_drag_motion(self, event):
        dx = event.x_root - self._drag_data['x']
        dy = event.y_root - self._drag_data['y']
        if dx == 0 and dy == 0:
            return
        self.root.geometry('+%d+%d' % (self.root.winfo_x() + dx,
                                       self.root.winfo_y() + dy))
        self._drag_data['x'] = event.x_root
        self._drag_data['y'] = event.y_root

    def _restore_position(self):
        try:
            if os.path.exists(POS_FILE):
                with open(POS_FILE, 'r') as f:
                    pos = f.read().strip()
                    if pos:
                        self.root.geometry('+%s' % pos)
                        return
        except Exception:
            pass
        sw = self.root.winfo_screenwidth()
        self.root.geometry('+%d+30' % (sw - 340))

    def _save_position(self):
        try:
            geom = self.root.geometry()
            pos = geom.split('+', 1)[1] if '+' in geom else ''
            if pos:
                with open(POS_FILE, 'w') as f:
                    f.write(pos)
        except Exception:
            pass

    def _quit(self):
        self._save_position()
        self.root.destroy()


if __name__ == '__main__':
    if sys.platform == 'darwin':
        print('macOS 请使用 volume_menu.py (状态栏版)', file=sys.stderr)
        sys.exit(1)
    VolumeFloatWin()
