"""风控中枢（GitHub 方案核心机制）：任何风控信号 → 该平台冷却 60 分钟，绝不硬冲。

状态机：idle →(scan命中/验证页/空白页/会话异常)→ cooling(until=now+N分钟, trips+1)
        →(到期)→ probe_required（只放行 1 次登录探针，通过才回 idle）
        →(当日 trips≥2)→ 平台自动置 enabled=0（BOSS 直接回到总开关关闭，需人工恢复）。
"""
import time

import config
import db


class Cooldown(Exception):
    """平台处于风控冷却期。"""


def _minutes():
    return int(db.setting("cooldown_minutes", config.COOLDOWN_MIN))


def _day_trips(platform):
    with db.connect() as con:
        r = con.execute("SELECT trip_day, trips FROM cooldown WHERE platform=?",
                        (platform,)).fetchone()
    if not r:
        return 0, ""
    from datetime import date
    return (r["trips"] if r["trip_day"] == date.today().isoformat() else 0), r["trip_day"]


def check(platform):
    """入口守卫：平台被暂停或冷却中 → 抛 Cooldown（三平台统一）。"""
    if not platform_on(platform):
        raise Cooldown(
            f"{config.PLATFORM_NAMES.get(platform, platform)} 已被暂停"
            "（到总览勾选「允许自动访问」即可开启）")
    left = remaining(platform)
    if left > 0:
        raise Cooldown(f"{config.PLATFORM_NAMES.get(platform, platform)} 风控冷却中，"
                       f"还剩 {int(left / 60) + 1} 分钟")


def platform_on(platform):
    """三平台统一的「是否允许自动访问」判断。

    唯一真实来源是 enabled_<platform>。BOSS 历史上用 boss_enabled 字段，
    启动时 migrate() 会把它同步过来。
    """
    if platform == "boss":
        v = db.setting("enabled_boss", None)
        if v is None:
            return False                    # BOSS 高风险，未配置时默认关
    else:
        v = db.setting(f"enabled_{platform}", 1)
    try:
        return bool(int(v))
    except (TypeError, ValueError):
        return False if platform == "boss" else True


def migrate():
    """一次性数据迁移：把旧的 boss_enabled 同步到统一字段 enabled_boss。"""
    if db.setting("enabled_boss", None) is None:
        old = db.setting("boss_enabled", None)
        if old is not None:
            db.set_setting("enabled_boss", old)
            db.event("settings", "boss", "migrate",
                     f"旧开关 boss_enabled={old} 已同步到 enabled_boss")


def set_platform(platform, on, reason="user"):
    """统一的平台开关写入（三平台一致）。

    reason: user=用户手动，risk=风控自动关停。界面据此区分提示文案。
    """
    v = 1 if on else 0
    db.set_setting(f"enabled_{platform}", v)
    db.set_setting(f"off_reason_{platform}", "" if on else reason)
    if platform == "boss":
        db.set_setting("boss_enabled", v)        # 兼容旧字段，避免两处不一致
        if on:
            db.set_setting("probe_boss", 0)
    db.event("settings", platform, "toggle",
             f"{config.PLATFORM_NAMES.get(platform, platform)} 自动访问："
             f"{'开' if on else ('关（风控）' if reason == 'risk' else '关')}")


def remaining(platform):
    with db.connect() as con:
        r = con.execute("SELECT until_ts FROM cooldown WHERE platform=?",
                        (platform,)).fetchone()
    if not r:
        return 0.0
    return max(0.0, (r["until_ts"] or 0) - time.time())


def trip(platform, why, minutes=None, clock=None):
    """触发风控：进入冷却并记录事件；当日第 2 次触发自动关停平台开关。"""
    minutes = minutes or _minutes()
    now = (clock or time.time)()
    trips, _ = _day_trips(platform)
    from datetime import date
    with db.connect() as con:
        con.execute(
            "INSERT INTO cooldown(platform, until_ts, reason, trips, trip_day, updated_at) "
            "VALUES(?,?,?,?,?,datetime('now','localtime')) "
            "ON CONFLICT(platform) DO UPDATE SET until_ts=excluded.until_ts, "
            "reason=excluded.reason, trips=excluded.trips, trip_day=excluded.trip_day, "
            "updated_at=excluded.updated_at",
            (platform, now + minutes * 60, str(why)[:200], trips + 1,
             date.today().isoformat()))
    db.event("risk", platform, "cooldown", f"冷却 {minutes} 分钟：{why}")
    if trips + 1 >= 2:
        set_platform(platform, False, reason="risk")
        db.event("risk", platform, "disabled",
                 "当日第 2 次触发风控，已自动关闭该平台开关")


def clear(platform):
    """人工处理完（如手动过验证）后调用：清冷却 + 恢复开关，允许重新使用。"""
    with db.connect() as con:
        con.execute("UPDATE cooldown SET until_ts=0 WHERE platform=?", (platform,))
    db.set_setting(f"probe_{platform}", 1)
    set_platform(platform, True)
    db.event("risk", platform, "clear", "冷却已人工解除、平台开关已恢复")


def enable(platform):
    """单独恢复被暂停/关停的平台开关（不影响冷却状态）。"""
    set_platform(platform, True)
    db.event("risk", platform, "enable", "用户手动恢复平台开关")


def need_probe(platform):
    return bool(db.setting(f"probe_{platform}", 0))


def probe_done(platform):
    db.set_setting(f"probe_{platform}", 0)


def status():
    """三平台统一的风控状态。

    on       —— 开关本身（复选框状态）
    usable   —— 实际能否采集（开关开 且 不在冷却）
    cooldown_left / off_reason —— 冷却倒计时、关停原因（user/risk）
    """
    out = {}
    for p in config.PLATFORM_NAMES:
        on = platform_on(p)
        left = int(remaining(p))
        out[p] = {"enabled": on,
                  "usable": bool(on and left <= 0),
                  "cooldown_left": left,
                  "off_reason": "" if on else (db.setting(f"off_reason_{p}", "") or ""),
                  "auto_off": (not on) and (db.setting(f"off_reason_{p}", "") == "risk")}
    return out


# ---------------- BOSS 总开关（兼容旧接口，内部走统一实现） ----------------

def boss_enabled():
    return platform_on("boss") and remaining("boss") <= 0


def toggle_boss(on):
    set_platform("boss", on)


# ---------------- 信号识别 ----------------

def scan(text):
    """页面正文风控信号；命中返回原因，否则 None。"""
    t = str(text or "")
    if len(t.strip()) < 50:
        return "空白页"
    for kw in config.BLOCK_WORDS:
        if kw in t:
            return f"风控提示：{kw}"
    return None


def guard_security(rt, timeout=8, clock=None):
    """BOSS 跳 passport 验证链：短暂等待自动放行；未见放行或连续跳转 → trip+抛 Cooldown。
    绝不陪着验证链一直跳（每一跳都在被采集指纹）。"""
    import re
    end = (clock or time.time)() + timeout
    while (clock or time.time)() < end:
        try:
            cur = rt.d.current_url or ""
        except Exception:
            cur = ""
        m = re.search(r"_security_check=(\d+)", cur)
        if m and int(m.group(1)) >= 3:
            trip("boss", "安全验证连续跳转（指纹链）")
            raise Cooldown("BOSS 安全验证未自动通过，已停手进入冷却")
        if "security.html" in cur or "/web/passport" in cur:
            time.sleep(1.0)
            continue
        return cur
    trip("boss", "停在安全验证页超时")
    raise Cooldown("BOSS 安全验证未自动通过，已停手进入冷却")
