"""jobpilot 控制台：Flask 后端（任务队列、SQLite、风控闸门、AI、Cookie）。

启动：./run.sh；离线和在线在「设置 → 运行模式」里切换（默认离线）。
界面：http://127.0.0.1:8765
"""
import json
import os
import re
import threading
import time
from collections import deque
from datetime import datetime
from urllib.parse import urlsplit

from flask import Flask, jsonify, request, send_from_directory

import ai
import agent
import api_tpl
import chat
import citycfg
import config
import connect
import cookies
import db
import exporter
import ingest
import netio
import plan as planmod
import resume as R
import risk
import rules
import tools
from browser import Blocked, Bot, LaunchError, LiveDisabled
from platforms import (boss_api, city_api, http_apply, http_collect, liepin,
                       resume_sync, session_api, zhaopin)

app = Flask(__name__, static_folder="static", static_url_path="")
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024
bot = Bot()
MODULES = {"zhaopin": zhaopin, "liepin": liepin, "boss": boss_api}

LOGS = deque(maxlen=500)
STATE = {"task": None, "running": False, "done": 0, "total": 0, "message": ""}
# 在线简历核查的进度（同步请求，前端轮询这个看阶段）
MERGE_ST = {"platform": "", "stage": "idle", "started_at": 0}
_task_lock = threading.Lock()


def log(msg):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    LOGS.append(line)
    print(line, flush=True)


netio.set_logger(log)
agent.set_logger(log)
risk.migrate()          # 统一平台开关字段（兼容旧的 boss_enabled）


def _collect_cap(platform):
    """采集轮次上限：settings 的 rate_<p>_daily_collect 优先，回退 config。"""
    return int(db.setting(f"rate_{platform}_daily_collect",
                          config.RATE[platform]["daily_collect"]))


def _apply_channel(p):
    # 智联/猎聘没有可用的投递接口，只能走浏览器点击；BOSS 默认页面内接口
    default = "browser" if p in ("zhaopin", "liepin") else "session"
    return db.setting(f"apply_channel_{p}", default)


def _live():
    from browser import allow_live
    return allow_live()


# ---------------- 登录探针（严格：必须停在目标站点+头部出现账号名） ----------------

def _probe_login(rt, url, platform, timeout=16, keep_waiting=False):
    """严格登录检测。keep_waiting=True 时只导航一次，然后持续轮询，
    用于「打开窗口让用户扫码」场景（避免用户还没扫完就判失败）。"""
    want = urlsplit(url).netloc

    def _attempt():
        rt.goto(url)
        try:
            if platform == "boss":
                risk.guard_security(rt, 10)
        except risk.Cooldown as e:
            return "cooldown", str(e)
        if platform == "boss":
            rt.switch_to_url("zhipin.com")
        cur = rt.d.current_url or ""
        if urlsplit(cur).netloc != want:
            return "offsite", f"未能停在 {want}（当前：{cur[:60]}）"
        return rt.wait_login_state(platform, timeout)

    kind, head = _attempt()
    if kind is True:
        return True, head
    if kind == "cooldown":
        raise risk.Cooldown(head or "BOSS 冷却中")
    if keep_waiting and kind is False:
        # 已经停在正确站点、只是还没登录：原地继续等用户扫码，不重复导航
        end = time.time() + max(0, timeout - 16)
        while time.time() < end:
            kind2, head = rt.wait_login_state(platform, 12)
            if kind2 is True:
                return True, head
            head = head or head
        return False, head
    if kind in ("offsite", False) and not any(
            k in str(head) for k in ("登录", "注册", "扫码")):
        time.sleep(2)
        kind, head = _attempt()
        if kind == "cooldown":
            raise risk.Cooldown("BOSS 冷却中")
    return (kind is True), head


def _check_login(platforms):
    res = {}
    for p in platforms:
        if p == "boss" and not risk.boss_enabled():
            res[p] = "off"
            continue
        try:
            ok, head = bot.call(_probe_login, config.LOGIN_URLS[p], p)
            res[p] = bool(ok)
            if not ok:
                log(f"{config.PLATFORM_NAMES[p]} 未登录（头部：{str(head)[:40]}）")
        except risk.Cooldown as e:
            res[p] = "cooldown"
            log(f"{config.PLATFORM_NAMES[p]} {e}")
        except Exception as e:
            res[p] = None
            log(f"{config.PLATFORM_NAMES[p]} 检测异常：{str(e).splitlines()[0][:90]}")
        time.sleep(1.2)
    return res


def _preflight(platforms):
    """任务前置：过滤冷却/未开平台，检测登录，返回可继续列表。"""
    todo = []
    for p in platforms:
        try:
            risk.check(p)                 # 统一判断：开关 + 冷却，三平台一致
            todo.append(p)
        except risk.Cooldown as e:
            log(f"跳过 {config.PLATFORM_NAMES[p]}：{e}")
    if not todo:
        return []
    st = _check_login(todo)
    ok = [p for p in todo if st.get(p) is True]
    for p in todo:
        if st.get(p) is not True:
            log(f"{config.PLATFORM_NAMES[p]} 未登录/异常，本次跳过。"
                f"恢复方式：到「设置 → 账号连接」点对应平台的「专用窗口登录」，"
                f"在弹出的专用窗口里登录一次（采集用的是这个窗口的登录态，"
                f"与设置页读到的浏览器 Cookie 是两套，互不同步）")
    return ok


# ---------------- 任务 ----------------

def _run_task(name, fn):
    if agent.ST["running"] or agent.ST["pending"]:
        log(f"{name} 未启动：AI Agent 正在运行或有待确认操作")
        return False
    with _task_lock:
        if STATE["running"]:
            return False
        STATE.update({"running": True, "task": name, "done": 0, "total": 0,
                      "message": name + " 进行中"})

    def runner():
        try:
            fn()
            if STATE["message"] == name + " 进行中":
                STATE["message"] = name + " 完成"
                log(STATE["message"])
        except risk.Cooldown as e:
            STATE["message"] = f"{name} 已停止（风控冷却）：{e}"
            log(STATE["message"])
            db.event("risk", "", "task_stop", str(e))
        except Blocked as e:
            STATE["message"] = f"{name} 已停止：{e}"
            log(STATE["message"])
        except LiveDisabled as e:
            STATE["message"] = f"{name} 未执行：{e}"
            log(STATE["message"])
        except LaunchError as e:
            STATE["message"] = f"{name} 失败：{e}"
            log(STATE["message"])
        except Exception as e:
            first = str(e).strip().splitlines()[0][:240] if str(e).strip() else e.__class__.__name__
            STATE["message"] = f"{name} 出错：{first}"
            log(STATE["message"])
        finally:
            STATE.update({"running": False, "task": None})
    threading.Thread(target=runner, daemon=True).start()
    return True


def _plan_combos(platform):
    """从启用的计划行拼 (keyword, code, city) 组合；无计划回退配置默认。"""
    rows = [r for r in db.plan_list(only_enabled=True) if r["platform"] == platform]
    combos, cities = [], []
    if platform == "liepin":
        return [], sorted({r["city"] for r in rows if r["city"]}) or citycfg.names()
    if rows:
        for r in rows:
            combos.append((r["keyword"], r["city"], r["city"]))
        cities = sorted({c for _, c, _ in combos})
    else:
        kws = config.COLLECT_KEYWORDS.get(platform) or ["IT经理"]
        cities = citycfg.names()
        citymap = (citycfg.map_zl() if platform == "zhaopin" else citycfg.map_boss())
        combos = [(k, citymap.get(c), c) for c in cities for k in kws
                  if citymap.get(c)]
    return combos, cities


def _ai_score_new(platform=None):
    """对规则放行且未判定的岗位做 AI 匹配度判定（25 条一批）。"""
    if not ai.configured() or not db.setting("ai_score_enabled", 1):
        return 0
    rows = db.jobs_for_ai(platform)
    done = 0
    for i in range(0, len(rows), 25):
        batch = rows[i:i + 25]
        try:
            res = ai.score_jobs(batch)
        except Exception as e:
            log(f"AI 判定中断：{str(e).splitlines()[0][:80]}")
            break
        for jid, (tier, why) in res.items():
            db.set_ai(jid, tier, why)
            done += 1
    if done:
        log(f"AI 判定完成 {done} 条（强匹配/匹配/弱匹配/不匹配，见岗位库 AI 列）")
    return done


def _combos_for(platform, kw=None, cities=None):
    """(keyword, code, city) 组合：显式关键词 > 采集计划启用行 > 配置默认。"""
    if platform == "liepin":                      # 猎聘走推荐流，只要城市
        if cities:
            return [], cities
        rows = [r for r in db.plan_list(only_enabled=True) if r["platform"] == "liepin"]
        return [], sorted({r["city"] for r in rows if r["city"]}) or citycfg.names()
    if kw:
        cl = cities or citycfg.names()
        citymap = citycfg.map_zl() if platform == "zhaopin" else citycfg.map_boss()
        return [(k, citymap.get(c), c) for c in cl for k in kw if citymap.get(c)], cl
    return _plan_combos(platform)


def _task_collect(platforms, kw=None, cities=None, pages=None):
    if not _live():
        STATE["message"] = "采集未执行：离线模式（请到「设置 → 运行模式」开启在线模式）"
        log(STATE["message"])
        return {"added": 0, "error": "离线模式：真实访问未开启"}
    if db.setting("collect_channel", "session") == "http":
        return _task_collect_http(platforms, kw, cities, pages)
    if db.setting("collect_channel", "session") == "session":
        return _task_collect_session(platforms, kw, cities, pages)
    total_new = 0
    todo = _preflight(platforms)
    if not todo:
        log("没有可用平台，任务结束")
        return {"added": 0, "error": "没有可用平台（未登录/冷却/开关关）"}
    if kw:
        log("指定关键词采集：" + "、".join(kw) +
            ("（城市：" + "、".join(cities) + "）" if cities else "（全部支持城市）"))
    for p in todo:
        try:
            if db.quota_left(p, "collect", _collect_cap(p)) <= 0:
                log(f"{config.PLATFORM_NAMES[p]} 今日采集轮次已用完，跳过")
                continue
            combos, cl = _combos_for(p, kw if p != "liepin" else None, cities)
            if p in ("zhaopin", "boss"):
                cmap = citycfg.map_zl() if p == "zhaopin" else citycfg.map_boss()
                miss = [c for c in (cl or []) if not cmap.get(c)]
                if miss:
                    log(f"{config.PLATFORM_NAMES[p]} 缺少城市码，跳过：{'、'.join(miss)}")
            n_combos = max(1, len(combos) if p != "liepin" else 1)
            pages_p = int(pages) if pages else planmod.budget(p, n_combos)
            if p == "boss":
                pages_p = min(pages_p, int(db.setting(
                    "rate_boss_pages_per_round", config.RATE["boss"]["pages_per_round"])))
            log(f"{config.PLATFORM_NAMES[p]} 计划：{n_combos} 组合 × {pages_p} 页（慢速）")
            bot.pacer.wait(p)

            def _do(rt, p=p, combos=combos, pages_p=pages_p, cl=cl):
                if p == "boss":
                    return boss_api.collect(rt, log, combos, pages_p)
                if p == "zhaopin":
                    return zhaopin.collect(rt, log, combos, pages_p)
                return liepin.collect(rt, log, cl, min(3, pages_p))
            res = bot.call(_do)
            db.quota_add(p, "collect")
            total_new += res.get("added", 0)
            log(f"{config.PLATFORM_NAMES[p]} 采集完成：新增 {res.get('added', 0)}，"
                f"重复 {res.get('dup', 0)}")
            # 每个平台采完立刻判定，后续平台异常不影响已完成部分
            _apply_rules_to_new()
            _ai_score_new(p)
        except (Blocked, LaunchError, LiveDisabled):
            raise                                  # 风控/启动失败/离线：整单刹停
        except Exception as e:
            log(f"!! {config.PLATFORM_NAMES[p]} 采集异常，已跳过该平台继续其余："
                f"{str(e).splitlines()[0][:120]}")
    log(f"本轮共新增 {total_new} 条（规则+AI 已判定）")
    return {"added": total_new, "platforms": todo,
            "channel": "browser", "note": "明细见运行日志"}


