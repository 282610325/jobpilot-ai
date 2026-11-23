"""connect.py：一键连接账号（给非技术求职者用，不需要 F12 / 不需要懂抓包）。

一次点击完成：
1. 自动从本机 Chrome 读取三平台 Cookie（解密 Chrome 本地库，零手工复制）
2. 逐平台发一次极轻量搜索请求，判断「Cookie 可用 / 已过期 / 缺接口模板」
3. 只在「已过期」时才需要用户扫码；扫码后自动录制接口模板、关窗口
4. 之后所有采集/投递都走纯 HTTP，浏览器不再出现

对外只暴露两个动作：probe_all()（探测）和 login_and_record(platform)（扫码+抓包）。
"""
import time
from pathlib import Path

import api_tpl
import config
import cookie_extract
import db
import netio
import risk
from browser import Blocked, LiveDisabled
from platforms import boss_api, http_apply

NAMES = config.PLATFORM_NAMES


def refresh_cookies(platforms=None):
    """读取本机浏览器 Cookie 并落库。返回 (保存情况, 来源说明)。"""
    saved, src = cookie_extract.save_to_db(platforms)
    return saved, src


# 登录态必需 Cookie（只做本地判断，不触达平台，避免任何风险）
# 名称来自实测：智联网页登录看 x_passport_sid/c_user_id；猎聘看 lt_auth；
# BOSS 看 __zp_stoken__/zp_at/wt2。注意智联的 at/rt 是通用 token，
# 未登录访问首页也会下发，**不能**作为登录依据。
KEY_COOKIES = cookie_extract.WEB_LOGIN_KEYS


def _check(platform):
    """本地判断（不发任何请求）：ok / no_cookie / off。

    判断依据是「你日常浏览器里有没有网页登录态」，这正是"一键同步"的数据源。
    真正能否采集还要看采集窗口是否已同步，见 probe_all 的 synced 字段。
    """
    if not risk.platform_on(platform):
        reason = db.setting(f"off_reason_{platform}", "") or ""
        return "off", (f"{NAMES[platform]} 曾被风控自动关停，到设置页勾选「允许自动访问」恢复"
                       if reason == "risk" else
                       f"{NAMES[platform]} 已被暂停，到设置页勾选「允许自动访问」开启")
    got, _src = cookie_extract.extract_user([platform])
    ck = got.get(platform) or {}
    if not ck:
        return "no_cookie", "日常浏览器里没有该平台的 Cookie，请先在浏览器登录"
    ok, hits = cookie_extract.login_state(platform, ck)
    if not ok:
        return "no_cookie", (
            f"日常浏览器里有 {len(ck)} 条 Cookie，但缺少网页登录凭证"
            f"（应有 {'/'.join(KEY_COOKIES[platform])} 之一）。"
            "请在你的浏览器里正常登录一次该平台")
    return "ok", f"日常浏览器已登录（{len(ck)} 条，含 {hits[0]}）"


# 本次服务运行内同步成功的平台（内存标记：cookie 文件落盘不可靠，见 sync_to_window）
_SYNCED = {}


def probe_all(log=print):
    """检测三平台：日常浏览器登录态 + 采集窗口是否已同步（纯本地读库）。

    顺带把浏览器 Cookie 刷新进库（供纯 HTTP 通道用），零平台请求。
    """
    saved, src = refresh_cookies()
    if saved:
        log("已从本机浏览器刷新 Cookie：" +
            "、".join(f"{NAMES[p]}{n}条" for p, n in saved.items()))
    out = {}
    for p in config.PLATFORM_NAMES:
        st, msg = _check(p)
        item = {"status": st, "msg": msg, "synced": False, "sync_msg": ""}
        if st == "ok":
            mark = _SYNCED.get(p)
            if mark and mark[1]:
                item["synced"] = True
                item["sync_msg"] = f"本次运行已同步（{mark[0]}）"
            else:
                win = cookie_extract.extract_window([p])[0].get(p) or {}
                wok, whist = cookie_extract.login_state(p, win)
                item["synced"] = wok
                item["sync_msg"] = (
                    f"采集窗口有持久 Cookie（含 {whist[0]}）" if wok else
                    "采集窗口尚未同步 → 点「同步到采集窗口」")
        out[p] = item
        icon = {"ok": "✓", "expired": "✗", "no_cookie": "○",
                "no_tpl": "△", "cooldown": "⏸", "offline": "⏻",
                "off": "⏸"}.get(st, "?")
        log(f"  {icon} {NAMES[p]}：{msg}"
            + (f"｜{item['sync_msg']}" if item["sync_msg"] else ""))
    return out


def need_browser(status):
    """哪些平台必须开一次窗口（扫码或抓包）。"""
    return [p for p, v in status.items()
            if v["status"] in ("expired", "no_cookie", "no_tpl")
            and not (p == "boss" and not risk.boss_enabled())]


