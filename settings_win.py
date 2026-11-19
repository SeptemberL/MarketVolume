#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
量能监控 - 设置页(macOS / Windows 共用)
==========================================
纯 tkinter, 由主程序以**独立进程**拉起(设置窗口不干扰状态栏/悬浮框的事件循环)。

可设置:
  1. 监视股票列表(可多只): 标题按「指数 -> 股票1 -> 股票2 …」循环播放
  2. 指数与个股视图的切换间隔(秒), 默认 10 秒
  3. 是否在个股标题里附加「成交量」(默认只看成交额)

保存到 .settings.json, 主程序每 1 秒对比 mtime 热加载 —— 保存即生效, 不用重启。
手动打开: python settings_win.py
"""
import os
import sys
import threading
import tkinter as tk
from tkinter import ttk, messagebox

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from volume_core import (load_settings, save_settings, resolve_stock,
                         normalize_code, CYCLE_SEC_DEFAULT,
                         CYCLE_SEC_MIN, CYCLE_SEC_MAX, SETTINGS_FILE)

BG = '#F7F8FA'
FG = '#1F2430'
FG_DIM = '#6B7280'
ACCENT = '#C62828'      # 与行情涨色同一套观感(红)
LINE = '#DDE1E6'


class SettingsWin:
    def __init__(self, root):
        self.root = root
        self.cfg = load_settings()
        self.stocks = [dict(s) for s in self.cfg['stocks']]
        self._busy = False
        self._name_tried = set()     # 已尝试补名的代码(失败不重试, 防死循环)

        root.title('量能监控 · 设置')
        root.configure(bg=BG)
        root.geometry('640x520')
        root.minsize(560, 460)

        self._build()

    # ---------------- UI ----------------
    def _build(self):
        pad = {'padx': 14}
        # 标题
        head = tk.Frame(self.root, bg=BG)
        head.pack(fill='x', pady=(14, 2), **pad)
        tk.Label(head, text='量能监控 设置', bg=BG, fg=FG,
                 font=('PingFang SC', 15, 'bold')).pack(side='left')
        tk.Label(head, text='保存后主程序自动生效，无需重启', bg=BG, fg=FG_DIM,
                 font=('PingFang SC', 10)).pack(side='left', padx=(10, 0), pady=(4, 0))

        # ---- 监视股票 ----
        box = tk.LabelFrame(self.root, text=' 监视股票（与指数一起循环播放） ',
                            bg=BG, fg=FG, font=('PingFang SC', 11),
                            bd=1, relief='solid', highlightthickness=0)
        box.pack(fill='both', expand=True, pady=(10, 6), **pad)

        add_row = tk.Frame(box, bg=BG)
        add_row.pack(fill='x', padx=10, pady=(10, 6))
        tk.Label(add_row, text='代码/名称', bg=BG, fg=FG,
                 font=('PingFang SC', 11)).pack(side='left')
        self.var_input = tk.StringVar()
        ent = tk.Entry(add_row, textvariable=self.var_input, font=('PingFang SC', 11),
                       relief='solid', bd=1, width=22)
        ent.pack(side='left', padx=8, ipady=3)
        ent.bind('<Return>', lambda e: self._add())
        tk.Button(add_row, text='添加', command=self._add, width=6,
                  font=('PingFang SC', 11)).pack(side='left')
        self.lbl_add = tk.Label(add_row, text='例：600000 / 平安银行 / bj430047',
                                bg=BG, fg=FG_DIM, font=('PingFang SC', 10))
        self.lbl_add.pack(side='left', padx=(10, 0))

        list_row = tk.Frame(box, bg=BG)
        list_row.pack(fill='both', expand=True, padx=10, pady=(0, 6))
        style = ttk.Style()
        try:
            style.theme_use('clam')
        except tk.TclError:
            pass
        style.configure('Stock.Treeview', font=('PingFang SC', 12), rowheight=26,
                        background='white', fieldbackground='white', foreground=FG)
        style.configure('Stock.Treeview.Heading', font=('PingFang SC', 11))

        self.tree = ttk.Treeview(list_row, columns=('code', 'name', 'show'),
                                 show='headings', height=7,
                                 style='Stock.Treeview', selectmode='browse')
        for cid, txt, w in (('code', '代码', 120), ('name', '名称', 200),
                            ('show', '轮播顺序', 90)):
            self.tree.heading(cid, text=txt)
            self.tree.column(cid, width=w, anchor='w' if cid != 'show' else 'center')
        sb = ttk.Scrollbar(list_row, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side='left', fill='both', expand=True)
        sb.pack(side='left', fill='y')
        self.tree.bind('<Delete>', lambda e: self._remove())

        btns = tk.Frame(box, bg=BG)
        btns.pack(fill='x', padx=10, pady=(0, 10))
        for txt, cmd in (('删除选中', self._remove), ('上移', lambda: self._move(-1)),
                         ('下移', lambda: self._move(1)), ('清空', self._clear)):
            tk.Button(btns, text=txt, command=cmd, width=8,
                      font=('PingFang SC', 11)).pack(side='left', padx=(0, 6))

        # ---- 轮播设置 ----
        cyc = tk.LabelFrame(self.root, text=' 轮播设置 ', bg=BG, fg=FG,
                            font=('PingFang SC', 11), bd=1, relief='solid')
        cyc.pack(fill='x', pady=(0, 6), **pad)

        row1 = tk.Frame(cyc, bg=BG)
        row1.pack(fill='x', padx=10, pady=(10, 4))
        tk.Label(row1, text='切换间隔', bg=BG, fg=FG,
                 font=('PingFang SC', 11)).pack(side='left')
        self.var_cycle = tk.StringVar(value=str(self.cfg['cycle_sec']))
        tk.Spinbox(row1, from_=CYCLE_SEC_MIN, to=CYCLE_SEC_MAX, width=5,
                   textvariable=self.var_cycle, font=('PingFang SC', 11),
                   relief='solid', bd=1).pack(side='left', padx=8)
        tk.Label(row1, text='秒/项（%d~%d，默认 %d）—— 指数与个股之间循环'
                 % (CYCLE_SEC_MIN, CYCLE_SEC_MAX, CYCLE_SEC_DEFAULT),
                 bg=BG, fg=FG_DIM, font=('PingFang SC', 10)).pack(side='left')

        row2 = tk.Frame(cyc, bg=BG)
        row2.pack(fill='x', padx=10, pady=(2, 10))
        self.var_vol = tk.BooleanVar(value=bool(self.cfg['show_volume']))
        tk.Checkbutton(row2, text='在个股标题里显示成交量（默认只显示成交额）',
                       variable=self.var_vol, bg=BG, fg=FG,
                       activebackground=BG, font=('PingFang SC', 11),
                       anchor='w').pack(side='left')

        # ---- 底部 ----
        foot = tk.Frame(self.root, bg=BG)
        foot.pack(fill='x', pady=(0, 12), **pad)
        self.lbl_status = tk.Label(foot, text='配置：%s' % SETTINGS_FILE,
                                   bg=BG, fg=FG_DIM, font=('PingFang SC', 10),
                                   anchor='w')
        self.lbl_status.pack(side='left', fill='x', expand=True)
        tk.Button(foot, text='保存', command=self._save, width=8,
                  font=('PingFang SC', 11, 'bold'), fg='white', bg=ACCENT,
                  activebackground='#A31F1F', activeforeground='white',
                  relief='flat').pack(side='right', padx=(8, 0))
        tk.Button(foot, text='关闭', command=self.root.destroy, width=8,
                  font=('PingFang SC', 11)).pack(side='right')

        self._refresh_tree()
        self._fill_missing_names()

    # ---------------- 列表 ----------------
    def _refresh_tree(self, select=None):
        self.tree.delete(*self.tree.get_children())
        for i, s in enumerate(self.stocks):
            iid = s['code']
            self.tree.insert('', 'end', iid=iid,
                             values=(s['code'], s.get('name') or '-', '第 %d 项' % (i + 1)))
        if select and self.tree.exists(select):
            self.tree.selection_set(select)
            self.tree.focus(select)
        self.lbl_add.config(text='共 %d 只%s'
                            % (len(self.stocks),
                               '' if self.stocks else '（未添加则只播放指数）'),
                            fg=FG_DIM)

    def _fill_missing_names(self):
        """历史配置里 name 退化成代码的, 后台补一次真名(每只只试一次, 失败就算了)"""
        todo = [s for s in self.stocks
                if (s.get('name') or '') == s['code'] and s['code'] not in self._name_tried]
        if not todo:
            return
        code = todo[0]['code']
        self._name_tried.add(code)
        self._resolve_async(code, lambda c, n: self._apply_names())

    def _apply_names(self):
        self._refresh_tree(select=self._selected())
        self._fill_missing_names()

    def _selected(self):
        sel = self.tree.selection()
        return sel[0] if sel else None

    def _add(self):
        if self._busy:
            return
        kw = self.var_input.get().strip()
        if not kw:
            return
        code = normalize_code(kw)
        if code and any(s['code'] == code for s in self.stocks):
            self.lbl_add.config(text='%s 已在监视列表里了' % code, fg=ACCENT)
            return
        self._busy = True
        self.lbl_add.config(text='正在查询 %s …' % kw, fg=FG_DIM)

        def done(c, n):
            self._busy = False
            if not c:
                self.lbl_add.config(text='没找到「%s」，换个代码或名称试试' % kw, fg=ACCENT)
                return
            if any(s['code'] == c for s in self.stocks):
                self.lbl_add.config(text='%s 已在监视列表里了' % c, fg=ACCENT)
                return
            self.stocks.append({'code': c, 'name': n or c})
            self.var_input.set('')
            self._refresh_tree(select=c)
            self.lbl_add.config(text='已添加 %s %s（记得点保存）' % (c, n or ''), fg=FG_DIM)

        self._resolve_async(kw, done)

    def _resolve_async(self, kw, callback):
        """后台解析代码/名称, 避免网络卡住界面"""
        holder = {}

        def worker():
            try:
                holder['r'] = resolve_stock(kw)
            except Exception:
                holder['r'] = (None, None)

        th = threading.Thread(target=worker, daemon=True)
        th.start()

        def poll():
            if th.is_alive():
                self.root.after(120, poll)
                return
            c, n = holder.get('r') or (None, None)
            if c and n:
                for s in self.stocks:
                    if s['code'] == c:
                        s['name'] = n
            callback(c, n)

        self.root.after(120, poll)

    def _remove(self):
        sel = self._selected()
        if not sel:
            return
        self.stocks = [s for s in self.stocks if s['code'] != sel]
        self._refresh_tree()

    def _move(self, step):
        sel = self._selected()
        if not sel:
            return
        idx = next((i for i, s in enumerate(self.stocks) if s['code'] == sel), None)
        if idx is None:
            return
        j = idx + step
        if not (0 <= j < len(self.stocks)):
            return
        self.stocks[idx], self.stocks[j] = self.stocks[j], self.stocks[idx]
        self._refresh_tree(select=sel)

    def _clear(self):
        if self.stocks and messagebox.askyesno('清空', '确定清空监视股票列表？'):
            self.stocks = []
            self._refresh_tree()

    # ---------------- 保存 ----------------
    def _save(self):
        try:
            cycle = int(float(self.var_cycle.get()))
        except (TypeError, ValueError):
            messagebox.showerror('切换间隔', '请填数字（%d~%d 秒）'
                                 % (CYCLE_SEC_MIN, CYCLE_SEC_MAX))
            return
        if not (CYCLE_SEC_MIN <= cycle <= CYCLE_SEC_MAX):
            messagebox.showerror('切换间隔', '请填 %d~%d 之间的整数秒'
                                 % (CYCLE_SEC_MIN, CYCLE_SEC_MAX))
            return
        self.cfg['cycle_sec'] = cycle
        self.cfg['show_volume'] = bool(self.var_vol.get())
        self.cfg['stocks'] = self.stocks
        try:
            save_settings(self.cfg)
        except Exception as e:
            messagebox.showerror('保存失败', '%s: %s' % (type(e).__name__, e))
            return
        self.lbl_add.config(text='已保存 · %d 只股票 · 每 %d 秒切换一项'
                            % (len(self.stocks), cycle), fg='#2E7D32')


def main():
    try:
        root = tk.Tk()
    except Exception as e:
        # 本机(如 macOS 的 venv)可能缺 Tcl 运行时, 起不了 Tk 窗口
        print('无法创建 Tk 窗口: %s: %s' % (type(e).__name__, e), file=sys.stderr)
        if sys.platform == 'darwin':
            print('macOS 请在状态栏菜单点「设置…」，用的是原生设置窗口。',
                  file=sys.stderr)
        return
    SettingsWin(root)
    # 由状态栏/悬浮框拉起时, 尽量把窗口顶到最前面(否则可能被压在别的窗口下面)
    try:
        root.lift()
        root.attributes('-topmost', True)
        root.after(800, lambda: root.attributes('-topmost', False))
        root.focus_force()
    except tk.TclError:
        pass
    root.mainloop()


if __name__ == '__main__':
    main()