def _combos_for_http(platform, kw=None, cities=None):
    """HTTP 回放用的 (keyword, city_code, city) 组合。

    - BOSS：内置模板按参数拼，城市码直接可用。
    - 智联：模板回放，关键词必换；城市码只有抓到锚点才能替换，否则保持抓包城市。
    - 猎聘：搜索接口按关键词，城市跟随抓包页面（dq 码未见公开字典）。
    """
    if platform == "boss":
        return _combos_for("boss", kw, cities)
    if platform == "liepin":
        rows = [r for r in db.plan_list(only_enabled=True)
                if r["platform"] == "liepin" and r["keyword"]]
        kws = kw or [r["keyword"] for r in rows] or ["IT经理"]
        return [(k, None, "") for k in dict.fromkeys(kws)][:6], []
    rows = [r for r in db.plan_list(only_enabled=True)
            if r["platform"] == "zhaopin" and r["keyword"]]
    kws = kw or [r["keyword"] for r in rows] \
        or config.COLLECT_KEYWORDS["zhaopin"]
    cl = cities or citycfg.names()
    combos = [(k, citycfg.map_zl().get(c), c) for c in cl for k in kws
              if citycfg.map_zl().get(c)]
    tpl = api_tpl.get("zhaopin")
    if tpl and not (tpl.get("anchor") or {}).get("city_code") and len(cl) > 1:
        combos = [(k, None, cl[0]) for k in kws]   # 模板没城市锚点，别假换城市
    return combos, cl


def _task_collect_session(platforms, kw=None, cities=None, pages=None):
    """会话内接口采集：窗口移到屏幕外，页面自己发请求，用户零操作、无需点击。"""
    todo = _preflight(platforms)
    if not todo:
        log("没有可用平台，任务结束")
        return {"added": 0, "error": "没有可用平台（未登录/冷却/开关关）"}
    if kw:
        log("指定关键词采集：" + "、".join(kw) +
            ("（城市：" + "、".join(cities) + "）" if cities else "（全部支持城市）"))
    total_new = 0
    for p in todo:
        try:
            if db.quota_left(p, "collect", _collect_cap(p)) <= 0:
                log(f"{config.PLATFORM_NAMES[p]} 今日采集轮次已用完，跳过")
                continue
            if p == "liepin":          # 猎聘：按关键词搜（推荐流模式下城市跟随页面）
                kws = kw or [r["keyword"] for r in db.plan_list(only_enabled=True)
                             if r["platform"] == "liepin" and r["keyword"]] or ["IT经理"]
                combos, cl = [(k, None, "") for k in kws], (cities or [])
            else:
                combos, cl = _combos_for(p, kw, cities)
            if p in ("zhaopin", "boss"):
                cmap = citycfg.map_zl() if p == "zhaopin" else citycfg.map_boss()
                miss = [c for c in (cl or []) if not cmap.get(c)]
                if miss:
                    log(f"{config.PLATFORM_NAMES[p]} 缺少城市码，跳过：{'、'.join(miss)}")
            n_combos = max(1, len(combos))
            pages_p = int(pages) if pages else planmod.budget(p, n_combos)
            if p == "boss":
                pages_p = min(pages_p, int(db.setting(
                    "rate_boss_pages_per_round",
                    config.RATE["boss"]["pages_per_round"])))
            log(f"{config.PLATFORM_NAMES[p]} 会话内采集：{n_combos} 组合 × "
                f"{pages_p} 页（窗口已移到屏幕外，无需操作）")
            bot.pacer.wait(p)

            def _do(rt, p=p, combos=combos, pages_p=pages_p, cl=cl):
                rt.hide()
                try:
                    if p == "boss":
                        return session_api.collect_boss(rt, log, combos, pages_p)
                    return session_api.collect_via_page(rt, log, p, combos, pages_p)
                finally:
                    rt.show()

            res = bot.call(_do)
            db.quota_add(p, "collect")
            total_new += res.get("added", 0)
            log(f"{config.PLATFORM_NAMES[p]} 采集完成：新增 "
                f"{res.get('added', 0)}，重复 {res.get('dup', 0)}")
            _apply_rules_to_new()
            _ai_score_new(p)
        except (Blocked, LaunchError, LiveDisabled, risk.Cooldown):
            raise                                  # 风控/启动失败/离线：整单刹停
        except Exception as e:
            log(f"!! {config.PLATFORM_NAMES[p]} 采集异常，已跳过该平台继续其余："
                f"{str(e).splitlines()[0][:120]}")
    log(f"本轮共新增 {total_new} 条（规则+AI 已判定）")
    return {"added": total_new, "platforms": todo,
            "channel": "session", "note": "明细见运行日志"}


def _task_collect_http(platforms, kw=None, cities=None, pages=None):
    todo = []
    for p in platforms:
        try:
            risk.check(p)                 # 统一判断：开关 + 冷却
            todo.append(p)
        except risk.Cooldown as e:
            log(f"跳过 {config.PLATFORM_NAMES[p]}：{e}")
    if not todo:
        log("没有可用平台（HTTP 通道），任务结束")
        return {"added": 0, "error": "没有可用平台（HTTP 通道）"}
    if kw:
        log("HTTP 指定关键词采集：" + "、".join(kw) +
            ("（城市：" + "、".join(cities) + "）" if cities else "（全部支持城市）"))
    total_new = 0
    for p in todo:
        try:
            if db.quota_left(p, "collect", _collect_cap(p)) <= 0:
                log(f"{config.PLATFORM_NAMES[p]} 今日采集轮次已用完，跳过")
                continue
            combos, _cl = _combos_for_http(p, kw, cities)
            if p != "boss" and api_tpl.get(p) is None:
                log(f"!! {config.PLATFORM_NAMES[p]} 还没有 HTTP 模板"
                    "（设置页「抓包」一次即可），跳过该平台")
                continue
            n_combos = max(1, len(combos))
            pages_p = int(pages) if pages else min(
                3, planmod.budget(p, n_combos))    # HTTP 通道首轮从紧
            log(f"{config.PLATFORM_NAMES[p]} HTTP 计划：{n_combos} 组合 × "
                f"{pages_p} 页（限速 {netio.browser_interval(p):.0f}s 起）")
            res = http_collect.collect_http(p, log, combos, pages_p)
            db.quota_add(p, "collect")
            total_new += res.get("added", 0)
            log(f"{config.PLATFORM_NAMES[p]} HTTP 采集完成：新增 "
                f"{res.get('added', 0)}，重复 {res.get('dup', 0)}")
            _apply_rules_to_new()
            _ai_score_new(p)
        except (Blocked, LiveDisabled, risk.Cooldown):
            raise                                  # 风控/离线：整单刹停
        except http_collect.NoTpl as e:
            log(f"!! {config.PLATFORM_NAMES[p]}：{e}（跳过该平台）")
        except Exception as e:
            log(f"!! {config.PLATFORM_NAMES[p]} HTTP 采集异常，已跳过该平台继续其余："
                f"{str(e).splitlines()[0][:120]}")
    log(f"本轮（HTTP 直连）共新增 {total_new} 条（规则+AI 已判定）")
    return {"added": total_new, "platforms": todo,
            "channel": "http", "note": "明细见运行日志"}


def _apply_rules_to_new():
    """新入库/未判定的行跑规则引擎。"""
    floor = int(db.setting("salary_floor", config.SALARY_FLOOR))
    for j in db.list_jobs(status="new"):
        if db.blacklist_has(j.get("company") or ""):
            db.set_job_status(j["id"], "不投-黑名单", "公司在黑名单")
            continue
        st, why = rules.verdict({"title": j["title"], "city": j["city"],
                                 "salary": j["salary_text"], "edu": j["edu"],
                                 "exp": j["exp"]}, floor=floor)
        tier, sc = rules.score(j["title"], j["city"], j["salary_text"], floor=floor)
        with db.connect() as con:
            con.execute("UPDATE jobs SET status=?, reason=?, tier=?, score=?, "
                        "salary_cap=? WHERE id=?",
                        (st, why, tier, sc, rules.salary_cap(j["salary_text"]),
                         j["id"]))


