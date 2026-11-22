"""Cookie 备份/恢复（程序专用 Chrome 配置 ↔ data/cookies.json）。含登录凭证，注意保密。"""
import json
import time

import config
import db
import risk


def _keys(c):
    keep = ("name", "value", "domain", "path", "secure", "expiry", "httpOnly", "sameSite")
    return {k: c[k] for k in keep if c.get(k) is not None}


def dump(bot, log):
    blob = {}
    for p, url in config.LOGIN_URLS.items():
        if p == "boss" and not risk.boss_enabled():
            continue
        try:
            def _g(rt, u=url):
                rt.goto(u)
                return rt.d.get_cookies() or []
            cs = bot.call(_g)
            blob[p] = {"saved_at": time.strftime("%Y-%m-%d %H:%M"),
                       "cookies": [_keys(c) for c in cs]}
            log(f"Cookie备份 {config.PLATFORM_NAMES[p]}：{len(cs)} 条")
        except Exception as e:
            log(f"Cookie备份 {config.PLATFORM_NAMES[p]} 失败：{str(e).splitlines()[0][:70]}")
    config.COOKIES_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.COOKIES_PATH.write_text(json.dumps(blob, ensure_ascii=False))
    return blob


def status():
    blob = _load()
    return {p: {"count": len(v.get("cookies") or []), "saved_at": v.get("saved_at")}
            for p, v in blob.items()}


def _load():
    if config.COOKIES_PATH.exists():
        try:
            return json.loads(config.COOKIES_PATH.read_text())
        except Exception:
            pass
    return {}


def restore(bot, log, platforms=None):
    blob = _load()
    if not blob:
        return {}, "没有备份文件"
    out = {}
    for p, url in config.LOGIN_URLS.items():
        if platforms and p not in platforms:
            continue
        item = blob.get(p)
        if not item or not item.get("cookies"):
            out[p] = "无备份"
            continue
        if p == "boss" and not risk.boss_enabled():
            out[p] = "开关关闭"
            continue
        try:
            def _r(rt, u=url, cs=item["cookies"], plat=p):
                rt.goto(u)
                n = 0
                for c in cs:
                    payload = {"name": c["name"], "value": c["value"],
                               "path": c.get("path") or "/"}
                    for k in ("domain", "secure", "expiry"):
                        if c.get(k) is not None:
                            payload[k] = c[k]
                    try:
                        rt.d.add_cookie(payload)
                        n += 1
                    except Exception:
                        pass
                rt.goto(u)
                ok, _ = rt.wait_login_state(plat, 14)
                return n, ok
            n, ok = bot.call(_r)
            out[p] = f"注入{n}条·" + ("已登录" if ok else "仍未登录")
        except Exception as e:
            out[p] = f"失败：{str(e).splitlines()[0][:50]}"
    return out, ""
