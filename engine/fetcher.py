# -*- coding: utf-8 -*-
"""分层抓取器：L1 requests → L2 cloudscraper → L3 真浏览器(DrissionPage)。

对外接口与 autobs.Fetcher 兼容：get(url, referer=)/post(url, data, referer=)
返回 requests.Response 兼容对象（.status_code/.content/.url/.text），失败返回 None。
"""
import re
import threading
import time
import queue

import os
import requests
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "autobs"))
from autobs import UA, is_waf_page

try:
    import cloudscraper
except ImportError:
    cloudscraper = None


class BrowserResponse:
    """把 DrissionPage 页面包装成 requests.Response 兼容对象。
    text 是浏览器已正确解码的字符串；parse_doc 对 browser 标记的响应
    直接用 text 构建，避免 lxml 对无 meta 声明的 bytes 猜错编码。"""

    def __init__(self, url, html):
        self.status_code = 200
        self.url = url
        self.content = html.encode("utf-8", errors="ignore")
        self.headers = {"Content-Type": "text/html; charset=utf-8"}
        self.text = html
        self.browser = True


class BrowserLayer:
    """L3：真浏览器（Chrome 优先，Edge 兜底），懒启动、进程内单例。

    单个 ChromiumPage 配一把全局锁时，并发 L3 请求严格排队（page.get 25s +
    等 JS 挑战 10s），8 个候选最坏 8×35s，直接被外层预算切掉。这里改成
    标签页池：最多 POOL_SIZE 个 tab，每个 tab 一把锁，并发上限从 1 提到 2，
    且单次等挑战有硬上限（wait），超时立即返回，不再死等 10s。
    """

    POOL_SIZE = 2

    def __init__(self, headless=True, pool_size=None):
        self.headless = headless
        self._pool_size = max(1, int(pool_size or self.POOL_SIZE))
        self._page = None
        self._lock = threading.Lock()      # 只管「启动浏览器 / 建 tab」
        self._tabs = []                    # [(tab, lock), ...]
        self._free = queue.Queue()

    def _ensure(self):
        if self._page is not None:
            return self._page
        from DrissionPage import ChromiumPage, ChromiumOptions
        import shutil
        opts = ChromiumOptions()
        browser = None
        for name, path in (
            ("chrome", shutil.which("chrome") or r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
            ("msedge", shutil.which("msedge") or r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        ):
            if path and shutil.which(path) or (path and __import__("os").path.exists(path)):
                opts.set_browser_path(path)
                opts.set_paths(browser_path=path)
                browser = name
                break
        if browser is None:
            raise RuntimeError("未找到 Chrome/Edge 浏览器")
        if self.headless:
            opts.headless()
        opts.set_argument("--disable-blink-features=AutomationControlled")
        opts.set_argument("--no-first-run")
        opts.set_argument("--no-default-browser-check")
        self._page = ChromiumPage(opts)
        return self._page

    def _acquire(self, timeout=30):
        """取一个空闲标签页 (tab, lock)；池子满了就排队等，等不到返回 None。"""
        try:
            return self._free.get_nowait()
        except queue.Empty:
            pass
        with self._lock:
            page = self._ensure()
            if len(self._tabs) < self._pool_size:
                tab = page if not self._tabs else page.new_tab()
                item = (tab, threading.Lock())
                self._tabs.append(item)
                return item
        try:
            return self._free.get(timeout=timeout)
        except queue.Empty:
            return None

    def get(self, url, timeout=25, wait=6):
        item = self._acquire(timeout=wait + timeout)
        if item is None:            # 池子被占满且等超时：快速失败，别拖住调用方
            return None
        tab, lk = item
        try:
            with lk:
                tab.get(url, timeout=timeout)
                # 等 JS 挑战跳转完成（CF 会有 5 秒等待页）。轮询间隔 0.4s，
                # 上限 wait 秒 —— 原来固定 10s 硬等，慢站会把并发全拖死。
                html = tab.html or ""
                deadline = time.time() + wait
                while time.time() < deadline:
                    if html and not is_waf_page(html.encode("utf-8", errors="ignore")):
                        break
                    time.sleep(0.4)
                    html = tab.html or ""
                return BrowserResponse(tab.url or url, tab.html or html)
        finally:
            self._free.put(item)

    def close(self):
        with self._lock:
            for tab, _lk in self._tabs:
                try:
                    tab.close() if tab is not self._page else tab.quit()
                except Exception:
                    pass
            self._tabs = []
            while not self._free.empty():
                try:
                    self._free.get_nowait()
                except queue.Empty:
                    break
            if self._page is not None:
                try:
                    self._page.quit()
                except Exception:
                    pass
                self._page = None


class LayeredFetcher:
    """get/post 自动分层：普通请求失败或命中 WAF 时逐级升级。"""

    def __init__(self, debug=False, headless=True):
        self.s = requests.Session()
        self.s.trust_env = False  # 书站均为国内直连站，绕过系统代理（代理常导致 RST/超时）
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
        self._scraper = None
        self._browser = None
        self._headless = headless
        self.debug = debug
        self.last = 0.0
        # 每站节流：{host: 上次请求时间戳}。原来是一个全局 self.last，10 个种子站
        # 并发搜索时全排同一个 0.6s 闸门（实测「遮天」正好 6.0s = 10 × 0.6s），
        # 站点响应时间被节流完全掩盖。改成按 host 后，对单站仍礼貌，跨站互不拖累。
        self._last_by_host = {}
        self._throttle_lock = threading.Lock()
        self.layer_stats = {"L1": 0, "L2": 0, "L3": 0}
        # 每站熔断：{host: [连续失败数, 冷却截止时间戳]}
        # 连续 3 次全层失败 → 冷却 90 秒（期间直接快速失败，不重试），
        # 避免站方限流时每个请求都走完整三层重试把调用方拖死
        self._host_state = {}
        self._host_lock = threading.Lock()

    def _host_of(self, url):
        try:
            from urllib.parse import urlparse
            return urlparse(url).netloc.lower()
        except Exception:
            return ""

    def _host_blocked(self, url):
        host = self._host_of(url)
        with self._host_lock:
            st = self._host_state.get(host)
            return bool(st and time.time() < st[1])

    def _host_fail(self, url):
        host = self._host_of(url)
        with self._host_lock:
            st = self._host_state.setdefault(host, [0, 0])
            st[0] += 1
            if st[0] >= 3:
                st[1] = time.time() + 90
                st[0] = 0
                if self.debug:
                    print("[fuse] %s 冷却 90s" % host)

    def _host_ok(self, url):
        host = self._host_of(url)
        with self._host_lock:
            if host in self._host_state:
                self._host_state[host] = [0, 0]

    # ---- 对外接口（autobs 兼容） ----
    def get(self, url, **kw):
        return self._req("GET", url, **kw)

    def post(self, url, data=None, **kw):
        return self._req("POST", url, data=data, **kw)

    def _throttle(self, url=""):
        """同一站点两次请求间隔 ≥0.6s；不同站点各自计时，互不排队。"""
        host = self._host_of(url) if url else ""
        now = time.time()
        with self._throttle_lock:
            last = self._last_by_host.get(host, 0.0)
            wait = 0.6 - (now - last)
            # 先占位再睡：把本线程的「发车时刻」写回去，后面的线程据此顺延，
            # 避免多个线程同时算出 wait=0 一起冲同一站点。
            self._last_by_host[host] = max(now, last + 0.6)
            self.last = now
        if wait > 0:
            time.sleep(wait)

    def _ok(self, r):
        return r is not None and r.status_code == 200 and not is_waf_page(r.content)

    # "JS 设 cookie 后刷新"验证墙（ge_js_validator 等）：提取 cookie 重试即过，免开浏览器
    _JS_WALL = re.compile(
        r'document\.cookie\s*=\s*["\']([^"\']+)["\'][^<]{0,200}?location\.(?:reload|href)',
        re.S)

    def _try_js_wall(self, url, r):
        """若是 JS cookie 验证墙：把墙要求的 cookie 写入会话，返回 True（调用方重试）。"""
        if len(r.content) >= 2048:
            return False
        try:
            m = self._JS_WALL.search(r.content.decode("utf-8", errors="replace"))
        except Exception:
            return False
        if not m:
            return False
        kv = m.group(1).split(";")[0]
        if "=" not in kv:
            return False
        from urllib.parse import urlparse
        name, _, val = kv.partition("=")
        try:
            self.s.cookies.set(name.strip(), val.strip(),
                               domain=urlparse(url).netloc, path="/")
            if self._scraper is not None:
                self._scraper.cookies.set(name.strip(), val.strip(),
                                          domain=urlparse(url).netloc, path="/")
        except Exception:
            return False
        return True

    def _req(self, method, url, data=None, referer=None, force_layer=None,
             quick=False, allow_l2=True, timeout=8):
        # 熔断检查：该站冷却中直接快速失败
        if self._host_blocked(url):
            return None
        if quick:
            return self._req_quick(method, url, data, referer,
                                   allow_l2=allow_l2, timeout=timeout)
        self._throttle(url)
        headers = {}
        if referer:
            headers["Referer"] = referer
        if isinstance(data, (bytes, str)):
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        self._last_hard_fail = False  # 业务失败(404等)不算站点故障，不计熔断
        result = self._req_layers(method, url, data, headers, referer, force_layer)
        if result is not None:
            self._host_ok(url)
        elif self._last_hard_fail:
            self._host_fail(url)
        return result

    def _req_quick(self, method, url, data, referer, allow_l2=True, timeout=8):
        """快速模式：L1 单次请求（默认 8s 超时）；失败再试一次 cloudscraper，不走 L3。

        原来完全不做分层升级，遇到 Cloudflare 站点（笔趣阁tw）L1 稳定 403，
        体检的站点探活与章节快抓就会全线判死（实测：目录页/章节页全 None）。
        补一次 L2 兜底，代价约 1s，仍远快于完整链路。

        allow_l2=False 用于"批量预验候选页"场景：那里只需判断标题对不对，
        死站直接跳过即可。而 cloudscraper 首次 create_scraper 有秒级初始化开销，
        且 _scraper 是共享实例、无锁，多个候选并发进来会各建一次互相争抢
        —— 实测 9 个候选的预验因此从 5s 拖到 14.6s。"""
        headers = {}
        if referer:
            headers["Referer"] = referer
        if isinstance(data, (bytes, str)):
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        try:
            r = self.s.get(url, headers=headers, timeout=timeout) if method == "GET" \
                else self.s.post(url, data=data, headers=headers, timeout=timeout)
            if self._ok(r):
                self._host_ok(url)
                return r
        except requests.RequestException:
            pass
        if allow_l2 and cloudscraper is not None and method == "GET":
            try:
                if self._scraper is None:
                    self._scraper = cloudscraper.create_scraper(
                        browser={"custom": UA[:120]})
                r = self._scraper.get(url, headers=headers, timeout=12)
                if self._ok(r):
                    self.layer_stats["L2"] += 1
                    self._host_ok(url)
                    return r
            except Exception:
                pass
        return None

    def _req_layers(self, method, url, data, headers, referer, force_layer):

        # L1 requests（这些书站网络不稳常见，退避重试 3 次）
        if force_layer in (None, "L1"):
            for attempt in range(3):
                try:
                    r = self.s.get(url, headers=headers, timeout=15) if method == "GET" \
                        else self.s.post(url, data=data, headers=headers, timeout=15)
                    if self._ok(r):
                        self.layer_stats["L1"] += 1
                        return r
                    if r.status_code == 200 and self._try_js_wall(url, r):
                        continue  # JS cookie 墙：已写入过墙 cookie，立即重试
                    if r.status_code not in (403, 503, 429) and not is_waf_page(r.content):
                        # 404 等业务错误：不升级，直接失败（不算站点故障）
                        if r.status_code != 200:
                            if r.status_code in (404, 500):
                                self._last_hard_fail = False
                                return None
                            return r
                    break
                except requests.RequestException:
                    if attempt < 2:
                        time.sleep(2 + attempt * 3)
                        continue

        # L2 cloudscraper（GET/POST 都支持）
        if force_layer in (None, "L2") and cloudscraper is not None:
            for attempt in range(2):
                try:
                    if self._scraper is None:
                        self._scraper = cloudscraper.create_scraper(
                            browser={"custom": UA[:120]})
                    r = self._scraper.get(url, headers=headers, timeout=20) if method == "GET" \
                        else self._scraper.post(url, data=data, headers=headers, timeout=20)
                    if self._ok(r):
                        self.layer_stats["L2"] += 1
                        return r
                except Exception:
                    if attempt == 0:
                        time.sleep(2)
                        continue

        # 走到这里说明 L1/L2 都没成 —— 后续再失败属网络/防护层故障
        self._last_hard_fail = True

        # L3 真浏览器（只支持 GET；POST 站点极少被拦到需要 L3）
        if force_layer in (None, "L3") and method == "GET":
            try:
                if self._browser is None:
                    self._browser = BrowserLayer(headless=self._headless)
                r = self._browser.get(url)
                if self._ok(r):
                    self.layer_stats["L3"] += 1
                    return r
            except Exception as e:
                if self.debug:
                    print("[L3] browser failed: %s" % e)
        return None