def _task_apply(platforms, limit, job_ids=None):
    if not _live():
        STATE["message"] = "投递未执行：离线模式（请到「设置 → 运行模式」开启在线模式）"
        log(STATE["message"])
        return
    applied = skipped = 0
    # 通道分流：会话内接口（默认，不点按钮）/ 纯 HTTP（BOSS 单次）/ 浏览器点击
    sess = [p for p in platforms if _apply_channel(p) == "session"]
    brow = [p for p in platforms if _apply_channel(p) == "browser"]
    todo = _preflight(brow) if brow else []
    for p in sess:
        if p in ("zhaopin", "liepin"):
            log(f"{config.PLATFORM_NAMES[p]} 暂未开放接口投递："
                "请在岗位库点「打开」手动投递（该平台投递接口需页面签名，未做逆向）")
            continue
        try:
            risk.check("boss")                 # 统一：开关 + 冷却
            todo.append("boss")
        except risk.Cooldown as e:
            log(f"跳过 BOSS（投递）：{e}")
    if not todo:
        log("没有可用平台，任务结束")
        return
    floor = int(db.setting("salary_floor", config.SALARY_FLOOR))
    for p in todo:
        use_session = _apply_channel(p) == "session"
        use_http = (not use_session) and _apply_channel(p) == "http"
        if job_ids:
            picked = [j for j in db.list_jobs(p) if j["id"] in set(job_ids.get(p, []))]
        else:
            picked = [j for j in db.list_jobs(p, "待投")
                      if (j["salary_cap"] or 0) >= floor or
                      j["status"].startswith("保留")]
        cap = int(limit)
        if p == "boss":
            cap = min(cap, int(db.setting("rate_boss_daily_apply",
                                          config.RATE["boss"]["daily_apply"])),
                      db.quota_left("boss", "apply",
                                    int(db.setting("rate_boss_daily_apply",
                                                   config.RATE["boss"]["daily_apply"]))))
        picked = picked[:max(1, cap)]
        log(f"{config.PLATFORM_NAMES[p]} 本次计划投递 {len(picked)} 条")
        STATE["total"] += len(picked)
        for j in picked:
            if not bot.pacer.can(p, "apply", config.RATE[p]["daily_apply"]):
                log(f"{config.PLATFORM_NAMES[p]} 触发当日上限，停止")
                break
            if use_session:
                if p == "boss" and not (j.get("security_id") and j.get("ext_id")):
                    skipped += 1
                    db.set_job_status(j["id"], "待定-链接过期",
                                      "缺新鲜 securityId，重新采集后可投")
                    continue
                log(f"→ BOSS 投递（会话内接口）{j['title']} / {j['company']}")
                try:
                    ok, msg = bot.call(
                        lambda rt, j=j: session_api.boss_greet(rt, j, log))
                except (Blocked, LiveDisabled, risk.Cooldown) as e:
                    log(f"BOSS 立即停止：{e}")
                    db.log_app(j["id"], p, "apply", False, f"风控中止：{e}")
                    break
                except Exception as e:
                    m0 = str(e).splitlines()[0][:110]
                    log(f"   ! 投递异常「{j['title']}」：{m0}（标记待复查，继续下一条）")
                    db.log_app(j["id"], p, "apply", False, f"异常：{m0}")
                    db.set_job_status(j["id"], "待复查", f"投递异常：{m0}")
                    skipped += 1
                    STATE["done"] += 1
                    continue
                if "上限" in (msg or ""):
                    log(f"BOSS 当日沟通上限：{msg}，停止该平台")
                    break
            elif use_http:
                if not (j.get("security_id") and j.get("ext_id")):
                    skipped += 1
                    db.set_job_status(j["id"], "待定-链接过期",
                                      "缺新鲜 securityId，重新采集后可投")
                    continue
                log(f"→ BOSS 投递（HTTP 直连）{j['title']} / {j['company']}")
                try:
                    ok, msg = http_apply.boss_greet(j)
                except (Blocked, LiveDisabled, risk.Cooldown) as e:
                    log(f"BOSS 立即停止：{e}")
                    db.log_app(j["id"], p, "apply", False, f"风控中止：{e}")
                    break
                except Exception as e:
                    m0 = str(e).splitlines()[0][:110]
                    log(f"   ! 投递异常「{j['title']}」：{m0}（标记待复查，继续下一条）")
                    db.log_app(j["id"], p, "apply", False, f"异常：{m0}")
                    db.set_job_status(j["id"], "待复查", f"投递异常：{m0}")
                    skipped += 1
                    STATE["done"] += 1
                    continue
                if "上限" in (msg or ""):
                    log(f"BOSS 当日沟通上限：{msg}，停止该平台")
                    break
            else:
                bot.pacer.wait(p)
                url = boss_api.detail_url(j) if p == "boss" else j["url"]
                if p == "boss" and not url:
                    skipped += 1
                    db.set_job_status(j["id"], "待定-链接过期", "缺 securityId，重新采集后可投")
                    continue
                log(f"→ {config.PLATFORM_NAMES[p]} 投递 {j['title']} / {j['company']}")
                try:
                    def _go(rt, p=p, url=url):
                        if p == "zhaopin":
                            return zhaopin.apply(rt, url, log)
                        if p == "liepin":
                            return liepin.apply(rt, url, log)
                        return boss_api.apply_greet(rt, url, log)
                    ok, msg = bot.call(_go)
                except (Blocked, risk.Cooldown) as e:
                    log(f"{config.PLATFORM_NAMES[p]} 立即停止：{e}")
                    db.log_app(j["id"], p, "apply", False, f"风控中止：{e}")
                    break
                except Exception as e:
                    m0 = str(e).splitlines()[0][:110]
                    log(f"   ! 投递异常「{j['title']}」：{m0}（标记待复查，继续下一条）")
                    db.log_app(j["id"], p, "apply", False, f"异常：{m0}")
                    db.set_job_status(j["id"], "待复查", f"投递异常：{m0}")
                    skipped += 1
                    STATE["done"] += 1
                    continue
            db.log_app(j["id"], p, "apply", ok, msg)
            if ok:
                bot.pacer.use(p, "apply")
                db.set_job_status(j["id"], "已投", f"{datetime.now():%Y-%m-%d} {msg}")
                db.pipeline_mark_applied(j["id"], f"{datetime.now():%Y-%m-%d} 自动投递")
                applied += 1
                log(f"   ✓ {msg}")
            else:
                skipped += 1
                if msg in ("已投过", "已沟通", "已打招呼"):
                    db.set_job_status(j["id"], "已投", msg)
                    db.pipeline_mark_applied(j["id"])
                log(f"   × {msg}")
            STATE["done"] += 1
    log(f"投递结束：成功 {applied}，未成/跳过 {skipped}")


# ---------------- 路由：基础 ----------------

@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/state")
def api_state():
    since = int(request.args.get("since", 0))
    return jsonify({
        "state": STATE, "logs": list(LOGS)[since:], "log_total": len(LOGS),
        "stats": db.job_stats(),
        "quota": {p: {"apply": db.quota_left(
            p, "apply", config.RATE[p]["daily_apply"])} for p in config.PLATFORM_NAMES},
        "risk": risk.status(), "live": _live(),
        "initialized": db.wizard_get("initialized", "0") == "1",
        "floor": int(db.setting("salary_floor", config.SALARY_FLOOR)),
    })


@app.get("/api/settings")
def api_settings_get():
    keys = ["allow_live", "salary_floor", "boss_enabled", "blacklist_learn_enabled",
            "cleanse_enabled", "followup_enabled", "cooldown_minutes",
            "followup_daily_cap", "cleanse_daily_cap", "followup_min_interval_h"] + \
           [f"rate_{p}_{k}" for p in config.RATE for k in
            ("interval", "daily_apply", "daily_api", "pages_per_round")]
    return jsonify({"settings": {k: db.setting(k) for k in keys if db.setting(k) is not None},
                    "defaults": {"salary_floor": config.SALARY_FLOOR,
                                 "rate": config.RATE},
                    "env_live": os.environ.get("JOBPILOT_ALLOW_LIVE") == "1"})


@app.post("/api/settings")
def api_settings_save():
    d = request.json or {}
    if "allow_live" in d and (STATE["running"] or agent.ST["running"]):
        return jsonify({"ok": False,
                        "error": "有任务在跑，先等结束再切换运行模式"}), 409
    saved = {}
    for k, v in d.items():
        if k == "allow_live":
            try:
                v = 1 if int(v) else 0
            except (TypeError, ValueError):
                continue
            db.set_setting("allow_live", v)
            saved[k] = v
            log("运行模式已切换为" + ("在线（真实访问三平台）" if v else "离线（不访问平台）"))
            continue
        if k.startswith("rate_") or k in (
                "salary_floor", "cooldown_minutes", "blacklist_learn_enabled",
                "cleanse_enabled", "followup_enabled", "followup_daily_cap",
                "cleanse_daily_cap", "followup_min_interval_h"):
            db.set_setting(k, v)
            saved[k] = v
    if "boss_enabled" in d:
        risk.toggle_boss(bool(int(d["boss_enabled"])))
        saved["boss_enabled"] = int(d["boss_enabled"])
    log("参数已保存：" + "，".join(f"{k}={v}" for k, v in list(saved.items())[:6]))
    return jsonify({"ok": True, "saved": saved})


@app.get("/api/cities")
def api_cities_get():
    return jsonify({"names": citycfg.names(), "status": citycfg.status()})


@app.post("/api/cities")
def api_cities_save():
    """保存支持城市：只传城市名，城市码由系统自动匹配。

    BOSS：直接拉官方公开城市表（无需登录）。
    智联：需要已登录浏览器，走平台搜索页自身的 city 接口，在线模式且无任务时自动抓一次。
    """
    d = request.json or {}
    items = d.get("names") or d.get("rows")      # 兼容旧格式
    if not isinstance(items, list):
        return jsonify({"ok": False, "error": "names 必须是列表"}), 400
    try:
        cleaned = citycfg.save(items)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    log("支持城市已更新：" + "、".join(cleaned))

    zlmsg = ""
    need = [n for n in cleaned if not citycfg.map_zl().get(n)]
    if need:
        if not _live():
            zlmsg = "离线模式：智联城市码未自动匹配（切到在线模式后重新保存即可）"
        elif STATE["running"] or agent.ST["running"] or agent.ST["pending"]:
            zlmsg = "有任务正在运行：本次未自动匹配智联城市码，稍后重新保存即可"
        else:
            try:
                log("正在从智联搜索接口匹配城市码：" + "、".join(need))
                mapping = bot.call(city_api.fetch_zhaopin_cities)
                citycfg.save_zl_codes(mapping)
                left = [n for n in cleaned if not citycfg.map_zl().get(n)]
                log(f"智联城市码匹配完成，缓存 {len(mapping)} 个"
                    + (f"，仍缺：{'、'.join(left)}" if left else ""))
                if left:
                    zlmsg = "智联未收录这些城市：" + "、".join(left)
            except Blocked as e:
                zlmsg = f"智联城市码未匹配：{e}（请先在「设置 → 账号连接」用专用窗口登录智联后重新保存）"
                log(zlmsg)
            except Exception as e:
                zlmsg = f"智联城市码未匹配：{str(e).splitlines()[0][:120]}"
                log(zlmsg)
    return jsonify({"ok": True, "names": cleaned, "status": citycfg.status(),
                    "zl_msg": zlmsg})


@app.post("/api/boss/toggle")
def api_boss_toggle():
    on = bool((request.json or {}).get("on"))
    risk.toggle_boss(on)
    return jsonify({"ok": True, "boss_enabled": int(on), "risk": risk.status()})


