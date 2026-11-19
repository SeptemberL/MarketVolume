#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
量能监控 - 设置窗口(macOS 原生 AppKit 版)
==========================================
为什么不用 tkinter: 本机 venv 里的 Python 自带 _tkinter 但缺 Tcl 运行时
(`Cannot find a usable init.tcl`), 系统/Homebrew 的 Python 又没编 _tkinter,
所以 macOS 走原生 AppKit; Windows 仍用 tkinter 版 settings_win.py。

窗口由 volume_menu.py 在**同一进程内**拉起:
  - 保存后立刻热加载并重绘标题, 不用等 10 秒刷新, 也不用重启
  - 关闭窗口不退出进程, 再次点"设置…"复用同一个窗口

可设置项:
  1. 监视股票列表(可多只, 可调顺序): 标题在「指数 -> 股票1 -> 股票2 …」间循环
  2. 切换间隔(秒), 默认 10
  3. 是否在个股标题里附加「成交量」
"""
import sys
import threading

import objc
from Foundation import NSObject
from AppKit import (NSAlert, NSApplication, NSBackingStoreBuffered, NSBezelStyleRounded,
                    NSButton, NSButtonTypeSwitch, NSColor, NSFont, NSMakeRect,
                    NSScrollView, NSTextField, NSView, NSWindow, NSBezelBorder,
                    NSFloatingWindowLevel, NSNormalWindowLevel,
                    NSWindowStyleMaskClosable, NSWindowStyleMaskTitled,
                    NSWindowStyleMaskMiniaturizable)
from PyObjCTools import AppHelper

import volume_core as vc

W, H = 560, 520
ROW_H = 26

# 与主界面同一套观感(原生控件, 只在这里统一字号/颜色)
F_TITLE = 15
F_BODY = 13
F_SMALL = 11


def _label(text, rect, size=F_BODY, bold=False, dim=False, align=None):
    """只读文本标签(不画边框/背景)"""
    lbl = NSTextField.alloc().initWithFrame_(rect)
    lbl.setStringValue_(text)
    lbl.setBezeled_(False)
    lbl.setDrawsBackground_(False)
    lbl.setEditable_(False)
    lbl.setSelectable_(False)
    lbl.setFont_(NSFont.boldSystemFontOfSize_(size) if bold
                 else NSFont.systemFontOfSize_(size))
    if dim:
        lbl.setTextColor_(NSColor.secondaryLabelColor())
    if align is not None:
        lbl.setAlignment_(align)
    return lbl


def _button(title, rect, target, action, small=False):
    btn = NSButton.alloc().initWithFrame_(rect)
    btn.setTitle_(title)
    btn.setBezelStyle_(NSBezelStyleRounded)
    btn.setTarget_(target)
    btn.setAction_(action)
    if small:
        btn.setFont_(NSFont.systemFontOfSize_(F_SMALL))
    return btn


class SettingsWindowController(NSObject):
    """设置窗口控制器(selector 用 PyObjC 的 xxx_ 命名)"""

    def initWithApp_(self, app):
        self = objc.super(SettingsWindowController, self).init()
        if self is None:
            return None
        self.app = app
        cfg = getattr(app, '_settings', None) or vc.load_settings()
        self.stocks = [dict(s) for s in cfg.get('stocks') or []]
        self.cycle_sec = int(cfg.get('cycle_sec') or vc.CYCLE_SEC_DEFAULT)
        self.show_volume = bool(cfg.get('show_volume'))
        self._row_views = []
        self._policy_changed = False
        self._build_window()
        return self

    # ---------------- 窗口与控件 ----------------
    @objc.python_method
    def _build_window(self):
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, W, H),
            NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
            | NSWindowStyleMaskMiniaturizable,
            NSBackingStoreBuffered, False)
        self.window.setTitle_('量能监控 · 设置')
        self.window.setReleasedWhenClosed_(False)
        self.window.center()
        # 浮在所有普通窗口之上: 状态栏 App 常常激活不了, 靠窗口层级兜底(关掉即消失)
        self.window.setLevel_(NSFloatingWindowLevel)
        self.window.setDelegate_(self)

        view = self.window.contentView()

        view.addSubview_(_label('量能监控 设置', NSMakeRect(18, H - 48, 200, 22),
                                F_TITLE, bold=True))
        view.addSubview_(_label('保存后立即生效，无需重启',
                                NSMakeRect(150, H - 44, 260, 18), F_SMALL, dim=True))

        # ---- 添加股票 ----
        self.f_input = NSTextField.alloc().initWithFrame_(
            NSMakeRect(18, H - 92, 210, 26))
        self.f_input.setFont_(NSFont.systemFontOfSize_(F_BODY))
        self.f_input.setPlaceholderString_('代码或名称，如 600000')
        view.addSubview_(self.f_input)
        view.addSubview_(_button('添加', NSMakeRect(234, H - 92, 64, 26), self,
                                 b'addStock:'))
        self.lbl_status = _label('', NSMakeRect(306, H - 90, 240, 22), F_SMALL, dim=True)
        view.addSubview_(self.lbl_status)

        # ---- 监视股票列表 ----
        view.addSubview_(_label('监视股票（与指数一起循环播放，可调顺序）',
                                NSMakeRect(18, H - 122, 400, 18), F_SMALL, dim=True))
        list_h = 210
        self.scroll = NSScrollView.alloc().initWithFrame_(
            NSMakeRect(16, 160, W - 32, list_h))
        self.scroll.setBorderType_(NSBezelBorder)
        self.scroll.setHasVerticalScroller_(True)
        self.scroll.setDrawsBackground_(True)
        self.doc = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, W - 34, list_h))
        self.scroll.setDocumentView_(self.doc)
        view.addSubview_(self.scroll)

        # ---- 轮播设置 ----
        view.addSubview_(_label('轮播设置', NSMakeRect(18, 128, 100, 18), F_SMALL,
                                bold=True))
        view.addSubview_(_label('切换间隔', NSMakeRect(18, 96, 70, 20), F_BODY))
        self.f_cycle = NSTextField.alloc().initWithFrame_(NSMakeRect(88, 94, 56, 24))
        self.f_cycle.setFont_(NSFont.systemFontOfSize_(F_BODY))
        self.f_cycle.setStringValue_(str(self.cycle_sec))
        self.f_cycle.setAlignment_(2)     # 居中
        view.addSubview_(self.f_cycle)
        view.addSubview_(_label('秒/项（%d~%d，默认 %d）—— 指数与个股之间循环'
                                % (vc.CYCLE_SEC_MIN, vc.CYCLE_SEC_MAX,
                                   vc.CYCLE_SEC_DEFAULT),
                                NSMakeRect(152, 96, 380, 20), F_SMALL, dim=True))

        self.cb_vol = NSButton.alloc().initWithFrame_(NSMakeRect(18, 64, 360, 22))
        self.cb_vol.setButtonType_(NSButtonTypeSwitch)
        self.cb_vol.setTitle_('在个股标题里显示成交量（默认只显示成交额）')
        self.cb_vol.setFont_(NSFont.systemFontOfSize_(F_BODY))
        self.cb_vol.setState_(1 if self.show_volume else 0)
        self.cb_vol.setTarget_(self)
        self.cb_vol.setAction_(b'toggleVolume:')
        view.addSubview_(self.cb_vol)

        # ---- 底部按钮 ----
        view.addSubview_(_button('保存', NSMakeRect(W - 160, 18, 66, 32), self,
                                 b'save:'))
        view.addSubview_(_button('关闭', NSMakeRect(W - 88, 18, 66, 32), self,
                                 b'close:'))
        self.lbl_hint = _label('配置写入本目录 .settings.json（不进 git）',
                               NSMakeRect(18, 22, W - 190, 18), F_SMALL, dim=True)
        view.addSubview_(self.lbl_hint)

        self._render_rows()

    # ---------------- 列表渲染 ----------------
    @objc.python_method
    def _clear_rows(self):
        for v in self._row_views:
            v.removeFromSuperview()
        self._row_views = []

    @objc.python_method
    def _render_rows(self):
        self._clear_rows()
        n = len(self.stocks)
        doc_h = max(ROW_H * max(n, 1) + 8, self.scroll.frame().size.height)
        self.doc.setFrame_(NSMakeRect(0, 0, W - 34, doc_h))
        if not n:
            empty = _label('还没有监视股票 —— 上面输入代码或名称后点「添加」',
                           NSMakeRect(10, doc_h - 30, W - 60, 20), F_SMALL, dim=True)
            self.doc.addSubview_(empty)
            self._row_views.append(empty)
            return
        for i, s in enumerate(self.stocks):
            y = doc_h - (i + 1) * ROW_H
            txt = '%d.  %s   %s' % (i + 1, s['code'], s.get('name') or '')
            lbl = _label(txt, NSMakeRect(10, y + 4, 300, 18), F_BODY)
            self.doc.addSubview_(lbl)
            self._row_views.append(lbl)
            for j, (title, sel) in enumerate((('↑', b'moveUp:'), ('↓', b'moveDown:'),
                                              ('×', b'removeStock:'))):
                btn = _button(title, NSMakeRect(W - 132 + j * 34, y + 2, 30, 22),
                              self, sel, small=True)
                btn.setTag_(i)
                self.doc.addSubview_(btn)
                self._row_views.append(btn)

    # ---------------- 动作 ----------------
    def addStock_(self, _sender):
        kw = (self.f_input.stringValue() or '').strip()
        if not kw:
            return
        code = vc.normalize_code(kw)
        if code and any(s['code'] == code for s in self.stocks):
            self.lbl_status.setStringValue_('%s 已在列表里了' % code)
            return
        self.lbl_status.setStringValue_('正在查询 %s …' % kw)
        th = threading.Thread(target=self._resolve_worker, args=(kw,), daemon=True)
        th.start()

    @objc.python_method
    def _resolve_worker(self, kw):
        try:
            code, name = vc.resolve_stock(kw)
        except Exception:
            code, name = None, None
        AppHelper.callAfter(self._resolve_done, kw, code, name)

    @objc.python_method
    def _resolve_done(self, kw, code, name):
        if not code:
            self.lbl_status.setStringValue_('没找到「%s」，换个代码或名称' % kw)
            return
        if any(s['code'] == code for s in self.stocks):
            self.lbl_status.setStringValue_('%s 已在列表里了' % code)
            return
        self.stocks.append({'code': code, 'name': name or code})
        self.f_input.setStringValue_('')
        self.lbl_status.setStringValue_('已添加 %s %s（记得点保存）' % (code, name or ''))
        self._render_rows()

    def removeStock_(self, sender):
        i = sender.tag()
        if 0 <= i < len(self.stocks):
            code = self.stocks[i]['code']
            del self.stocks[i]
            self.lbl_status.setStringValue_('已移除 %s（记得点保存）' % code)
            self._render_rows()

    @objc.python_method
    def _move_row(self, i, step):
        j = i + step
        if 0 <= i < len(self.stocks) and 0 <= j < len(self.stocks):
            self.stocks[i], self.stocks[j] = self.stocks[j], self.stocks[i]
            self._render_rows()

    def moveUp_(self, sender):
        self._move_row(sender.tag(), -1)

    def moveDown_(self, sender):
        self._move_row(sender.tag(), 1)

    def toggleVolume_(self, sender):
        self.show_volume = bool(sender.state())

    @objc.python_method
    def _alert(self, title, text):
        a = NSAlert.alloc().init()
        a.setMessageText_(title)
        a.setInformativeText_(text)
        a.addButtonWithTitle_('好')
        a.runModal()

    def save_(self, _sender):
        raw = (self.f_cycle.stringValue() or '').strip()
        try:
            cycle = int(float(raw))
        except (TypeError, ValueError):
            self._alert('切换间隔', '请填数字（%d~%d 秒）'
                        % (vc.CYCLE_SEC_MIN, vc.CYCLE_SEC_MAX))
            return
        if not (vc.CYCLE_SEC_MIN <= cycle <= vc.CYCLE_SEC_MAX):
            self._alert('切换间隔', '请填 %d~%d 之间的整数秒'
                        % (vc.CYCLE_SEC_MIN, vc.CYCLE_SEC_MAX))
            return
        cfg = {'cycle_sec': cycle, 'show_volume': bool(self.cb_vol.state()),
               'stocks': self.stocks}
        try:
            vc.save_settings(cfg)
        except Exception as e:
            self._alert('保存失败', '%s: %s' % (type(e).__name__, e))
            return
        self.cycle_sec = cycle
        msg = '已保存 · %d 只股票 · 每 %d 秒切换一项' % (len(self.stocks), cycle)
        self.lbl_status.setStringValue_(msg)
        self.lbl_hint.setStringValue_(msg)
        # 立即生效: 让主程序重读配置并马上抓一轮个股行情
        try:
            self.app.apply_settings_now()
        except Exception:
            pass

    def close_(self, _sender):
        self.window.close()

    # ---------------- 窗口生命周期 ----------------
    def windowWillClose_(self, _note):
        app = getattr(self, 'app', None)
        if app is not None:
            app._settings_win = None     # 下次点"设置…"重新建窗
        self.window.setLevel_(NSNormalWindowLevel)
        if getattr(self, '_policy_changed', False):
            # 关窗即退回纯状态栏模式(去掉 Dock 图标), 与启动时一致
            NSApplication.sharedApplication().setActivationPolicy_(2)
            self._policy_changed = False

    def show(self):
        """把窗口顶到最前面

        ⚠️ 状态栏 App 的激活策略是 Accessory(2): 窗口能"显示"但**抢不到焦点、会被压在
        别的窗口后面**, 用户看起来就是"点了设置没反应"。所以临时切成 Regular(0) 让它真激活,
        窗口关闭时再切回 Accessory(见 windowWillClose_)。
        """
        nsapp = NSApplication.sharedApplication()
        if nsapp.activationPolicy() != 0:
            nsapp.setActivationPolicy_(0)      # Regular: 短暂出现 Dock 图标, 换取能激活
            self._policy_changed = True
        nsapp.activateIgnoringOtherApps_(True)
        self.window.makeKeyAndOrderFront_(None)
        self.window.orderFrontRegardless()     # 双保险: 即使未激活也强制排到最前
        self.window.makeFirstResponder_(self.f_input)   # 打开即可直接输入代码
        # 留一行诊断: 万一还是"点了没反应", 看日志就知道窗口到底有没有出来
        print('[设置] 窗口 visible=%s level=%s policy=%s active=%s'
              % (self.window.isVisible(), self.window.level(),
                 nsapp.activationPolicy(), nsapp.isActive()),
              file=sys.stderr)


def show_settings(app):
    """由 volume_menu 调用: 已有窗口就顶到前面, 否则新建

    主程序里用 self._settings_win 持有强引用(PyObjC 对象一旦被 GC 窗口就空了)
    """
    win = getattr(app, '_settings_win', None)
    if win is None:
        win = SettingsWindowController.alloc().initWithApp_(app)
        app._settings_win = win
    win.show()
    return win