def login_window(platform):
    """打开可见窗口供用户扫码登录（这是唯一需要人工的步骤）。

    会一直等到用户完成登录（最多 3 分钟），避免"刚打开就报失败"。
    返回 (ok, 提示)。拿到登录态后自动刷新 Cookie 落库。
    """
    if not risk.platform_on(platform):
        return False, f"{NAMES[platform]} 已被暂停，请先在总览勾选「允许自动访问」"
    try:
        def _go(rt):
            from app import _probe_login      # 复用严格登录判定
            rt.show()
            ok, head = _probe_login(rt, config.LOGIN_URLS[platform], platform,
                                    timeout=180, keep_waiting=True)
            return ok, head
        from app import bot
        ok, head = bot.call(_go)
        if ok:
            refresh_cookies([platform])
            # 登录成功 = 采集窗口本次运行可用（智联的 session cookie 读不到文件，
            # 只能靠这个内存标记反映真实状态）
            _SYNCED[platform] = (time.strftime("%H:%M"), True)
            extra = ""
            if db.setting(f"enabled_{platform}", 1) == 0:
                extra = "；注意该平台曾被自动关停，需到设置页风控栏点「恢复」"
            return True, f"{NAMES[platform]} 已登录{extra}"
        return False, (f"3 分钟内没检测到登录（页面头部：{str(head)[:30]}），"
                       "请确认已完成扫码后重试")
    except LiveDisabled as e:
        return False, str(e)[:120]
    except Blocked as e:
        return False, str(e)[:120]
    except risk.Cooldown as e:
        return False, str(e)[:120]
    except Exception as e:
        return False, str(e).splitlines()[0][:140]


def auto_record(platform):
    """在已登录窗口里自动完成一次搜索并录制接口模板（用户不用操作）。"""
    if platform == "boss":
        return True, "BOSS 用内置接口，无需录制"
    from app import bot
    try:
        def _go(rt):
            return api_tpl.record(rt, lambda m: None, platform,
                                  probe_kw=_default_kw(platform))
        tpl = bot.call(_go)
        refresh_cookies([platform])
        return True, f"已录制 {NAMES[platform]} 接口（{tpl['method']} {tpl['url'][:70]}）"
    except LiveDisabled as e:
        return False, str(e)[:120]
    except Blocked as e:
        return False, str(e)[:160]
    except risk.Cooldown as e:
        return False, str(e)[:120]
    except Exception as e:
        return False, str(e).splitlines()[0][:140]


def _default_kw(platform):
    kws = config.COLLECT_KEYWORDS.get(platform) or []
    return kws[0] if kws else "IT经理"


def sync_to_window(platforms=None, log=print):
    """把你日常浏览器的登录态注入程序专用窗口（采集真正用的那份）。

    为什么需要：采集走浏览器会话，用的是 ~/Library/.../JobBot/chrome-profile；
    而设置页读到的是你日常 Chrome 的 Cookie —— 两个 profile 互不共享。
    只导航到各平台首页，不搜索、不开详情，风险很低。

    已知限制（实测）：智联网页登录态（x_passport_sid 等）是**会话级 Cookie**，
    天然无法跨浏览器实例传递 —— 只对猎聘/BOSS 这类持久 Cookie 有效。
    智联若同步后仍掉登录，请在专用窗口里真实登录一次，且服务别重启。
    """
    import time as _t
    got = {}
    src = ""
    for prof in cookie_extract.USER_PROFILES:
        if not prof.exists():
            continue
        g = cookie_extract.read_profile_full(prof, platforms) or {}
        for p, lst in g.items():
            if p not in got or len(lst) > len(got[p]):
                got[p] = lst
        if g:
            src = str(prof).replace(str(Path.home()), "~")
    if not got:
        return {}, src or "日常浏览器里没找到登录 Cookie"
    from app import bot                      # 延迟导入，避免循环依赖
    out = {}
    for p, cookies in got.items():
        if not risk.platform_on(p):
            out[p] = "跳过（该平台已被暂停）"
            log(f"  同步 {NAMES[p]}：跳过（平台已被暂停）")
            continue
        if not any(c["name"] in KEY_COOKIES[p] for c in cookies):
            out[p] = "跳过（日常浏览器里没有网页登录凭证）"
            log(f"  同步 {NAMES[p]}：跳过（缺网页登录凭证）")
            continue
        try:
            def _go(rt, p=p, cookies=cookies):
                url = config.LOGIN_URLS[p]
                rt.goto(url)
                dom = "." + cookie_extract.PLATFORM_DOMAINS[p][0].lstrip(".")
                n = sess = 0
                for c in cookies:
                    payload = {"name": c["name"], "value": c["value"],
                               "path": c.get("path") or "/",
                               "secure": c.get("secure", False),
                               "httpOnly": c.get("httpOnly", False)}
                    if c.get("expires"):
                        payload["expiry"] = int(c["expires"])
                    else:
                        sess += 1          # 会话级 Cookie，关浏览器即失效
                    for pl in (dict(payload, domain=dom), dict(payload)):
                        try:
                            rt.d.add_cookie(pl)
                            n += 1
                            break
                        except Exception:
                            continue
                rt.goto(url)
                ok, head = rt.wait_login_state(p, 18)
                return n, sess, ok, head
            n, sess, ok, head = bot.call(_go)
            tail = f"，其中 {sess} 条会话级（关浏览器即失效）" if sess else ""
            out[p] = (f"已注入 {n} 条{tail}，" + ("登录成功" if ok else
                      f"仍未登录（头部：{str(head)[:30]}）"))
            if ok and db.setting(f"enabled_{p}", 1) == 0:
                out[p] += "；但该平台曾被风控自动关停，需到设置页风控栏点「恢复」才能采集"
            _SYNCED[p] = (_t.strftime("%H:%M"), bool(ok))
            log(f"  同步 {NAMES[p]}：{out[p]}")
        except LiveDisabled as e:
            out[p] = f"失败：{str(e)[:80]}"
        except risk.Cooldown as e:
            out[p] = f"失败：{str(e)[:80]}"
        except Blocked as e:
            out[p] = f"失败：{str(e)[:80]}"
        except Exception as e:
            out[p] = f"失败：{str(e).splitlines()[0][:100]}"
            log(f"  同步 {NAMES[p]} 异常：{out[p]}")
    return out, src