@app.post("/api/platform/toggle")
def api_platform_toggle():
    """三平台统一开关（设置页复选框用这一个接口）。"""
    d = request.get_json(silent=True) or {}
    p = d.get("platform")
    if p not in config.PLATFORM_NAMES:
        return jsonify({"ok": False, "error": "平台不正确"}), 400
    on = bool(d.get("on"))
    risk.set_platform(p, on)
    log(f"{config.PLATFORM_NAMES[p]} 自动访问已{'开启' if on else '暂停'}")
    return jsonify({"ok": True, "risk": risk.status(),
                    "platform": p, "on": on})


# ---------------- 路由：HTTP 直连（netio/模板/Cookie） ----------------

@app.get("/api/net")
def api_net():
    ck = cookies.status()
    return jsonify({
        "live": _live(),
        "channel": {
            "collect": db.setting("collect_channel", "session"),
            "apply": {p: _apply_channel(p) for p in config.PLATFORM_NAMES}},
        "templates": [{k: t[k] for k in
                       ("id", "platform", "kind", "url", "method", "saved_at",
                        "note", "anchor")}
                      for t in db.tpl_list()],
        "builtins": ["boss(search)"],
        "cookies": {p: {"pasted": bool((db.setting(f"cookie_raw_{p}", "") or "")
                                       .strip()),
                        "blob": (ck.get(p) or {}).get("count", 0)}
                    for p in config.PLATFORM_NAMES},
    })


@app.post("/api/net/channel")
def api_net_channel():
    d = request.json or {}
    if "collect" in d:
        v = d["collect"]
        db.set_setting("collect_channel",
                       v if v in ("session", "http", "browser") else "session")
    for p, v in (d.get("apply") or {}).items():
        if p not in config.PLATFORM_NAMES:
            continue
        if v == "http" and p != "boss":
            return jsonify({"ok": False,
                            "error": f"{config.PLATFORM_NAMES[p]} 没有可用的纯接口投递端点"}), 400
        db.set_setting(f"apply_channel_{p}",
                       v if v in ("session", "http", "browser") else "session")
    log("通道已更新：采集=" + db.setting("collect_channel", "session") +
        "，BOSS投递=" + _apply_channel("boss"))
    return jsonify({"ok": True})


@app.post("/api/net/cookie")
def api_net_cookie():
    d = request.json or {}
    p = d.get("platform")
    if p not in config.PLATFORM_NAMES:
        return jsonify({"ok": False, "error": "平台不正确"}), 400
    raw = (d.get("raw") or "").strip()
    if not raw:
        db.set_setting(f"cookie_raw_{p}", "")
        log(f"{config.PLATFORM_NAMES[p]} 已清除粘贴的 Cookie")
        return jsonify({"ok": True, "count": 0})
    n = len(netio.parse_cookie_string(raw))
    if n < 2:
        return jsonify({"ok": False,
                        "error": "没解析出 Cookie（应为 name=value; name2=value2 格式）"}), 400
    db.set_setting(f"cookie_raw_{p}", raw)
    db.event("info", p, "cookie", f"粘贴 {n} 条 Cookie")
    log(f"{config.PLATFORM_NAMES[p]} Cookie 已粘贴保存（{n} 条）")
    return jsonify({"ok": True, "count": n})


@app.post("/api/net/test")
def api_net_test():
    p = (request.json or {}).get("platform")
    if p not in config.PLATFORM_NAMES:
        return jsonify({"ok": False, "error": "平台不正确"}), 400
    if not _live():
        return jsonify({"ok": False, "error": "离线模式：请到「设置 → 运行模式」开启在线模式"})
    if p == "boss" and not risk.boss_enabled():
        return jsonify({"ok": False, "error": "BOSS 自动访问开关为关，先勾选再测"})
    if STATE["running"]:
        return jsonify({"ok": False, "error": "任务运行中，稍后再测"}), 409
    try:
        ok, msg = http_apply.probe(p, log)
        db.event("info", p, "net_test", str(msg)[:120])
        return jsonify({"ok": ok, "msg": msg})
    except (Blocked, LiveDisabled, risk.Cooldown) as e:
        log(f"netio 测试 {config.PLATFORM_NAMES[p]} 刹停：{str(e)[:120]}")
        db.event("risk", p, "net_test", str(e)[:120])
        return jsonify({"ok": False, "error": str(e)[:160]})
    except Exception as e:
        m0 = str(e).splitlines()[0][:140]
        log(f"netio 测试 {config.PLATFORM_NAMES[p]} 异常：{m0}")
        return jsonify({"ok": False, "error": m0})


@app.post("/api/net/record")
def api_net_record():
    p = (request.json or {}).get("platform")
    if p not in ("zhaopin", "liepin"):
        return jsonify({"ok": False,
                        "error": "只有智联/猎聘需要抓包（BOSS 用内置模板）"}), 400
    if not _live():
        return jsonify({"started": False, "live": False,
                        "error": "离线模式：请到「设置 → 运行模式」开启在线模式"})
    def fn():
        bot.call(lambda rt: api_tpl.record(rt, log, p))
    started = _run_task("接口抓包", fn)
    return jsonify({"started": started, "live": True})


@app.post("/api/net/tpl")
def api_net_tpl_save():
    d = request.json or {}
    p = d.get("platform")
    url = (d.get("url") or "").strip()
    if p not in config.PLATFORM_NAMES or not url:
        return jsonify({"ok": False, "error": "平台或 URL 不正确"}), 400
    if url.split("/")[2] if "/" in url.replace("//", "") else "":
        host = urlsplit(url).netloc
        if not host.endswith(netio.DOMAIN.get(p, "")):
            return jsonify({"ok": False,
                            "error": f"URL 域名 {host} 不属于该平台"}), 400
    tpl = {"platform": p, "kind": d.get("kind") or "search", "url": url,
           "method": d.get("method") or "GET",
           "headers": d.get("headers") or {}, "body": d.get("body") or "",
           "anchor": d.get("anchor") or {}, "note": "手工录入"}
    db.tpl_save(tpl)
    log(f"模板已手工保存：{config.PLATFORM_NAMES[p]}")
    return jsonify({"ok": True})


@app.post("/api/net/tpl/delete")
def api_net_tpl_del():
    i = (request.json or {}).get("id")
    db.tpl_delete(int(i or 0))
    return jsonify({"ok": True})


@app.post("/api/net/connect")
def api_net_connect():
    """一键连接：检测日常浏览器登录态 + 采集窗口是否已同步（纯本地读库）。"""
    if STATE["running"]:
        return jsonify({"ok": False, "error": "任务运行中，稍后再试"}), 409
    if not _live():
        return jsonify({"ok": False,
                        "error": "离线模式：请到「设置 → 运行模式」开启在线模式"})
    try:
        st = connect.probe_all(log)
        return jsonify({"ok": True, "status": st})
    except Exception as e:
        m0 = str(e).splitlines()[0][:140]
        log(f"一键连接异常：{m0}")
        return jsonify({"ok": False, "error": m0})


@app.post("/api/net/sync")
def api_net_sync():
    """把你的浏览器登录态注入采集专用窗口（只走平台首页，不开搜索/详情）。"""
    d = request.get_json(silent=True) or {}
    plats = d.get("platforms") or list(config.PLATFORM_NAMES)
    if STATE["running"] or agent.ST["running"]:
        return jsonify({"ok": False, "error": "有任务在跑，稍后再同步"}), 409
    if not _live():
        return jsonify({"ok": False, "error": "离线模式：请到「设置 → 运行模式」开启在线模式"})
    log(f"开始同步登录态到采集窗口：{'、'.join(config.PLATFORM_NAMES[p] for p in plats if p in config.PLATFORM_NAMES)}")
    try:
        res, src = connect.sync_to_window([p for p in plats
                                           if p in config.PLATFORM_NAMES], log)
        db.event("settings", "", "sync_login", str(res)[:200])
        return jsonify({"ok": True, "results": res, "source": src})
    except Exception as e:
        m0 = str(e).splitlines()[0][:140]
        log(f"同步登录态异常：{m0}")
        return jsonify({"ok": False, "error": m0})


@app.get("/api/net/window_cookies")
def api_net_window_cookies():
    """诊断：读采集窗口当前真实持有的 Cookie（不导航，零风险）。"""
    p = request.args.get("platform") or "zhaopin"
    if p not in config.PLATFORM_NAMES:
        return jsonify({"error": "平台不正确"}), 400
    dom = cookie_extract.PLATFORM_DOMAINS[p]
    try:
        def _read(rt):
            if rt.d is None:
                return {"alive": False, "note": "浏览器未启动"}
            try:
                cur = rt.d.current_url or ""
            except Exception:
                cur = ""
            cks = rt.d.get_cookies() or []
            mine = [c for c in cks if any((c.get("domain") or "").endswith(d)
                                          for d in dom)]
            names = sorted({c["name"] for c in mine})
            keys = cookie_extract.WEB_LOGIN_KEYS.get(p, ())
            return {"alive": True, "url": cur[:80], "count": len(names),
                    "names": names,
                    "has_login_key": [k for k in keys if k in names],
                    "missing_key": [k for k in keys if k not in names]}
        return jsonify(bot.call(_read))
    except Exception as e:
        return jsonify({"error": str(e).splitlines()[0][:140]})


@app.post("/api/net/login")
def api_net_login():
    """打开可见窗口供扫码登录（唯一需要人工的步骤），成功后自动刷新 Cookie。"""
    p = (request.json or {}).get("platform")
    if p not in config.PLATFORM_NAMES:
        return jsonify({"ok": False, "error": "平台不正确"}), 400
    if STATE["running"]:
        return jsonify({"ok": False, "error": "任务运行中"}), 409
    try:
        ok, msg = connect.login_window(p)
        log(f"登录窗口 {config.PLATFORM_NAMES[p]}：{msg}")
        return jsonify({"ok": ok, "msg": msg})
    except Exception as e:
        m0 = str(e).splitlines()[0][:140]
        return jsonify({"ok": False, "error": m0})


@app.post("/api/net/autorecord")
def api_net_autorecord():
    """自动录制接口模板（已登录状态下用户零操作）。"""
    p = (request.json or {}).get("platform")
    if p not in config.PLATFORM_NAMES:
        return jsonify({"ok": False, "error": "平台不正确"}), 400
    if STATE["running"]:
        return jsonify({"ok": False, "error": "任务运行中"}), 409
    if p == "boss":
        return jsonify({"ok": True, "msg": "BOSS 用内置接口，无需录制"})
    try:
        ok, msg = connect.auto_record(p)
        log(f"自动录制 {config.PLATFORM_NAMES[p]}：{msg}")
        return jsonify({"ok": ok, "msg": msg, "error": "" if ok else msg})
    except Exception as e:
        m0 = str(e).splitlines()[0][:140]
        return jsonify({"ok": False, "error": m0, "msg": m0})


@app.get("/api/events")
def api_events():
    return jsonify({"events": db.events(200, request.args.get("level") or None,
                                        request.args.get("platform") or None)})


