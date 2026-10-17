#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
B站「用户评论」爬虫  (bili_comments.py)

输入一个用户的昵称 -> 自动搜索出他的 UID -> 抓取视频评论区 -> 筛选出「他本人发的评论」，
可按时间段过滤，最终导出为 CSV（Excel 可直接打开）和 JSON。

用法：
    python bili_comments.py                       # 交互式：运行后输入昵称
    python bili_comments.py 昵称                  # 直接按昵称搜索
    python bili_comments.py --uid 12345           # 已知 UID 直接抓
    python bili_comments.py --url https://space.bilibili.com/12345

扫描范围（重要）：
    默认自动扫描三类视频的评论区，筛出该用户发的评论：
      1) 他自己的投稿视频
      2) 他的公开收藏视频
      3) 他转发过的视频（动态里的转发）
    另外还可以用 --bvid / --bvid-file 指定任意视频补充扫描。
    （点赞过的视频是私密的，B 站接口拿不到，无法纳入。）

    关闭某类来源：
        --no-own-videos   不扫他自己的投稿
        --no-fav          不扫公开收藏
        --no-dynamic      不扫转发

时间段过滤：
    python bili_comments.py 昵称 --start 2026-01-01 --end 2026-12-31
    python bili_comments.py 昵称 --start "2026-01-01 00:00:00" --end "2026-06-30 23:59:59"
    python bili_comments.py 昵称 --days 30         # 只抓最近 30 天

常用可选参数：
    --bvid BV/av/链接      额外扫描的视频，可重复传入或逗号分隔
    --bvid-file 文件       每行一个视频，批量扫描
    --no-own-videos        不扫该用户自己的投稿
    --no-fav               不扫该用户的公开收藏
    --no-dynamic           不扫该用户转发过的视频
    --max-videos N         最多扫描多少个投稿视频（默认 30）
    --max-fav-videos N     公开收藏最多扫多少个视频（默认 100）
    --max-dynamic N        转发动态最多扫多少个视频（默认 50）
    --max-comment-pages N  每个视频最多翻多少页评论，每页约 20 条（默认 20）
    --max-sub-threads N    每个视频最多展开多少个楼中楼楼层（默认 100）
    --max-sub-pages N      每个楼层楼中楼最多翻几页（默认 5，每页 20 条）
    --workers N            并发线程数（默认 3；被限流时改成 1）
    --rate R               全局限速：每秒最多请求数（默认 1.5；有 Cookie 可调到 5+）
    --cooldown S           风控命中后全局冷却秒数（默认 20）
    --sleep S              每次额外请求间隔秒数（默认 0.3）
    --cookie STR           可选，登录 Cookie，例如 "SESSDATA=xxxx"
    --early-stop           开启按时间提前停止（更快，但可能漏掉老楼层下的新楼中楼）
    --out DIR              结果输出目录（默认 ./output）

说明（重要）：
    B 站没有公开的“按用户返回其全部评论”的接口，也没有“评论”的全局搜索索引，
    所以无法真正遍历全站所有视频。本脚本退而求其次，用该用户主页能公开拿到的
    「投稿 + 公开收藏 + 转发」作为候选视频池，再按 UID 筛出他发的评论，
    并按时间段过滤。

风控说明：
    B 站接口有风控和限流。本脚本会自动调用 B 站指纹接口生成 buvid3/buvid4 反爬
    Cookie，并在遇到 -412 / -799 / -352 / HTTP 412 时自动退避重试、刷新 Cookie。
    若仍被拦截：调大 --sleep、提供 --cookie，或过几分钟再试。

