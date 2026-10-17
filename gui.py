# -*- coding: utf-8 -*-
"""
B站用户评论爬虫 - 图形界面（tkinter，无需额外依赖）

双击 run_gui.bat 或运行：
    python gui.py

界面收集参数后，在后台线程调用 bili_comments.crawl()，实时显示日志，
完成后显示结果数量、CSV/JSON 路径，并可一键打开输出文件夹。
"""

import os
import queue
import sys
import threading
import types
import webbrowser

import tkinter as tk
from tkinter import ttk, messagebox
from tkinter.scrolledtext import ScrolledText

import bili_comments as bc


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output")


def _int(value, default):
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        return default


def _float(value, default):
    try:
        return float(str(value).strip())
    except (ValueError, TypeError):
        return default


class App:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.running = False
        self.last_result = None

        root.title("B站用户评论爬虫")
        root.geometry("760x720")
        root.minsize(680, 620)

        self._build_widgets()
        self.root.after(100, self._poll)

    # ------------------------------------------------------------------ UI
    def _build_widgets(self):
        pad = dict(padx=6, pady=3)
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill="both", expand=True)

        # ---- 目标用户 ----
        user = ttk.LabelFrame(main, text="目标用户（昵称 / UID / 空间链接 三选一）", padding=8)
        user.pack(fill="x")
        user.columnconfigure(1, weight=1)
        ttk.Label(user, text="昵称").grid(row=0, column=0, sticky="e", **pad)
        self.nickname = ttk.Entry(user)
        self.nickname.grid(row=0, column=1, sticky="we", **pad)
        ttk.Label(user, text="UID").grid(row=1, column=0, sticky="e", **pad)
        self.uid = ttk.Entry(user)
        self.uid.grid(row=1, column=1, sticky="we", **pad)
        ttk.Label(user, text="空间链接").grid(row=2, column=0, sticky="e", **pad)
        self.url = ttk.Entry(user)
        self.url.grid(row=2, column=1, sticky="we", **pad)

        # ---- 时间范围 ----
        tm = ttk.LabelFrame(main, text="时间范围（留空 = 不限）", padding=8)
        tm.pack(fill="x", pady=(8, 0))
        tm.columnconfigure(1, weight=1)
        ttk.Label(tm, text="开始时间").grid(row=0, column=0, sticky="e", **pad)
        self.start = ttk.Entry(tm)
        self.start.grid(row=0, column=1, sticky="we", **pad)
        ttk.Label(tm, text="例：2026-01-01 或 2026-01-01 00:00:00").grid(row=0, column=2, sticky="w", **pad)
        ttk.Label(tm, text="结束时间").grid(row=1, column=0, sticky="e", **pad)
        self.end = ttk.Entry(tm)
        self.end.grid(row=1, column=1, sticky="we", **pad)
        ttk.Label(tm, text="只写日期 = 当天 23:59:59").grid(row=1, column=2, sticky="w", **pad)
        ttk.Label(tm, text="最近 N 天").grid(row=2, column=0, sticky="e", **pad)
        self.days = ttk.Entry(tm)
        self.days.grid(row=2, column=1, sticky="we", **pad)
        ttk.Label(tm, text="填了此项会忽略上面的起止时间").grid(row=2, column=2, sticky="w", **pad)

        # ---- 数据源 ----
        src = ttk.LabelFrame(main, text="数据源（候选视频池）", padding=8)
        src.pack(fill="x", pady=(8, 0))
        self.var_own = tk.BooleanVar(value=True)
        self.var_fav = tk.BooleanVar(value=True)
        self.var_dyn = tk.BooleanVar(value=True)
        self.var_expand = tk.BooleanVar(value=False)
        ttk.Checkbutton(src, text="他的投稿", variable=self.var_own).pack(side="left", padx=8)
        ttk.Checkbutton(src, text="公开收藏", variable=self.var_fav).pack(side="left", padx=8)
        ttk.Checkbutton(src, text="转发过的", variable=self.var_dyn).pack(side="left", padx=8)
        ttk.Checkbutton(src, text="展开全部楼中楼(更全但慢)", variable=self.var_expand).pack(side="left", padx=8)

        # ---- 附加视频 ----
        extra = ttk.LabelFrame(main, text="附加指定视频（可选，多个用逗号分隔）", padding=8)
        extra.pack(fill="x", pady=(8, 0))
        extra.columnconfigure(0, weight=1)
        self.bvid = ttk.Entry(extra)
        self.bvid.grid(row=0, column=0, sticky="we")
        ttk.Label(extra, text="BV号 / av号 / 视频链接").grid(row=0, column=1, padx=6)

        # ---- 高级选项 ----
        adv = ttk.LabelFrame(main, text="高级选项", padding=8)
        adv.pack(fill="x", pady=(8, 0))
        opts = [
            ("并发线程", "workers", "4"),
            ("速率(请求/秒)", "rate", "8"),
            ("冷却(秒)", "cooldown", "8"),
            ("请求间隔(秒)", "sleep", "0.05"),
            ("每视频评论页数", "max_comment_pages", "20"),
            ("投稿上限", "max_videos", "30"),
            ("收藏上限", "max_fav_videos", "100"),
            ("转发上限", "max_dynamic", "50"),
            ("楼中楼楼层上限", "max_sub_threads", "30"),
            ("楼中楼每层页数", "max_sub_pages", "3"),
        ]
        self.opt_vars = {}
        for i, (label, key, default) in enumerate(opts):
            r, c = divmod(i, 4)
            ttk.Label(adv, text=label).grid(row=r, column=c * 2, sticky="e", padx=(4, 2), pady=2)
            v = ttk.Entry(adv, width=7)
            v.insert(0, default)
            v.grid(row=r, column=c * 2 + 1, sticky="w", padx=(0, 8), pady=2)
            self.opt_vars[key] = v

        ttk.Label(adv, text="Cookie(可选)").grid(row=3, column=0, sticky="e", padx=(4, 2), pady=2)
        self.cookie = ttk.Entry(adv)
        self.cookie.grid(row=3, column=1, columnspan=7, sticky="we", padx=(0, 8), pady=2)

        # ---- 按钮 ----
        btns = ttk.Frame(main)
        btns.pack(fill="x", pady=(10, 0))
        self.run_btn = ttk.Button(btns, text="开始爬取", command=self.start_crawl)
        self.run_btn.pack(side="left")
        self.open_btn = ttk.Button(btns, text="打开输出文件夹", command=self.open_output, state="disabled")
        self.open_btn.pack(side="left", padx=8)

        # ---- 日志 ----
        logf = ttk.LabelFrame(main, text="运行日志", padding=6)
        logf.pack(fill="both", expand=True, pady=(10, 0))
        self.logbox = ScrolledText(logf, height=14, state="disabled", font=("Consolas", 9))
        self.logbox.pack(fill="both", expand=True)

        # ---- 结果 ----
        self.result_var = tk.StringVar(value="就绪：请填写昵称（或 UID/链接），然后点「开始爬取」。")
        ttk.Label(main, textvariable=self.result_var, foreground="#0a6").pack(anchor="w", pady=(6, 0))

    # ------------------------------------------------------------------ 逻辑
    def _append_log(self, text):
        self.logbox.configure(state="normal")
        self.logbox.insert("end", text + "\n")
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

    def start_crawl(self):
        if self.running:
            return
        opts = self._collect_opts()
        if opts is None:
            return

        bc.set_log_hook(lambda m: self.q.put(("log", m)))
        self.running = True
        self.run_btn.configure(state="disabled")
        self.result_var.set("运行中……")
        self.logbox.configure(state="normal")
        self.logbox.delete("1.0", "end")
        self.logbox.configure(state="disabled")

        t = threading.Thread(target=self._worker, args=(opts,), daemon=True)
        t.start()

    def _worker(self, opts):
        try:
            result = bc.crawl(opts)
            self.q.put(("result", result))
        except Exception as e:
            self.q.put(("result", {"ok": False, "error": repr(e), "records": [],
                                   "csv_path": "", "json_path": "", "uname": "", "mid": ""}))

    def _collect_opts(self):
        nickname = self.nickname.get().strip()
        uid = self.uid.get().strip()
        url = self.url.get().strip()
        if not nickname and not uid and not url:
            messagebox.showwarning("提示", "请填写昵称、UID 或空间链接中的至少一项。")
            return None

        days = self.days.get().strip()
        start = self.start.get().strip()
        end = self.end.get().strip()

        bvid = self.bvid.get().strip()
        bvid_list = [x.strip() for x in bvid.split(",") if x.strip()] if bvid else []

        opts = types.SimpleNamespace(
            nickname=nickname or None,
            uid=uid or None,
            url=url or None,
            bvid=bvid_list,
            bvid_file=None,
            start=start or None,
            end=end or None,
            days=_int(days, None) if days else None,
            no_own_videos=not self.var_own.get(),
            no_fav=not self.var_fav.get(),
            no_dynamic=not self.var_dyn.get(),
            max_videos=_int(self.opt_vars["max_videos"].get(), 30),
            max_fav_videos=_int(self.opt_vars["max_fav_videos"].get(), 100),
            max_dynamic=_int(self.opt_vars["max_dynamic"].get(), 50),
            max_comment_pages=_int(self.opt_vars["max_comment_pages"].get(), 20),
            max_sub_threads=_int(self.opt_vars["max_sub_threads"].get(), 30),
            max_sub_pages=_int(self.opt_vars["max_sub_pages"].get(), 3),
            workers=_int(self.opt_vars["workers"].get(), 4),
            rate=_float(self.opt_vars["rate"].get(), 8.0),
            cooldown=_float(self.opt_vars["cooldown"].get(), 8.0),
            sleep=_float(self.opt_vars["sleep"].get(), 0.05),
            cookie=self.cookie.get().strip(),
            expand_replies=self.var_expand.get(),
            no_early_stop=False,
            out=None,
            interactive=False,
        )
        return opts

    def _poll(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "result":
                    self._on_result(payload)
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _on_result(self, result):
        self.running = False
        self.run_btn.configure(state="normal")
        if result.get("ok"):
            n = len(result.get("records") or [])
            self.last_result = result
            self.result_var.set(f"完成：找到 {n} 条评论。CSV / JSON 已保存到 output 目录。")
            self.open_btn.configure(state="normal")
        else:
            self.result_var.set("失败：" + (result.get("error") or "见日志"))

    def open_output(self):
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        try:
            os.startfile(OUTPUT_DIR)  # Windows
        except AttributeError:
            webbrowser.open("file://" + OUTPUT_DIR)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