# ---------------- 路由：登录 / Cookie ----------------

@app.post("/api/precheck")
def api_precheck():
    if STATE["running"]:
        return jsonify({"ok": False, "error": "任务运行中"}), 409
    plats = (request.json or {}).get("platforms") or list(config.PLATFORM_NAMES)
    st = _check_login(plats)
    for p, ok in st.items():
        tag = {True: "已登录", False: "未登录", "off": "自动访问已关闭",
               "cooldown": "冷却中"}.get(ok, "检测失败")
        log(f"登录检测 {config.PLATFORM_NAMES[p]}：{tag}")
    return jsonify({"ok": True, "status": st})


@app.post("/api/open")
def api_open():
    p = (request.json or {}).get("platform")
    if p not in config.LOGIN_URLS:
        return jsonify({"ok": False, "hint": "平台不正确"}), 400
    if p == "boss" and not risk.boss_enabled():
        return jsonify({"ok": False, "hint": "BOSS 自动访问未开启（先勾选开关）"})
    try:
        ok, _ = bot.call(_probe_login, config.LOGIN_URLS[p], p)
        return jsonify({"ok": bool(ok),
                        "hint": "" if ok else "浏览器已打开，请在窗口里登录后再验证"})
    except risk.Cooldown as e:
        return jsonify({"ok": False, "hint": str(e)})
    except Exception as e:
        return jsonify({"ok": False, "hint": str(e)[:150]})


@app.post("/api/risk/clear")
def api_risk_clear():
    p = (request.json or {}).get("platform")
    if p not in config.PLATFORM_NAMES:
        return jsonify({"ok": False}), 400
    risk.clear(p)
    log(f"{config.PLATFORM_NAMES[p]} 冷却已解除、开关已恢复")
    return jsonify({"ok": True, "risk": risk.status()})


@app.post("/api/risk/enable")
def api_risk_enable():
    """只恢复被自动保护关停的平台开关（不动冷却）。"""
    p = (request.json or {}).get("platform")
    if p not in config.PLATFORM_NAMES:
        return jsonify({"ok": False, "error": "平台不正确"}), 400
    risk.enable(p)
    log(f"{config.PLATFORM_NAMES[p]} 平台开关已手动恢复")
    return jsonify({"ok": True, "risk": risk.status()})


@app.get("/api/cookies")
def api_cookies_status():
    return jsonify({"status": cookies.status()})


@app.post("/api/cookies/export")
def api_cookies_export():
    if STATE["running"]:
        return jsonify({"ok": False, "error": "任务运行中"}), 409
    try:
        blob = cookies.dump(bot, log)
        return jsonify({"ok": True,
                        "counts": {p: len(v["cookies"]) for p, v in blob.items()}})
    except LiveDisabled as e:
        return jsonify({"ok": False, "error": str(e)}), 409
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:150]})


@app.post("/api/cookies/restore")
def api_cookies_restore():
    if STATE["running"]:
        return jsonify({"ok": False, "error": "任务运行中"}), 409
    try:
        out, err = cookies.restore(bot, log, (request.json or {}).get("platforms"))
        if err:
            return jsonify({"ok": False, "error": err})
        return jsonify({"ok": True, "results": out})
    except LiveDisabled as e:
        return jsonify({"ok": False, "error": str(e)}), 409
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:150]})


# ---------------- 路由：任务触发 ----------------

@app.post("/api/collect")
def api_collect():
    d = request.json or {}
    plats = d.get("platforms") or list(config.PLATFORM_NAMES)

    def _lst(v):
        if isinstance(v, str):
            return [x.strip() for x in v.replace("，", ",").split(",") if x.strip()]
        return [str(x).strip() for x in (v or []) if str(x).strip()]

    kw, cities = _lst(d.get("keywords")), _lst(d.get("cities"))
    pages = int(d.get("pages") or 0) or None
    bad = [c for c in cities if c not in citycfg.names()]
    if bad:
        return jsonify({"started": False,
                        "error": f"不支持的城市：{'、'.join(bad)}"}), 400
    started = _run_task("采集", lambda: _task_collect(plats, kw or None,
                                                      cities or None, pages))
    return jsonify({"started": started, "live": _live()})


@app.post("/api/apply")
def api_apply():
    d = request.json or {}
    plats = d.get("platforms") or list(config.PLATFORM_NAMES)
    limit = int(d.get("limit") or 5)
    rows = d.get("job_ids")
    started = _run_task("投递", lambda: _task_apply(plats, limit, rows))
    return jsonify({"started": started, "live": _live()})


@app.post("/api/checkjobs")
def api_checkjobs():
    plats = (request.json or {}).get("platforms") or list(config.PLATFORM_NAMES)
    started = _run_task("有效性检测", lambda: _task_check(plats))
    return jsonify({"started": started})


def _task_check(plats):
    if not _live():
        STATE["message"] = "检测未执行：离线模式（请到「设置 → 运行模式」开启在线模式）"
        log(STATE["message"])
        return
    fixed = unknown = okn = 0
    todo = _preflight(plats)
    for p in todo:
        jobs = [j for j in db.list_jobs(p)
                if (j["status"] or "").startswith(("待投", "待定", "保留"))]
        log(f"{config.PLATFORM_NAMES[p]} 待检测 {len(jobs)} 条")
        STATE["total"] += len(jobs)
        for j in jobs:
            bot.pacer.wait(p)
            url = boss_api.detail_url(j) if p == "boss" else j["url"]
            try:
                def _rd(rt, p=p, url=url):
                    if p == "boss":
                        return boss_api.read_jd(rt, url)
                    if p == "zhaopin":
                        return zhaopin.read_jd(rt, url)
                    return liepin.read_jd(rt, url)
                text, err = bot.call(_rd)
            except (Blocked, risk.Cooldown) as e:
                log(f"{config.PLATFORM_NAMES[p]} 停止检测：{e}")
                break
            STATE["done"] += 1
            if text:
                db.save_jd(j["id"], text)
                okn += 1
            elif err and ("过期" in err or "关闭" in err or "不存在" in err):
                db.set_job_status(j["id"], "已关闭", err)
                fixed += 1
                log(f"   × 失效 {j['title']}/{j['company']}（{err}）")
            else:
                unknown += 1
                log(f"   ? 无法判断 {j['title']}（{err}）")
    log(f"有效性检测完成：有效 {okn}，失效 {fixed}，无法判断 {unknown}")


@app.post("/api/jobs/purge")
def api_purge():
    n = db.delete_jobs("已关闭", (request.json or {}).get("platform"))
    log(f"已清除失效岗位 {n} 条")
    return jsonify({"ok": True, "removed": n})


@app.post("/api/jobs/purge_rejected")
def api_purge_rejected():
    """一键清除所有「不投」记录（不投-薪资低/不投-硬门槛/不投-人工等）。"""
    p = (request.json or {}).get("platform")
    n = db.delete_jobs_by_prefix("不投", p)
    log(f"已清除「不投」岗位 {n} 条（只删本地库记录，不影响平台）")
    return jsonify({"ok": True, "removed": n})


# ---------------- 路由：岗位 / JD / 定制 ----------------

@app.get("/api/jobs")
def api_jobs():
    p = request.args.get("platform") or None
    rows = db.list_jobs_full(p)          # 带跟进阶段 p_stage/p_note
    return jsonify({"jobs": rows})


@app.post("/api/jobs/ai")
def api_jobs_ai():
    """手动对未判定的放行岗位补跑 AI 匹配度（采集任务里已自动做，这里兜底）。"""
    p = (request.json or {}).get("platform")
    try:
        n = _ai_score_new(p or None)
        return jsonify({"ok": True, "scored": n})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:150]})


@app.post("/api/ai/review_plan")
def api_ai_review_plan():
    """AI 评估采集计划：砍掉的行自动禁用，改写建议写进理由。"""
    rows = db.plan_list()
    if not rows:
        return jsonify({"ok": False, "error": "先让 AI 生成或手工添加计划"})
    try:
        items = ai.review_plan(rows)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:180]})
    changed = 0
    for it in items:
        idx = it.get("index")
        if idx is None or not (0 <= int(idx) < len(rows)):
            continue
        r = rows[int(idx)]
        v = str(it.get("verdict") or "")
        why = str(it.get("why") or "")[:80]
        if v == "砍掉":
            db.plan_set(r["id"], enabled=0, reason=f"AI砍掉：{why}")
        elif v == "改写":
            sug = str(it.get("suggest_keyword") or "").strip()[:12]
            db.plan_set(r["id"], enabled=0,
                        reason=f"AI建议改写为「{sug}」：{why}")
        else:
            db.plan_set(r["id"], reason=f"AI保留：{why}")
        changed += 1
    log(f"AI 评估计划：{changed} 行已标注（砍掉/改写行自动禁用）")
    return jsonify({"ok": True, "reviewed": changed})


@app.post("/api/job")
def api_job():
    d = request.json or {}
    db.set_job_status(int(d["id"]), d["status"], d.get("reason", ""))
    return jsonify({"ok": True})


@app.post("/api/job/jd")
def api_job_jd():
    d = request.json or {}
    j = db.get_job(int(d.get("id") or 0))
    if not j:
        return jsonify({"ok": False, "error": "找不到岗位"}), 404
    if STATE["running"]:
        return jsonify({"ok": False, "error": "任务运行中"}), 409
    p = j["platform"]
    if p == "boss" and not risk.boss_enabled():
        return jsonify({"ok": False, "error": "BOSS 自动访问未开启"})
    try:
        risk.check(p)
    except risk.Cooldown as e:
        return jsonify({"ok": False, "error": str(e)})
    url = boss_api.detail_url(j) if p == "boss" else j["url"]
    if p == "boss" and not url:
        return jsonify({"ok": False, "error": "缺 securityId（旧裸链），重新采集后再试"})
    try:
        def _rd(rt):
            if p == "boss":
                return boss_api.read_jd(rt, url)
            if p == "zhaopin":
                return zhaopin.read_jd(rt, url)
            return liepin.read_jd(rt, url)
        text, err = bot.call(_rd)
        if err:
            return jsonify({"ok": False, "error": err})
        db.save_jd(j["id"], text)
        return jsonify({"ok": True, "jd": text[:6000], "title": j["title"],
                        "company": j["company"]})
    except LiveDisabled as e:
        return jsonify({"ok": False, "error": str(e)}), 409
    except Exception as e:
        return jsonify({"ok": False, "error": str(e).splitlines()[0][:150]})


def _read_jd_job(j):
    """在专用浏览器里读岗位 JD（与采集/投递同一登录态）。"""
    p = j["platform"]
    url = boss_api.detail_url(j) if p == "boss" else j.get("url")
    if not url:
        return "", "缺少岗位链接（重新采集后再试）"

    def _rd(rt):
        if p == "boss":
            return boss_api.read_jd(rt, url)
        if p == "zhaopin":
            return zhaopin.read_jd(rt, url)
        return liepin.read_jd(rt, url)
    return bot.call(_rd)