依赖：仅使用 Python 标准库（urllib），无需安装第三方包。
"""

import argparse
import concurrent.futures
import csv
import hashlib
import http.cookiejar
import json
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime

# ---------------------------------------------------------------------------
# 基础配置
# ---------------------------------------------------------------------------
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
REFERER = "https://www.bilibili.com/"

SEARCH_URL = "https://api.bilibili.com/x/web-interface/search/type"
VIDEO_URL_OLD = "https://api.bilibili.com/x/space/arc/search"          # 旧投稿接口（免 WBI）
VIDEO_URL_WBI = "https://api.bilibili.com/x/space/wbi/arc/search"      # 新投稿接口（需 WBI）
COMMENT_URL = "https://api.bilibili.com/x/v2/reply/main"               # 评论列表
VIEW_URL = "https://api.bilibili.com/x/web-interface/view"             # BV -> aid
NAV_URL = "https://api.bilibili.com/x/web-interface/nav"               # 取 WBI 密钥
SPI_URL = "https://api.bilibili.com/x/frontend/finger/spi"             # 取 buvid3/buvid4 指纹
FAV_FOLDER_URL = "https://api.bilibili.com/x/v3/fav/folder/created/list-all"  # 收藏夹列表
FAV_RESOURCE_URL = "https://api.bilibili.com/x/v3/fav/resource/list"         # 收藏夹内容
DYNAMIC_URL = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"  # 动态(含转发)

COMMENT_MODE = 2   # 2 = 按时间倒序（最新在前），便于时间段过滤与提前停止

# 这些返回码表示被风控/限流，应退避重试
RISK_CODES = {-412, -799, -352, -504, -509}
RISK_HTTP = {403, 412, 429}

MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61,
    26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
    20, 34, 44, 52,
]


_LOG_HOOK = None   # 供 GUI 挂载日志回调

# 全局限速与冷却：所有 BiliClient 实例（含多线程）共享同一速率上限和冷却窗口，
# 避免并发线程各自为政把 B 站打到风控。
_RATE_LOCK = threading.Lock()
_NEXT_REQ_AT = 0.0      # 下一次允许发起请求的时间戳（令牌桶）
_COOLDOWN_UNTIL = 0.0   # 风控后，所有请求暂停到这个时间点


def set_log_hook(fn):
    """GUI 可设置一个回调 fn(msg) 来接收进度日志（同时仍打印到 stdout）。"""
    global _LOG_HOOK
    _LOG_HOOK = fn


def log(msg: str = ""):
    if _LOG_HOOK is not None:
        try:
            _LOG_HOOK(msg)
        except Exception:
            pass
    try:
        print(msg, flush=True)
    except Exception:
        pass


def ensure_console_utf8():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def get_mixin_key(orig: str) -> str:
    return "".join(orig[i] for i in MIXIN_KEY_ENC_TAB)[:32]


def enc_wbi(params: dict, img_key: str, sub_key: str) -> dict:
    mixin_key = get_mixin_key(img_key + sub_key)
    params = {k: re.sub(r"[!'()*]", "", str(v)) for k, v in params.items()}
    params["wts"] = int(time.time())
    params = dict(sorted(params.items()))
    query = urllib.parse.urlencode(params)
    params["w_rid"] = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
    return params


def parse_time(s: str, is_end: bool = False):
    """把用户输入的时间解析成 unix 时间戳。

    is_end=True 时，只给日期的输入会取当天 23:59:59。
    支持：2026-01-01、2026/1/1、20260101、2026-01-01 12:00:00，以及 10 位以上 unix 时间戳。
    """
    s = (s or "").strip()
    if not s:
        return None
    date_fmts = ["%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"]
    time_fmts = [
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M",
    ]
    if ":" in s:
        for f in time_fmts:
            try:
                return int(datetime.strptime(s, f).timestamp())
            except ValueError:
                continue
        raise ValueError(f"无法解析时间：{s}")
    for f in date_fmts:
        try:
            dt = datetime.strptime(s, f)
            if is_end:
                dt = dt.replace(hour=23, minute=59, second=59)
            return int(dt.timestamp())
        except ValueError:
            continue
    # 纯数字且长度 >= 10 才当作 unix 时间戳（避免把 20260101 这类日期误判）
    if s.isdigit() and len(s) >= 10:
        return int(s)
    raise ValueError(f"无法解析时间：{s}")


class BiliClient:
    """封装 HTTP 会话、反爬 Cookie、WBI 密钥、各接口调用。"""

    def __init__(self, cookie: str = "", sleep: float = 0.05, max_retries: int = 3,
                 rate: float = 8.0, cooldown: float = 8.0, light: bool = False):
        self.sleep = sleep
        self.max_retries = max_retries
        self.rate = rate          # 全局限速：每秒最多请求数
        self.cooldown = cooldown  # 风控后全局冷却秒数
        self.cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cj)
        )
        self.img_key = ""
        self.sub_key = ""
        self._bootstrap_fingerprint()
        if cookie:
            self._inject_cookie(cookie)
        if not light:
            # 轻量模式（评论线程用）：跳过首页播种与 WBI 密钥，省 2 个 bootstrap 请求
            self._seed_cookie()
            self._load_wbi_keys()

    # ---- Cookie ----
    def _set_cookie(self, name, value, domain=".bilibili.com"):
        try:
            self.cj.set_cookie(
                http.cookiejar.Cookie(
                    version=0, name=name, value=value, port=None, port_specified=False,
                    domain=domain, domain_specified=True, domain_initial_dot=True,
                    path="/", path_specified=True, secure=False, expires=None,
                    discard=False, comment=None, comment_url=None, rest={}, rfc2109=False,
                )
            )
        except Exception:
            pass

    def _seed_cookie(self):
        try:
            self._get("https://www.bilibili.com/", referer="https://www.bilibili.com/")
        except Exception:
            pass

    def _bootstrap_fingerprint(self):
        """调用指纹接口，生成 buvid3/buvid4 反爬 Cookie（规避风控的关键）。"""
        try:
            status, body = self._get(SPI_URL)
            d = (json.loads(body) or {}).get("data") or {}
            if d.get("b_3"):
                self._set_cookie("buvid3", d["b_3"])
            if d.get("b_4"):
                self._set_cookie("buvid4", d["b_4"])
        except Exception:
            pass
        self._set_cookie("b_nut", str(int(time.time() * 1000)))
        self._set_cookie("b_lsid", uuid.uuid4().hex.upper() + "_" + uuid.uuid4().hex[:8].upper())

    def _inject_cookie(self, cookie: str):
        for part in cookie.split(";"):
            part = part.strip()
            if "=" not in part:
                continue
            k, _, v = part.partition("=")
            if k.strip():
                self._set_cookie(k.strip(), v.strip())

    def _load_wbi_keys(self):
        j = self.get_json(NAV_URL, retries=2)
        try:
            img = j["data"]["wbi_img"]["img_url"]
            sub = j["data"]["wbi_img"]["sub_url"]
            self.img_key = img.rsplit("/", 1)[-1].split(".")[0]
            self.sub_key = sub.rsplit("/", 1)[-1].split(".")[0]
        except Exception:
            self.img_key = self.sub_key = ""

    # ---- HTTP ----
    def _throttle(self):
        """全局令牌桶限速 + 风控冷却：所有实例/线程共享。"""
        global _NEXT_REQ_AT, _COOLDOWN_UNTIL
        interval = 1.0 / self.rate if self.rate > 0 else 0.0
        while True:
            now = time.time()
            with _RATE_LOCK:
                wait = max(0.0, _COOLDOWN_UNTIL - now, _NEXT_REQ_AT - now)
                if wait <= 0:
                    _NEXT_REQ_AT = max(now, _NEXT_REQ_AT) + interval
                    return
            time.sleep(min(wait, 0.5))

    def _mark_risk_cooldown(self):
        """风控命中后，让所有线程暂停一段时间，避免继续轰炸把限流越拉越长。"""
        global _COOLDOWN_UNTIL
        with _RATE_LOCK:
            _COOLDOWN_UNTIL = max(_COOLDOWN_UNTIL, time.time() + self.cooldown)

    def _get(self, url: str, referer: str = REFERER):
        self._throttle()
        headers = {
            "User-Agent": UA,
            "Referer": referer,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        req = urllib.request.Request(url, headers=headers)
        try:
            with self.opener.open(req, timeout=20) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception as e:
            return -1, str(e)

    def get_json(self, url: str, retries: int = None):
        """带风控退避 + Cookie 刷新的 JSON 请求。

        - HTTP 412：IP 被硬封，重试无意义，直接快速失败并提示。
        - -799 / -352 / -412 / -504 / -509：软限流或校验失败，退避重试并刷新指纹。
        """
        retries = self.max_retries if retries is None else retries
        last = {"code": -1, "message": "未知错误"}
        risk_strikes = 0
        for attempt in range(retries):
            status, body = self._get(url)
            try:
                j = json.loads(body)
            except Exception:
                j = {"code": -1, "message": f"解析失败 status={status}", "_raw": body[:200]}
            code = j.get("code")

            # HTTP 412 = 硬封（返回的是风控 HTML 页），重试没用，直接放弃
            if status == 412:
                log("  [硬封] HTTP 412：当前 IP 被 B 站临时封禁，重试无效。请稍等几分钟、换网络，或提供登录 Cookie。")
                return {"code": -412, "message": "HTTP 412 风控拦截", "_raw": body[:200]}

            if code in RISK_CODES or status in RISK_HTTP:
                last = j
                risk_strikes += 1
                self._mark_risk_cooldown()
                wait = min(1.5 * (attempt + 1) + random.random(), 6.0)
                log(f"  [风控/限流 code={code} http={status}] 全局冷却，随后重试 ({attempt + 1}/{retries})...")
                if risk_strikes >= 2:
                    self._bootstrap_fingerprint()
                    risk_strikes = 0
                time.sleep(wait)
                continue
            return j
        return last

    def _sleep(self):
        if self.sleep > 0:
            time.sleep(self.sleep + random.random() * 0.3)

    # ---- 1. 昵称 -> UID ----
    def search_users(self, keyword: str):
        url = SEARCH_URL + "?" + urllib.parse.urlencode(
            {"search_type": "bili_user", "keyword": keyword}
        )
        j = self.get_json(url)
        if j.get("code") != 0:
            raise RuntimeError(f"搜索失败 code={j.get('code')} message={j.get('message')}")
        result = (j.get("data") or {}).get("result") or []
        users = []
        for item in result:
            if not isinstance(item, dict):
                continue
            if "mid" in item and "uname" in item:
                users.append(item)
            elif isinstance(item.get("data"), list):
                for u in item["data"]:
                    if isinstance(u, dict) and "mid" in u:
                        users.append(u)
        return users

    @staticmethod
    def norm_user(u: dict) -> dict:
        return {
            "mid": str(u.get("mid", "")),
            "uname": str(u.get("uname", "")),
            "sign": str(u.get("usign", "") or ""),
            "fans": u.get("fans", ""),
            "level": u.get("level", ""),
            "face": u.get("upic", ""),
        }

    # ---- 2. UID -> 投稿视频 ----
    def _videos_page(self, mid, pn, ps):
        j = self.get_json(VIDEO_URL_OLD + "?" + urllib.parse.urlencode(
            {"mid": mid, "pn": pn, "ps": ps}
        ))
        if j.get("code") != 0:
            if self.img_key:
                params = enc_wbi({"mid": mid, "pn": pn, "ps": ps}, self.img_key, self.sub_key)
                j = self.get_json(VIDEO_URL_WBI + "?" + urllib.parse.urlencode(params))
        if j.get("code") != 0:
            raise RuntimeError(f"投稿接口失败 code={j.get('code')} message={j.get('message')}")
        return j.get("data") or {}

    def user_videos(self, mid, max_videos, ps=30):
        videos = []
        pn = 1
        total = None
        while len(videos) < max_videos:
            data = self._videos_page(mid, pn, ps)
            vlist = (data.get("list") or {}).get("vlist") or []
            page = data.get("page") or {}
            total = page.get("count", total)
            if not vlist:
                break
            for v in vlist:
                videos.append({
                    "aid": v.get("aid", ""),
                    "bvid": v.get("bvid", ""),
                    "title": v.get("title", ""),
                    "comment_count": v.get("comment", 0),
                    "pubdate": v.get("pubdate", 0),
                })
                if len(videos) >= max_videos:
                    break
            if total is not None and pn * ps >= int(total):
                break
            pn += 1
            self._sleep()
        return videos

    # ---- 2b. UID -> 公开收藏的视频 ----
    def user_fav_folders(self, mid):
        """该用户创建的公开收藏夹列表（私密收藏夹未登录时拿不到）。"""
        j = self.get_json(FAV_FOLDER_URL + "?" + urllib.parse.urlencode({"up_mid": mid}))
        if j.get("code") != 0:
            raise RuntimeError(f"收藏夹列表获取失败 code={j.get('code')} message={j.get('message')}")
        data = j.get("data") or {}
        folders = []
        for f in (data.get("list") or []):
            if not isinstance(f, dict):
                continue
            if f.get("attr", 0) & 1:   # bit0=1 表示私有，跳过
                continue
            folders.append({
                "media_id": f.get("id"),
                "title": f.get("title", ""),
                "count": f.get("media_count", 0),
            })
        return folders

    def fav_folder_videos(self, media_id, max_videos):
        """某个收藏夹里的视频。

        用 resource/ids 一次拿全 id+bvid（免翻页），再用 resource/infos 批量补标题，
        比逐页拉 resource/list 省大量请求。
        """
        ids_url = "https://api.bilibili.com/x/v3/fav/resource/ids?" + urllib.parse.urlencode(
            {"media_id": media_id, "platform": "web"}
        )
        j = self.get_json(ids_url)
        if j.get("code") != 0:
            return []
        items = [it for it in (j.get("data") or []) if isinstance(it, dict)]
        # 只取视频稿件 type==2
        video_items = [it for it in items if it.get("type") == 2 and (it.get("id") or it.get("bvid"))][:max_videos]

        videos = []
        for i in range(0, len(video_items), 50):
            batch = video_items[i:i + 50]
            resources = ",".join(f"{it.get('id')}:2" for it in batch if it.get("id"))
            info_map = {}
            if resources:
                infos = self.get_json("https://api.bilibili.com/x/v3/fav/resource/infos?" +
                                      urllib.parse.urlencode({"resources": resources, "platform": "web"}))
                for inf in (infos.get("data") or []) if infos.get("code") == 0 else []:
                    if isinstance(inf, dict):
                        info_map[inf.get("id")] = inf
            for it in batch:
                inf = info_map.get(it.get("id")) or {}
                videos.append({
                    "aid": it.get("id"),
                    "bvid": it.get("bvid") or it.get("bv_id") or inf.get("bvid") or "",
                    "title": inf.get("title") or "",
                    "comment_count": (inf.get("cnt_info") or {}).get("play", 0),
                    "pubdate": inf.get("pubtime", 0),
                })
            if len(videos) >= max_videos:
                break
        return videos[:max_videos]

    # ---- 2c. UID -> 转发过的视频（动态） ----
    @staticmethod
    def _dynamic_video(node):
        """从一条动态节点里抽取视频 aid/bvid/title（投稿或转发的原视频）。"""
        if not isinstance(node, dict):
            return None, None, ""
        mods = node.get("modules") or {}
        md = mods.get("module_dynamic") or {}
        arc = md.get("archive")
        if not isinstance(arc, dict):
            major = md.get("major") or {}
            arc = major.get("archive")
        if isinstance(arc, dict):
            return arc.get("aid"), arc.get("bvid"), arc.get("title", "")
        return None, None, ""

    def user_dynamics(self, mid, max_items):
        """从动态流里提取转发过的视频（含部分投稿）。"""
        videos = []
        offset = ""
        while len(videos) < max_items:
            params = {"host_mid": mid, "offset": offset}
            if self.img_key:
                params = enc_wbi(params, self.img_key, self.sub_key)
            j = self.get_json(DYNAMIC_URL + "?" + urllib.parse.urlencode(params))
            if j.get("code") != 0:
                break
            data = j.get("data") or {}
            items = data.get("items") or []
            if not items:
                break
            for it in items:
                # 转发动态优先取 orig 里的原视频
                nodes = [it]
                if isinstance(it.get("orig"), dict):
                    nodes.insert(0, it["orig"])
                got = False
                for node in nodes:
                    aid, bvid, title = self._dynamic_video(node)
                    if aid or bvid:
                        videos.append({
                            "aid": aid, "bvid": bvid, "title": title,
                            "comment_count": 0, "pubdate": 0,
                        })
                        got = True
                        break
                if got and len(videos) >= max_items:
                    break
            if not data.get("has_more"):
                break
            offset = data.get("offset") or ""
            self._sleep()
        return videos

    # ---- 3. 视频评论（按时间倒序） ----
    def video_comments(self, oid, max_pages, start_ts=0, early_stop=False):
        """生成器：逐条产出【一级评论】，按时间倒序。

        楼中楼（二级回复）不在本方法里产出，由 collect() 对 rcount>0 的楼层调用
        thread_replies() 单独展开，保证子回复抓全。

        early_stop=True 且 start_ts>0 时：某页最新的评论都比 start 旧，就提前停止。
        """
        cursor = 0
        for _ in range(max_pages):
            j = self.get_json(COMMENT_URL + "?" + urllib.parse.urlencode(
                {"type": 1, "oid": oid, "mode": COMMENT_MODE, "next": cursor}
            ))
            if j.get("code") != 0:
                break
            data = j.get("data") or {}
            replies = data.get("replies") or []
            if not replies:
                break
            if early_stop and start_ts and int(replies[0].get("ctime") or 0) < start_ts:
                break
            for r in replies:
                yield r
            cur = data.get("cursor") or {}
            if cur.get("is_end"):
                break
            nxt = cur.get("next", 0)
            if not nxt or nxt == cursor:
                break
            cursor = nxt
            self._sleep()

    def thread_replies(self, oid, root, max_pages=5):
        """生成器：产出某条一级评论下的全部楼中楼（二级回复）。

        用 /x/v2/reply/reply 分页拉全（page.count 为该楼层子回复总数）。
        """
        ps = 20
        for pn in range(1, max_pages + 1):
            j = self.get_json("https://api.bilibili.com/x/v2/reply/reply?" + urllib.parse.urlencode(
                {"type": 1, "oid": oid, "root": root, "pn": pn, "ps": ps}
            ))
            if j.get("code") != 0:
                break
            data = j.get("data") or {}
            replies = data.get("replies") or []
            if not replies:
                break
            for r in replies:
                yield r
            page = data.get("page") or {}
            total = int(page.get("count") or 0)
            if total and pn * ps >= total:
                break
            self._sleep()

    # ---- 4. 视频信息（BV/av/链接 -> aid） ----
    def video_info(self, aid=None, bvid=None):
        params = {}
        if bvid:
            params["bvid"] = bvid
        elif aid:
            params["aid"] = aid
        else:
            raise ValueError("需要提供 aid 或 bvid")
        j = self.get_json(VIEW_URL + "?" + urllib.parse.urlencode(params))
        if j.get("code") != 0:
            raise RuntimeError(f"视频信息获取失败 code={j.get('code')} message={j.get('message')}")
        d = j.get("data") or {}
        return {
            "aid": d.get("aid"),
            "bvid": d.get("bvid", bvid or ""),
            "title": d.get("title", ""),
            "comment_count": (d.get("stat") or {}).get("reply", 0),
        }

    def resolve_video_ref(self, ref: str):
        """把用户给的 BV号 / av号 / 视频链接 解析成视频信息。"""
        ref = (ref or "").strip()
        m = re.search(r"(BV[0-9A-Za-z]+)", ref, re.I)
        if m:
            return self.video_info(bvid=m.group(1))
        m = re.search(r"(?:av|aid[=:])(\d+)", ref, re.I)
        if m:
            return self.video_info(aid=int(m.group(1)))
        if ref.isdigit():
            return self.video_info(aid=int(ref))
        raise ValueError(f"无法识别的视频：{ref}")


def normalize_url(u):
    """把 B 站常见的 // 开头图片地址补全成 https://。"""
    if isinstance(u, str):
        u = u.strip()
        if u.startswith("//"):
            return "https:" + u
        return u
    return ""