@app.post("/api/job/open")
def api_job_open():
    """在自动化专用浏览器窗口打开岗位详情（与采集/投递同一登录态）。"""
    j = db.get_job(int((request.json or {}).get("id") or 0))
    if not j:
        return jsonify({"ok": False, "error": "找不到岗位"}), 404
    url = boss_api.detail_url(j) if j["platform"] == "boss" else j.get("url")
    if not url:
        return jsonify({"ok": False, "error": "缺少岗位链接，重新采集后再试"})
    try:
        bot.call(lambda rt: rt.goto(url))
        log(f"已在专用浏览器打开：{j.get('title', '')} / {j.get('company', '')}")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e).splitlines()[0][:150]})


@app.post("/api/tailor")
def api_tailor():
    d = request.json or {}
    j = db.get_job(int(d.get("id") or 0)) or {}
    jd = (d.get("jd") or j.get("jd") or "").strip()
    if len(jd) < 30:
        return jsonify({"ok": False, "error": "JD 太短：先读 JD 或手动粘贴"})
    try:
        out = ai.tailor(jd, d.get("target") or
                        f"{j.get('company', '')} · {j.get('title', '')}",
                        R.load())
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:180]})
    db.save_version(j.get("id"), out.get("target") or j.get("title", ""),
                    out["resume"], out["notes"], out["gaps"], out["warnings"])
    return jsonify({"ok": True, **out})


@app.post("/api/tailor/export")
def api_tailor_export():
    d = request.json or {}
    data = d.get("resume") or {}
    name = d.get("target") or "定制简历"
    try:
        html, pdf = R.export(data, f"定制-{name}"[:40].replace(" ", "").replace("/", ""))
        return jsonify({"ok": True, "html": f"/exports/{html.name}",
                        "pdf": (f"/exports/{pdf.name}" if pdf else None),
                        "pdf_error": "" if pdf else R.pdf_error()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:150]})


# ---------------- 路由：跟进看板与投递清单（全部零触达） ----------------

def _slim_job(j):
    """看板/清单用的裁剪视图：不回传 JD 全文，省流量也省 token。"""
    return {"id": j["id"], "platform": j["platform"], "title": j.get("title"),
            "company": j.get("company"), "salary_text": j.get("salary_text"),
            "city": j.get("city"), "status": j.get("status"),
            "ai_tier": j.get("ai_tier"), "ai_why": (j.get("ai_why") or "")[:100],
            "score": j.get("score"), "customized": bool(j.get("customized")),
            "has_jd": bool((j.get("jd") or "").strip()), "url": j.get("url") or "",
            "p_stage": j.get("p_stage") or "", "p_note": j.get("p_note") or "",
            "p_at": j.get("p_at") or ""}


@app.get("/api/pipeline")
def api_pipeline():
    stage = request.args.get("stage") or None
    rows = db.pipeline_list(stage)
    return jsonify({"stages": db.STAGES, "names": db.STAGE_NAMES,
                    "counts": db.pipeline_counts(),
                    "jobs": [_slim_job(r) for r in rows]})


@app.post("/api/pipeline")
def api_pipeline_set():
    d = request.json or {}
    jid = int(d.get("id") or 0)
    if not db.get_job(jid):
        return jsonify({"ok": False, "error": "岗位不存在"}), 404
    stage = d.get("stage")
    if stage is not None and stage not in db.STAGES + [""]:
        return jsonify({"ok": False, "error": f"未知阶段：{stage}"}), 400
    db.pipeline_set(jid, stage=stage, note=d.get("note"),
                    reply_text=d.get("reply_text"))
    if stage == "applied":                 # 手动标记已投递：同步岗位状态
        j = db.get_job(jid) or {}
        st = j.get("status") or ""
        if st.startswith(("待投", "保留")):
            db.set_job_status(jid, "已投", "手动标记已投递")
    return jsonify({"ok": True, "counts": db.pipeline_counts()})


@app.get("/api/applylist")
def api_applylist():
    plat = request.args.get("platform") or None
    limit = int(request.args.get("limit") or 0) or None
    tiers = request.args.get("tiers")
    tl = tuple(t for t in (tiers or "").split(",") if t) or ("强匹配", "匹配")
    rows = db.apply_list(plat, None, tl)          # 先取全量算总数
    total = len(rows)
    need_jd = sum(1 for r in rows if not (r.get("jd") or "").strip())
    shown = rows[:limit] if limit else rows
    return jsonify({"jobs": [_slim_job(r) for r in shown], "count": total,
                    "shown": len(shown), "need_jd": need_jd})


@app.post("/api/ai/reply")
def api_ai_reply():
    """HR 回复助手：分析意图 + 生成草稿。只调 AI，不碰平台。"""
    d = request.json or {}
    txt = (d.get("text") or "").strip()
    if len(txt) < 2:
        return jsonify({"ok": False, "error": "请先粘贴 HR 的消息内容"})
    if not ai.configured():
        return jsonify({"ok": False, "error": "AI 未配置（设置页填接口）"})
    job = db.get_job(int(d.get("id") or 0)) if d.get("id") else None
    try:
        out = ai.reply_assist(txt, job)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e).splitlines()[0][:180]})
    if d.get("id") and d.get("save"):
        db.pipeline_set(int(d["id"]), stage=out["next_stage"], reply_text=txt)
        log(f"跟进已更新：{out['next_stage']}（{(job or {}).get('title','')}）")
    return jsonify({"ok": True, **out})


def _task_batch_tailor(job_ids, auto_jd=True):
    """批量定制：库里已有 JD 的直接做；缺 JD 的在线模式下先读 JD 再定制。"""
    if not ai.configured():
        STATE["message"] = "批量定制未执行：AI 未配置"
        log(STATE["message"])
        return
    base = R.load()
    if not base:
        STATE["message"] = "批量定制未执行：简历为空"
        log(STATE["message"])
        return
    jobs, nojd = [], []
    for jid in job_ids:
        j = db.get_job(int(jid))
        if not j:
            continue
        (jobs if (j.get("jd") or "").strip() else nojd).append(j)
    if nojd and auto_jd and _live():
        log(f"{len(nojd)} 条缺 JD：先在专用浏览器读 JD，再定制")
        keep = []
        for idx, j in enumerate(nojd):
            try:
                risk.check(j["platform"])
                bot.pacer.wait(j["platform"])
                text, err = _read_jd_job(j)
            except risk.Cooldown as e:
                log(f"读 JD 停止（风控）：{e}")
                keep.extend(nojd[idx:])
                break
            except (Blocked, LiveDisabled) as e:
                log(f"读 JD 停止：{e}")
                keep.extend(nojd[idx:])
                break
            except Exception as e:
                log(f"  ! 读 JD 失败 {j.get('title', '')}："
                    f"{str(e).splitlines()[0][:80]}")
                keep.append(j)
                continue
            if text:
                db.save_jd(j["id"], text)
                j["jd"] = text
                jobs.append(j)
                log(f"  · 已读 JD：{j.get('title', '')}")
            else:
                keep.append(j)
                log(f"  ! 读 JD 失败 {j.get('title', '')}：{err or '空内容'}")
        nojd = keep
    if nojd:
        why = "离线模式不能读 JD" if not _live() else "读 JD 未成功"
        log(f"跳过 {len(nojd)} 条（{why}）："
            + "、".join((j.get("title") or "")[:12] for j in nojd[:5]))
    STATE["total"] = len(jobs)
    log(f"开始批量定制 {len(jobs)} 条")
    ok = fail = 0
    for j in jobs:
        target = f"{j.get('company','')} · {j.get('title','')}"
        try:
            out = ai.tailor(j["jd"], target, base)
            db.save_version(j["id"], target, out["resume"], out["notes"],
                            out["gaps"], out["warnings"])
            with db.connect() as con:
                con.execute("UPDATE jobs SET customized=1 WHERE id=?", (j["id"],))
            ok += 1
            warn = ("；".join(out["warnings"])[:60]) if out.get("warnings") else ""
            log(f"  ✓ {target}" + (f"（防编造告警：{warn}）" if warn else ""))
        except Exception as e:
            fail += 1
            log(f"  ! {target} 定制失败：{str(e).splitlines()[0][:90]}")
        STATE["done"] += 1
    log(f"批量定制结束：成功 {ok}，失败 {fail}（简历页/版本记录里可查看导出）")


@app.post("/api/tailor/batch")
def api_tailor_batch():
    d = request.json or {}
    ids = d.get("ids") or []
    if not ids:
        return jsonify({"ok": False, "error": "请先勾选岗位"}), 400
    if not ai.configured():
        return jsonify({"ok": False, "error": "AI 未配置"})
    auto_jd = bool(d.get("auto_jd"))
    started = _run_task("批量定制", lambda: _task_batch_tailor(ids, auto_jd))
    return jsonify({"ok": started, "started": started, "n": len(ids)})


@app.post("/api/applylist/export")
def api_applylist_export():
    """导出投递清单为 Markdown（含 AI 判定与理由），供打印/手机查看。"""
    d = request.get_json(silent=True) or {}
    rows = db.apply_list(d.get("platform") or None,
                         int(d.get("limit") or 0) or None)
    if not rows:
        return jsonify({"ok": False, "error": "清单为空"})
    lines = [f"# 今日投递清单 · {datetime.now():%Y-%m-%d %H:%M}", "",
             f"共 {len(rows)} 条，按 AI 匹配度排序。**投递请在平台手动完成**，"
             "回来在看板里更新阶段。", ""]
    for i, j in enumerate(rows, 1):
        lines += [f"## {i}. {j.get('title','')} · {j.get('company','')}",
                  f"- 平台：{config.PLATFORM_NAMES.get(j['platform'], j['platform'])}"
                  f"｜城市：{j.get('city','')}｜薪资：{j.get('salary_text','')}",
                  f"- AI 判定：**{j.get('ai_tier') or '待判'}**"
                  + (f"｜{j.get('ai_why','')}" if j.get("ai_why") else ""),
                  f"- 链接：{j.get('url','')}",
                  f"- 定制简历：{'已生成' if j.get('customized') else '未生成'}"
                  + ("（库里无 JD）" if not (j.get("jd") or "").strip() else ""), ""]
    config.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    p = config.EXPORT_DIR / f"投递清单-{datetime.now():%Y%m%d-%H%M}.md"
    p.write_text("\n".join(lines), encoding="utf-8")
    log(f"投递清单已导出：{p.name}（{len(rows)} 条）")
    return jsonify({"ok": True, "file": p.name, "count": len(rows)})


# ---------------- 路由：AI Agent（模型主导，闸门兜底） ----------------

def _agent_read_jd(job):
    """供 agent 调用的读 JD：返回 (text, err)。异常向上抛由 tools 兜住。"""
    p = job["platform"]
    if p == "boss" and not risk.boss_enabled():
        return None, "BOSS 自动访问开关为关"
    risk.check(p)
    url = boss_api.detail_url(job) if p == "boss" else job["url"]
    if p == "boss" and not url:
        return None, "缺 securityId（链接过期），重新采集后再试"
    bot.pacer.wait(p)

    def _rd(rt):
        rt.hide()
        try:
            if p == "boss":
                return boss_api.read_jd(rt, url)
            if p == "zhaopin":
                return zhaopin.read_jd(rt, url)
            return liepin.read_jd(rt, url)
        finally:
            rt.show()

    text, err = bot.call(_rd)
    return (text, err)


def _agent_apply_one(job):
    """供 agent 调用的单条投递：按通道设置走会话内接口或浏览器。"""
    p = job["platform"]
    risk.check(p)
    bot.pacer.wait(p)
    if p == "boss" and _apply_channel(p) == "session":
        return bot.call(lambda rt: session_api.boss_greet(rt, job, log))
    url = boss_api.detail_url(job) if p == "boss" else job["url"]
    if p == "boss" and not url:
        return False, "缺 securityId（链接过期），重新采集后再试"

    def _go(rt):
        if p == "zhaopin":
            return zhaopin.apply(rt, url, log)
        if p == "liepin":
            return liepin.apply(rt, url, log)
        return boss_api.apply_greet(rt, url, log)

    return bot.call(_go)


def _agent_ctx():
    """组装 agent 的运行时能力（tools 不 import app，全靠这里注入）。"""
    return tools.ToolContext(
        log=log, live=_live, bot=bot,
        collect=lambda plats, kw, cities, pages: _task_collect(
            plats, kw, cities, pages),
        read_jd=_agent_read_jd, apply_one=_agent_apply_one)


@app.get("/api/agent/tools")
def api_agent_tools():
    """工具清单与风险分级（让用户看清 agent 的能力边界）。"""
    return jsonify({"tools": [
        {"name": t["name"], "risk": t["risk"],
         "risk_label": tools.RISK_NAMES[t["risk"]], "desc": t["desc"]}
        for t in tools.REGISTRY.values()],
        "counts": {r: sum(1 for t in tools.REGISTRY.values() if t["risk"] == r)
                   for r in (tools.RISK_LOCAL, tools.RISK_READ, tools.RISK_WRITE)},
        "supports_tools": ai.configured()})


@app.get("/api/agent/state")
def api_agent_state():
    st = agent.state()
    pend = st.get("pending")
    if pend:
        j = db.get_job(int((pend.get("args") or {}).get("job_id") or 0))
        st["pending"] = {**pend, "job": (
            {"title": j.get("title"), "company": j.get("company"),
             "salary": j.get("salary_text"), "city": j.get("city"),
             "platform": config.PLATFORM_NAMES.get(j["platform"], j["platform"]),
             "ai_tier": j.get("ai_tier"), "url": j.get("url")} if j else None)}
        st["pending"].pop("call", None)
    return jsonify(st)


@app.get("/api/agent/events")
def api_agent_events():
    return jsonify({"events": agent.events(int(request.args.get("since") or 0))})


@app.post("/api/agent/run")
def api_agent_run():
    d = request.get_json(silent=True) or {}
    goal = (d.get("goal") or "").strip()
    if len(goal) < 4:
        return jsonify({"ok": False, "error": "请描述你想让 AI 做什么"}), 400
    if not ai.configured():
        return jsonify({"ok": False, "error": "AI 未配置：请先到设置页填接口"}), 400
    if STATE["running"]:
        return jsonify({"ok": False, "error": "有任务在跑，请等它结束"}), 409
    steps = max(3, min(int(d.get("max_steps") or agent.MAX_STEPS), 40))
    if not agent.start(goal, _agent_ctx(), steps):
        return jsonify({"ok": False,
                        "error": "Agent 正在运行或有待确认操作"}), 409
    log(f"Agent 启动：{goal[:80]}（上限 {steps} 步）")
    db.event("agent", "", "run", goal[:160])
    return jsonify({"ok": True, "started": True})


@app.post("/api/agent/confirm")
def api_agent_confirm():
    d = request.get_json(silent=True) or {}
    approved = bool(d.get("approved"))
    note = str(d.get("note") or "")[:200]
    ok, msg = agent.resume(approved, _agent_ctx(), note)
    if not ok:
        return jsonify({"ok": False, "error": msg}), 409
    log(f"Agent 确认：{'批准' if approved else '拒绝'}{('（' + note + '）') if note else ''}")
    return jsonify({"ok": True, "msg": msg})


@app.post("/api/agent/stop")
def api_agent_stop():
    agent.stop()
    if agent.ST.get("pending"):
        agent.clear_pending()
    log("Agent 已请求中止")
    return jsonify({"ok": True})


@app.get("/exports/<name>")
def exports(name):
    return send_from_directory(str(config.EXPORT_DIR), name)


# ---------------- 路由：简历 / AI / 计划 ----------------

@app.get("/api/resume")
def api_resume_get():
    return jsonify({"resume": R.load(), "text": R.to_text()})


@app.post("/api/resume")
def api_resume_save():
    R.save((request.json or {}).get("resume") or {})
    return jsonify({"ok": True})


@app.post("/api/resume/preview")
def api_resume_preview():
    d = (request.json or {}).get("resume") or R.load()
    return jsonify({"html": R.render_html(d, (request.json or {}).get("target", ""))})


@app.post("/api/resume/export")
def api_resume_export():
    try:
        html, pdf = R.export()
        return jsonify({"ok": True, "html": f"/exports/{html.name}",
                        "pdf": (f"/exports/{pdf.name}" if pdf else None),
                        "pdf_error": "" if pdf else R.pdf_error()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:150]})


@app.post("/api/resume/upload")
def api_resume_upload():
    """把当前简历导出 PDF，上传为平台附件简历（真实修改平台账号）。"""
    d = request.json or {}
    plats = [p for p in (d.get("platforms") or []) if p in config.PLATFORM_NAMES]
    if not plats:
        return jsonify({"started": False, "error": "先选择平台"}), 400
    if not _live():
        return jsonify({"started": False, "live": False,
                        "error": "离线模式：请到「设置 → 运行模式」开启在线模式"})
    if not R.load().get("name"):
        return jsonify({"started": False, "error": "当前没有简历，请先导入"})

    def fn():
        html, pdf = R.export()
        if not pdf:
            log("附件上传未执行：PDF 生成失败。" + R.pdf_error())
            return
        log(f"准备上传附件：{pdf.name}")
        for p in plats:
            try:
                risk.check(p)
                bot.pacer.wait(p)
                ok, msg = bot.call(
                    lambda rt, p=p: resume_sync.upload_attachment(rt, p, pdf, log))
                log(f"{config.PLATFORM_NAMES[p]} 附件简历：{'✓' if ok else '×'} {msg}")
                db.event("resume", p, "upload_attachment",
                         f"{'ok' if ok else 'fail'}: {msg}")
            except (Blocked, risk.Cooldown) as e:
                log(f"{config.PLATFORM_NAMES[p]} 附件上传停止：{e}")
                db.event("resume", p, "upload_attachment", f"stop: {e}")
                break
            except Exception as e:
                m0 = str(e).splitlines()[0][:100]
                log(f"{config.PLATFORM_NAMES[p]} 附件上传异常：{m0}")
                db.event("resume", p, "upload_attachment", f"error: {m0}")
        log("附件上传结束")

    started = _run_task("上传附件简历", fn)
    return jsonify({"started": started, "live": True})


@app.get("/api/resume/merge/status")
def api_resume_merge_status():
    """在线简历核查进度：reading（读页面）→ merging（AI 合并）→ idle。"""
    return jsonify({"platform": MERGE_ST["platform"], "stage": MERGE_ST["stage"],
                    "seconds": (int(time.time() - MERGE_ST["started_at"])
                                if MERGE_ST["started_at"] else 0)})


@app.post("/api/resume/merge")
def api_resume_merge():
    """读取平台在线简历 + 本地简历 → AI 合并报告（只读，不写平台）。"""
    p = (request.json or {}).get("platform")
    if p not in config.PLATFORM_NAMES:
        return jsonify({"ok": False, "error": "平台不正确"}), 400
    if not _live():
        return jsonify({"ok": False,
                        "error": "离线模式：请到「设置 → 运行模式」开启在线模式"})
    if STATE["running"] or agent.ST["running"]:
        return jsonify({"ok": False, "error": "有任务在跑，稍后再试"}), 409
    if MERGE_ST["stage"] in ("reading", "merging"):
        return jsonify({"ok": False, "error": "已有一次在线简历核查在进行中"}), 409
    name = config.PLATFORM_NAMES[p]
    MERGE_ST.update(platform=p, stage="reading", started_at=time.time())
    try:
        risk.check(p)
        log(f"开始读取 {name} 在线简历…")
        online = bot.call(lambda rt: resume_sync.read_online(rt, p))
        log(f"已读取 {name} 在线简历（{len(online)} 字），AI 合并中（约 1-2 分钟）…")
        MERGE_ST["stage"] = "merging"
        out = ai.merge_online_resume(online)
        log(f"{name} 在线简历核查完成：平台独有 "
            f"{len(out['only_online'])} 项，冲突 {len(out['conflicts'])} 项")
        return jsonify({"ok": True, **out, "online_len": len(online)})
    except (Blocked, risk.Cooldown) as e:
        log(f"{name} 在线简历核查停止：{e}")
        return jsonify({"ok": False, "error": str(e)[:160]})
    except Exception as e:
        m0 = str(e).splitlines()[0][:180]
        log(f"{name} 在线简历核查失败：{m0}")
        return jsonify({"ok": False, "error": m0})
    finally:
        MERGE_ST.update(stage="idle", started_at=0)


@app.get("/api/ai/settings")
def api_ai_get():
    s = ai.settings()
    s["api_key"] = (s["api_key"][:6] + "…") if s["api_key"] else ""
    return jsonify({"settings": s, "configured": ai.configured(),
                    "key_problem": ai.key_problem()})