def extract_comment(r: dict, video: dict, target_mid) -> dict:
    member = r.get("member") or {}
    content = r.get("content") or {}
    ctime = int(r.get("ctime") or 0)
    try:
        tstr = datetime.fromtimestamp(ctime).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        tstr = ""
    message = content.get("message", "") or ""
    pictures = content.get("pictures") or []
    img_urls = [normalize_url(p.get("img_src"))
                for p in pictures if isinstance(p, dict) and p.get("img_src")]
    if not message.strip() and img_urls:
        message = "[图片评论]"
    root = r.get("root")
    level = "一级评论" if (root in (0, "0", None)) else "回复(楼中楼)"
    return {
        "UID": target_mid,
        "昵称": member.get("uname", ""),
        "评论时间": tstr,
        "评论内容": message,
        "评论图片": "\n".join(img_urls),
        "点赞数": int(r.get("like") or 0),
        "评论层级": level,
        "稿件标题": video.get("title", ""),
        "稿件链接": f"https://www.bilibili.com/video/{video.get('bvid', '')}",
        "评论ID": r.get("rpid", ""),
        "楼层": r.get("floor", ""),
    }


def sanitize_filename(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", s).strip()
    return s or "unknown"


def save_results(records, uname, mid, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = sanitize_filename(f"{uname}_{mid}_{stamp}")

    csv_path = os.path.join(out_dir, base + ".csv")
    json_path = os.path.join(out_dir, base + ".json")

    columns = ["UID", "昵称", "评论时间", "评论内容", "评论图片", "点赞数", "评论层级",
               "稿件标题", "稿件链接", "评论ID", "楼层"]
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        for r in records:
            writer.writerow({k: r.get(k, "") for k in columns})

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)

    return csv_path, json_path


def choose_user(users, keyword, interactive):
    if not users:
        return None
    normed = [BiliClient.norm_user(u) for u in users]
    exact = [u for u in normed if u["uname"] == keyword]
    if exact and not interactive:
        u = exact[0]
        log(f"已精确匹配：{u['uname']}  (UID {u['mid']}，粉丝 {u['fans']})")
        return u

    show = normed[:10]
    log("找到以下候选用户：")
    for i, u in enumerate(show, 1):
        sign = (u["sign"] or "").replace("\n", " ")[:40]
        log(f"  [{i}] {u['uname']}  UID={u['mid']}  粉丝={u['fans']}  {sign}")
    if not interactive:
        u = exact[0] if exact else show[0]
        log(f"（自动选择）使用第 1 个候选：{u['uname']} (UID {u['mid']})")
        return u

    while True:
        try:
            choice = input("请输入序号选择用户（直接回车选第 1 个）: ").strip()
        except EOFError:
            return show[0]
        if choice == "":
            return show[0]
        if choice.isdigit() and 1 <= int(choice) <= len(show):
            return show[int(choice) - 1]
        log("输入无效，请重新输入。")


def _add_reply(reply, mid, start_ts, end_ts, video, records, seen):
    """把一条评论（若属于目标用户且在时间范围内）加入结果。返回是否加入。"""
    if str((reply.get("member") or {}).get("mid")) != str(mid):
        return 0
    ct = int(reply.get("ctime") or 0)
    if start_ts and ct < start_ts:
        return 0
    if end_ts and ct > end_ts:
        return 0
    rpid = reply.get("rpid")
    if rpid in seen:
        return 0
    seen.add(rpid)
    records.append(extract_comment(reply, video, mid))
    return 1


def scan_video(client, mid, video, max_comment_pages, max_sub_threads, max_sub_pages,
               start_ts, end_ts, early_stop, expand_replies):
    """扫描单个视频，返回该视频里目标用户的评论。

    expand_replies=False 时只取一级评论 + 接口自带的楼中楼预览（快）；
    expand_replies=True 时才逐层展开全部楼中楼（慢但更全）。
    """
    records = []
    seen = set()
    found = 0
    sub_threads = 0
    for top in client.video_comments(video["aid"], max_comment_pages, start_ts, early_stop):
        found += _add_reply(top, mid, start_ts, end_ts, video, records, seen)
        preview = top.get("replies") or []
        for sub in preview:
            found += _add_reply(sub, mid, start_ts, end_ts, video, records, seen)
        if not expand_replies:
            continue
        rcount = int(top.get("rcount") or 0)
        if rcount > len(preview) and sub_threads < max_sub_threads:
            sub_threads += 1
            try:
                for sub in client.thread_replies(video["aid"], top.get("rpid"), max_sub_pages):
                    found += _add_reply(sub, mid, start_ts, end_ts, video, records, seen)
            except Exception:
                pass
    return video, records, found


def collect(client_factory, mid, videos, max_comment_pages, max_sub_threads, max_sub_pages,
            start_ts, end_ts, early_stop, expand_replies, workers):
    """并发扫描多个视频，合并结果并去重。"""
    all_records = []
    total = 0
    tls = threading.local()

    def get_client():
        c = getattr(tls, "client", None)
        if c is None:
            c = client_factory()
            tls.client = c
        return c

    def worker(video):
        return scan_video(get_client(), mid, video, max_comment_pages,
                          max_sub_threads, max_sub_pages, start_ts, end_ts,
                          early_stop, expand_replies)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futures = [ex.submit(worker, v) for v in videos]
        for i, fut in enumerate(concurrent.futures.as_completed(futures), 1):
            try:
                video, records, found = fut.result()
            except Exception as e:
                log(f"  [警告] 某个视频抓取失败：{e}")
                continue
            all_records.extend(records)
            total += found
            log(f"  [{i}/{len(videos)}]《{video['title'][:32]}》找到 {found} 条，累计 {total} 条")

    # 跨视频按评论 rpid 去重
    seen = set()
    uniq = []
    for r in all_records:
        k = r.get("评论ID")
        if k in seen:
            continue
        seen.add(k)
        uniq.append(r)
    return uniq


def parse_args(argv):
    p = argparse.ArgumentParser(
        description="B站用户评论爬虫：输入昵称，抓取该用户在指定视频下发的评论（支持时间段过滤）",
        add_help=True,
    )
    p.add_argument("nickname", nargs="?", help="用户昵称")
    p.add_argument("--uid", help="用户 UID（可直接指定，跳过昵称搜索）")
    p.add_argument("--url", help="用户空间链接，例如 https://space.bilibili.com/12345")
    p.add_argument("--bvid", action="append", help=(
        "额外扫描的视频（可多次传入或逗号分隔），支持 BV号 / av号 / 视频链接，"
        "例如 --bvid BV1xxxx --bvid BV1yyyy"))
    p.add_argument("--bvid-file", help="一个文件，每行一个视频（BV号/av号/链接），批量扫描")
    p.add_argument("--no-own-videos", action="store_true", help="不扫描该用户自己的投稿（只扫收藏/转发/指定视频）")
    p.add_argument("--no-fav", action="store_true", help="不扫描该用户的公开收藏")
    p.add_argument("--no-dynamic", action="store_true", help="不扫描该用户转发过的视频")
    p.add_argument("--max-fav-videos", type=int, default=100, help="公开收藏最多扫描的视频数（默认 100）")
    p.add_argument("--max-dynamic", type=int, default=50, help="转发动态最多扫描的视频数（默认 50）")
    p.add_argument("--start", help="开始时间，如 2026-01-01 或 \"2026-01-01 00:00:00\"")
    p.add_argument("--end", help="结束时间，如 2026-12-31（只写日期会算到当天 23:59:59）")
    p.add_argument("--days", type=int, help="只抓最近 N 天（等价于 start=今天-N天, end=现在）")
    p.add_argument("--max-videos", type=int, default=30, help="最多扫描的投稿视频数（默认 30）")
    p.add_argument("--max-comment-pages", type=int, default=20, help="每个视频最多翻页数（默认 20，每页约 20 条）")
    p.add_argument("--expand-replies", action="store_true", help="展开全部楼中楼（更全，但慢很多；默认只取预览的楼中楼）")
    p.add_argument("--max-sub-threads", type=int, default=30, help="展开楼中楼时，每视频最多展开多少楼层（默认 30）")
    p.add_argument("--max-sub-pages", type=int, default=3, help="展开楼中楼时，每层最多翻几页（默认 3，每页 20 条）")
    p.add_argument("--workers", type=int, default=4, help="并发线程数（默认 4）")
    p.add_argument("--rate", type=float, default=8.0, help="全局限速：每秒最多请求数（默认 8；被限流再调低）")
    p.add_argument("--cooldown", type=float, default=8.0, help="风控命中后全局冷却秒数（默认 8）")
    p.add_argument("--sleep", type=float, default=0.05, help="额外请求间隔秒数（默认 0.05）")
    p.add_argument("--cookie", default="", help="可选登录 Cookie，如 SESSDATA=xxx（提速度关键）")
    p.add_argument("--no-early-stop", action="store_true", help="关闭按时间提前停止（完整模式，更慢）")
    p.add_argument("--out", default=None, help="输出目录（默认脚本目录下 output）")
    p.add_argument("--interactive", action="store_true", help="强制手动选择候选用户")
    return p.parse_args(argv)


def _fail(msg="执行失败（见日志）"):
    return {"ok": False, "error": msg, "records": [], "csv_path": "", "json_path": "", "uname": "", "mid": ""}


def crawl(opts):
    """执行完整爬取流程。opts 需与命令行参数同名的属性。返回结果字典。"""
    ensure_console_utf8()
    args = opts

    # ---- 时间范围 ----
    start_ts = end_ts = None
    try:
        if args.days is not None:
            if args.start or args.end:
                log("--days 与 --start/--end 不能同时使用。")
                return _fail()
            now = int(time.time())
            end_ts = now
            start_ts = now - args.days * 86400
        else:
            if args.start:
                start_ts = parse_time(args.start, is_end=False)
            if args.end:
                end_ts = parse_time(args.end, is_end=True)
    except ValueError as e:
        log(f"时间解析错误：{e}")
        return _fail()

    early_stop = (start_ts is not None) and (not getattr(args, "no_early_stop", False))

    if start_ts or end_ts:
        def fmt(ts):
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else "…"
        log(f"时间范围：{fmt(start_ts)} ~ {fmt(end_ts)}")
        if early_stop:
            log("（已启用按时间提前停止，扫描到早于开始时间的评论即停止翻页，减少请求）")
        else:
            log("（完整模式：不提前停止，会翻完每个视频的评论页，确保楼中楼不漏）")

    client = BiliClient(cookie=args.cookie, sleep=args.sleep, rate=args.rate, cooldown=args.cooldown)

    # ---- 确定目标 UID ----
    mid = None
    uname = None
    if args.uid:
        mid = str(args.uid).strip()
    elif args.url:
        m = re.search(r"space\.bilibili\.com/(\d+)", args.url)
        if m:
            mid = m.group(1)
        else:
            log("无法从链接中解析 UID，请确认是 space.bilibili.com/数字 这种格式。")
            return _fail()
    else:
        nickname = args.nickname
        if not nickname:
            try:
                nickname = input("请输入要查询的 B 站用户昵称: ").strip()
            except EOFError:
                log("未读取到昵称。")
                return _fail()
        if not nickname:
            log("昵称为空，退出。")
            return _fail()
        log(f"正在搜索用户「{nickname}」...")
        try:
            users = client.search_users(nickname)
        except Exception as e:
            log(f"搜索出错：{e}")
            return _fail()
        chosen = choose_user(users, nickname, args.interactive)
        if not chosen:
            log("未找到相关用户。")
            return _fail()
        mid, uname = chosen["mid"], chosen["uname"]

    log(f"目标用户 UID = {mid}")

    jobs = []

    # ---- 收集要扫描的视频：投稿 + 公开收藏 + 转发 ----
    if not args.no_own_videos:
        log("正在抓取投稿视频列表...")
        try:
            videos = client.user_videos(mid, args.max_videos)
            log(f"  投稿视频：{len(videos)} 个。")
            jobs.extend(videos)
        except Exception as e:
            log(f"  投稿列表抓取失败：{e}")
            log("  （可稍后重试，或用 --cookie 提供登录 Cookie；也可用 --no-own-videos 跳过）")

    if not args.no_fav:
        log("正在抓取公开收藏的视频...")
        try:
            fav_videos = []
            folders = client.user_fav_folders(mid)
            log(f"  公开收藏夹：{len(folders)} 个。")
            for fo in folders:
                if len(fav_videos) >= args.max_fav_videos:
                    break
                vs = client.fav_folder_videos(fo["media_id"], args.max_fav_videos - len(fav_videos))
                fav_videos.extend(vs)
            log(f"  收藏的视频：{len(fav_videos)} 个。")
            jobs.extend(fav_videos)
        except Exception as e:
            log(f"  公开收藏抓取失败：{e}")

    if not args.no_dynamic:
        log("正在抓取转发过的视频...")
        try:
            dyn_videos = client.user_dynamics(mid, args.max_dynamic)
            log(f"  转发/动态视频：{len(dyn_videos)} 个。")
            jobs.extend(dyn_videos)
        except Exception as e:
            log(f"  动态抓取失败：{e}")

    # 用户指定的视频（BV/av/链接），可来自 --bvid 和 --bvid-file
    refs = []
    for item in (args.bvid or []):
        refs.extend([x.strip() for x in item.split(",") if x.strip()])
    if args.bvid_file:
        try:
            with open(args.bvid_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        refs.append(line)
        except OSError as e:
            log(f"读取 --bvid-file 失败：{e}")

    for ref in refs:
        try:
            v = client.resolve_video_ref(ref)
            if v.get("aid"):
                jobs.append(v)
                log(f"已附加指定视频：{v['title'][:30]} (评论 {v['comment_count']} 条)")
        except Exception as e:
            log(f"视频解析失败「{ref}」：{e}")

    # 去重（按 aid；没有 aid 的按 bvid）
    seen = set()
    uniq = []
    for v in jobs:
        key = v.get("aid") or v.get("bvid")
        if not key or key in seen:
            continue
        seen.add(key)
        uniq.append(v)
    jobs = uniq

    if not jobs:
        log("没有可扫描的视频，退出。")
        return _fail()

    log(f"开始扫描 {len(jobs)} 个视频的评论区（{args.workers} 线程并发，每视频最多 {args.max_comment_pages} 页）...")

    def make_client():
        return BiliClient(cookie=args.cookie, sleep=args.sleep,
                          rate=args.rate, cooldown=args.cooldown, light=True)

    records = collect(make_client, mid, jobs, args.max_comment_pages,
                      args.max_sub_threads, args.max_sub_pages,
                      start_ts, end_ts, early_stop,
                      getattr(args, "expand_replies", False), args.workers)

    out_dir = args.out or os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
    csv_path, json_path = save_results(records, uname or mid, mid, out_dir)

    log("")
    log("=" * 60)
    log(f"完成！共找到该用户本人发的评论 {len(records)} 条。")
    log(f"CSV : {csv_path}")
    log(f"JSON: {json_path}")
    log("=" * 60)
    if len(records) == 0:
        log("提示：结果为 0 可能是：")
        log("  1) 该时间段内，该用户在「投稿/收藏/转发」这些视频下没有评论；")
        log("  2) 时间范围太窄，或该用户很少在别人视频下评论；")
        log("  3) 可加 --bvid 指定 TA 具体评论过的视频再试，或用 --cookie 登录态提高覆盖。")
    return {
        "ok": True,
        "error": "",
        "records": records,
        "csv_path": csv_path,
        "json_path": json_path,
        "uname": uname or mid,
        "mid": mid,
    }


def main(argv):
    args = parse_args(argv)
    res = crawl(args)
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))