@app.post("/api/ai/settings")
def api_ai_save():
    d = request.json or {}
    cur = ai.settings()
    key = str(d.get("api_key", "") or "").strip()
    if not key or "…" in key:
        d["api_key"] = cur["api_key"]          # 脱敏回显/留空 → 不覆盖原值
    elif len(key) < ai.MIN_KEY_LEN:
        # 宁可当场拒绝，也不要存进库里等到调用时才 401
        return jsonify({"ok": False, "configured": ai.configured(),
                        "msg": f"API Key 只有 {len(key)} 个字符，看起来不完整"
                               f"（通常 20 位以上）。请重新完整粘贴。"}), 400
    s = ai.save_settings(d)
    prob = ai.key_problem()
    if d.get("test"):
        ok, msg = ai.test_connection()
        if not ok and prob:
            msg = f"{msg}｜{prob}"
        return jsonify({"ok": ok, "msg": msg, "configured": ai.configured(),
                        "key_problem": prob})
    return jsonify({"ok": True, "configured": ai.configured(),
                    "key_problem": prob})


@app.post("/api/ai/optimize")
def api_ai_optimize():
    """生成优化版新简历（不落库，前端确认「采用」才覆盖）。"""
    try:
        out = ai.optimize_resume()
        return jsonify({"ok": True, **out})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:180]})


@app.post("/api/ai/adopt")
def api_ai_adopt():
    """采纳优化版：旧版先存档为版本记录，再覆盖当前简历。"""
    d = (request.json or {}).get("resume") or {}
    if not d.get("name"):
        return jsonify({"ok": False, "error": "内容无效"}), 400
    old = R.load()
    if old.get("name"):
        db.save_version(None, "优化前的原版", old, [], [], [])
    R.save(d)
    log("已采用优化版简历（原版已存入版本历史）")
    return jsonify({"ok": True})


@app.post("/api/ai/analyze")
def api_ai_analyze():
    try:
        return jsonify({"ok": True, **ai.analyze_resume()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:180]})


@app.post("/api/ai/interview")
def api_ai_interview():
    """对话补充经历：一轮访谈（AI 追问细节）。"""
    msgs = (request.json or {}).get("messages") or []
    if not isinstance(msgs, list) or not msgs:
        return jsonify({"ok": False, "error": "先说一句你的经历"}), 400
    try:
        return jsonify({"ok": True, "reply": ai.interview_reply(msgs)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:180]})


@app.post("/api/ai/interview/build")
def api_ai_interview_build():
    """把访谈对话合并进简历（只返回结果，采用仍走 /api/ai/adopt）。"""
    msgs = (request.json or {}).get("messages") or []
    try:
        return jsonify({"ok": True, **ai.interview_build(msgs)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:180]})


@app.post("/api/ai/plan")
def api_ai_plan():
    """AI 生成计划：只把「以前没有的行」追加进去，已有行原样保留。"""
    try:
        out = ai.plan_search()
        items, dropped = planmod.validate(out.get("plan") or [])
        seen = {(r["platform"], (r["keyword"] or "").strip(), (r["city"] or "").strip())
                for r in db.plan_list()}
        fresh = []
        for it in items:
            key = (it["platform"], (it["keyword"] or "").strip(),
                   (it["city"] or "").strip())
            if key in seen:
                dropped.append(f"已存在：{config.PLATFORM_NAMES[it['platform']]}/"
                               f"{it['keyword']}/{it['city'] or '全部城市'}")
                continue
            it["enabled"] = 0
            fresh.append(it)
            seen.add(key)
        if fresh:
            db.plan_save(fresh)
        log(f"AI 生成计划：新增 {len(fresh)} 行，跳过重复 {len(items) - len(fresh)} 行")
        return jsonify({"ok": True, "count": len(fresh), "added": len(fresh),
                        "skipped": len(items) - len(fresh),
                        "total": len(db.plan_list()),
                        "dropped": dropped, "notes": out.get("notes", "")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:180]})


@app.post("/api/plan/batch_add")
def api_plan_batch_add():
    """批量手动新增：一个平台 + 一个城市 + 多个关键词（换行/逗号分隔）。"""
    d = request.json or {}
    platform = d.get("platform")
    city = (d.get("city") or "").strip()
    if platform not in config.PLATFORM_NAMES:
        return jsonify({"ok": False, "error": "平台不正确"}), 400
    raw = d.get("keywords")
    if isinstance(raw, str):
        kws = [x.strip() for x in re.split(r"[,，;；\n]+", raw) if x.strip()]
    else:
        kws = [str(x).strip() for x in (raw or []) if str(x).strip()]
    if not kws:
        return jsonify({"ok": False, "error": "关键词为空"}), 400
    items, dropped = planmod.validate(
        [{"platform": platform, "keyword": k, "city": city} for k in kws])
    seen = {(r["platform"], (r["keyword"] or "").strip(), (r["city"] or "").strip())
            for r in db.plan_list()}
    fresh = []
    for it in items:
        key = (it["platform"], (it["keyword"] or "").strip(), (it["city"] or "").strip())
        if key in seen:
            dropped.append(f"已存在：{it['keyword']}/{it['city'] or '全部城市'}")
            continue
        it.update(enabled=1, source="manual")
        fresh.append(it)
        seen.add(key)
    if fresh:
        db.plan_save(fresh)
    log(f"批量新增计划：新增 {len(fresh)} 行，跳过 {len(items) - len(fresh)} 行")
    return jsonify({"ok": True, "added": len(fresh),
                    "skipped": len(items) - len(fresh), "dropped": dropped})


@app.post("/api/plan/batch_delete")
def api_plan_batch_delete():
    ids = (request.json or {}).get("ids") or []
    if not ids:
        return jsonify({"ok": False, "error": "没有选中行"}), 400
    n = db.plan_delete_many(ids)
    log(f"批量删除计划：{n} 行")
    return jsonify({"ok": True, "removed": n})


@app.get("/api/plan")
def api_plan_list():
    cities = R.intent_cities()
    hint = "" if cities else (
        "简历的「求职意向」里没有可用的城市。请到「简历」页 → JSON 编辑器的 intent 字段"
        f"补上城市（支持：{'、'.join(citycfg.names())}），保存后再回来。")
    return jsonify({"plan": db.plan_list(), "cities": cities, "city_hint": hint})


@app.post("/api/plan/save")
def api_plan_save():
    d = request.json or {}
    items, dropped = planmod.validate(d.get("plan") or [])
    with db.connect() as con:
        con.execute("DELETE FROM plan")          # 整表替换，避免追加重复
    db.plan_save(items)
    return jsonify({"ok": True, "count": len(items), "dropped": dropped})


@app.post("/api/plan/toggle")
def api_plan_toggle():
    d = request.json or {}
    db.plan_set(int(d["id"]), enabled=int(bool(d.get("enabled"))))
    return jsonify({"ok": True})


@app.post("/api/plan/delete")
def api_plan_delete():
    with db.connect() as con:
        con.execute("DELETE FROM plan WHERE id=?", (int((request.json or {}).get("id", 0)),))
    return jsonify({"ok": True})


# ---------------- 路由：聊天机制 B/C/D / 导出 ----------------

@app.post("/api/chat/<kind>")
def api_chat(kind):
    if STATE["running"]:
        return jsonify({"ok": False, "error": "任务运行中"}), 409
    try:
        if kind == "scan":
            return jsonify({"ok": True, **chat.scan_hr(bot, log)})
        if kind == "cleanse":
            return jsonify({"ok": True, **chat.cleanse_recommend(bot, log)})
        if kind == "followup":
            return jsonify({"ok": True, **chat.followup_sweep(bot, log)})
    except LiveDisabled as e:
        return jsonify({"ok": False, "error": str(e)}), 409
    except Exception as e:
        return jsonify({"ok": False, "error": str(e).splitlines()[0][:160]})
    return jsonify({"ok": False, "error": "未知机制"}), 404


@app.post("/api/export/xlsx")
def api_export_xlsx():
    try:
        return jsonify({"ok": True, "file": exporter.to_xlsx()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:150]})


@app.get("/api/apps")
def api_apps():
    return jsonify({"apps": db.apps_recent(80)})


# ---------------- 初始化向导 ----------------

@app.get("/api/wizard")
def api_wizard():
    return jsonify({"initialized": db.wizard_get("initialized", "0") == "1",
                    "steps": {"ai": ai.configured(),
                              "resume": bool(R.load().get("name")),
                              "plan": len(db.plan_list()) > 0,
                              "accounts": db.wizard_get("accounts", "")}})


@app.post("/api/wizard/ai")
def api_wizard_ai():
    d = request.json or {}
    cur = ai.settings()
    if str(d.get("api_key", "")).endswith("…"):
        d["api_key"] = cur["api_key"]
    ai.save_settings(d)
    ok, msg = ai.test_connection()
    return jsonify({"ok": ok, "msg": msg})


@app.post("/api/wizard/resume")
def api_wizard_resume():
    """mode=upload(文件) / text(粘贴)。multipart 时不能用 request.json（会 415）。"""
    d = request.get_json(silent=True) or {}
    mode = request.args.get("mode") or d.get("mode", "text")
    try:
        if mode == "upload" and "file" in request.files:
            f = request.files["file"]
            tmp = config.DATA_DIR / ("import_" + f.filename)
            tmp.parent.mkdir(exist_ok=True)
            f.save(str(tmp))
            text = ingest.extract_text(str(tmp))
        else:
            text = (d.get("text") or "").strip()
        if len(text) < 80:
            return jsonify({"ok": False, "error": "简历文本太短/解析失败"})
        data = ai.structure_resume(text)
        R.save(data)
        db.wizard_set("resume_source", mode)
        return jsonify({"ok": True, "resume": data})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:180]})


@app.post("/api/wizard/logins")
def api_wizard_logins():
    st = _check_login(list(config.PLATFORM_NAMES))
    db.wizard_set("accounts", json.dumps(st, ensure_ascii=False))
    return jsonify({"ok": True, "status": st})


@app.post("/api/wizard/finish")
def api_wizard_finish():
    miss = []
    if not ai.configured():
        miss.append("AI")
    if not R.load().get("name"):
        miss.append("简历")
    db.wizard_set("initialized", "1")
    log("初始化完成" + (f"（提示：还缺 {'、'.join(miss)}，可稍后在设置页补）" if miss else ""))
    return jsonify({"ok": True, "missing": miss})


@app.post("/api/wizard/reset")
def api_wizard_reset():
    db.wizard_set("initialized", "0")
    return jsonify({"ok": True})


def bootstrap():
    db.init()
    if db.setting("salary_floor") is None:
        db.set_setting("salary_floor", config.SALARY_FLOOR)
    if db.setting("boss_enabled") is None:
        db.set_setting("boss_enabled", 0)
    n = db.pipeline_backfill_applied()   # 历史「已投」岗位补进跟进看板
    if n:
        log(f"跟进看板：已把 {n} 条历史「已投」岗位补成「已投递」")


if __name__ == "__main__":
    bootstrap()
    log(f"jobpilot 控制台：http://{config.HOST}:{config.PORT} ｜ "
        f"{'在线模式' if _live() else '离线模式（不访问招聘网站）'}")
    app.run(host=config.HOST, port=config.PORT)
