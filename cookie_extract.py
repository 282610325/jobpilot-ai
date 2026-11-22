"""cookie_extract：直接读本机 Chrome 的 Cookie 库，用户无需 F12 复制。

原理：Chrome(macOS) 把 Cookie 存在 profile 下的 SQLite `Cookies` 表，value 用
AES-128-CBC 加密（v10/v11 前缀），密钥由「Chrome Safe Storage」Keychain 条目
经 PBKDF2(saltysalt, 1003 轮) 派生。本模块只读复制数据库（避免锁），解密后
按平台域名归集，交给 netio 发 HTTP 请求。

对求职者的意义：扫码登录一次 → 程序自动取 Cookie → 纯接口采集，零手动复制。
"""
import base64
import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import config

IS_WINDOWS = sys.platform.startswith("win")
WIN_MSG = ("Windows 暂不支持自动读取浏览器 Cookie（DPAPI/AES-GCM 且受 app-bound 加密限制）；"
           "请到「设置 → 账号连接 → 专用窗口登录」登录一次，采集窗口会持久保存登录态")

PLATFORM_DOMAINS = {
    "zhaopin": (".zhaopin.com", "zhaopin.com"),
    "liepin": (".liepin.com", "liepin.com", "lpt.liepin.com", "c.liepin.com"),
    "boss": (".zhipin.com", "zhipin.com", ".www.zhipin.com"),
}

# 候选 profile：程序专用目录优先，其次用户日常 Chrome/Edge
if IS_WINDOWS:
    _USER_DATA = Path(os.environ.get("LOCALAPPDATA") or Path.home())
    PROFILES = [
        config.CHROME_PROFILE,
        _USER_DATA / "Google/Chrome/User Data",
        _USER_DATA / "Microsoft/Edge/User Data",
    ]
else:
    PROFILES = [
        config.CHROME_PROFILE,
        Path.home() / "Library/Application Support/Google/Chrome",
        Path.home() / "Library/Application Support/Microsoft Edge",
    ]


def _key():
    """Chrome Safe Storage 密码（base64）→ 16 字节 AES key。

    Windows 的 Cookie 加密是 DPAPI + AES-256-GCM（新版还有 app-bound 加密），
    这里不做逆向：返回 None，由上层引导用户用专用窗口登录。
    """
    if IS_WINDOWS:
        return None
    for service in ("Chrome Safe Storage", "Microsoft Edge Safe Storage"):
        try:
            pwd = subprocess.run(
                ["security", "find-generic-password", "-w", "-s", service],
                capture_output=True, text=True, timeout=10).stdout.strip()
            if pwd:
                return hashlib.pbkdf2_hmac("sha1", pwd.encode(), b"saltysalt",
                                           1003, 16)
        except Exception:
            continue
    return None


def _decrypt(blob, key):
    """v10/v11 → 明文；失败返回 None。"""
    if not blob or len(blob) < 4:
        return None
    if blob[:3] not in (b"v10", b"v11"):
        try:
            return blob.decode("utf-8")       # 未加密（Linux/旧版）
        except UnicodeDecodeError:
            return None
    try:
        from Crypto.Cipher import AES
        iv = b" " * 16
        dec = AES.new(key, AES.MODE_CBC, iv).decrypt(blob[3:])
        pad = dec[-1]
        if 0 < pad <= 16:
            dec = dec[:-pad]
        # Chrome 130+ 会在明文前附加 32 字节 host hash
        if len(dec) > 32 and any(b < 0x20 for b in dec[:32]):
            dec = dec[32:]
        txt = dec.decode("utf-8", "ignore")
        # 解密失败会得到乱码/PUA 字符：只保留可安全放进 HTTP 头的值
        try:
            txt.encode("latin-1")
        except UnicodeEncodeError:
            return None
        return txt if all(32 <= ord(c) < 127 for c in txt) else None
    except Exception:
        return None


def _cookie_db(profile):
    for sub in ("Default", "Profile 1", "Profile 2", "Profile 3"):
        p = profile / sub / "Cookies"
        if p.exists():
            return p
    p = profile / "Network" / "Cookies"
    return p if p.exists() else None


def _read_profile(profile, key, wanted):
    """读一个 profile，返回 {platform: {name: value}}。"""
    db = _cookie_db(profile)
    if not db:
        return {}
    tmp = Path(tempfile.mkdtemp()) / "Cookies"
    try:
        # 复制三件套（WAL），避免直接锁住运行中的 Chrome
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(db) + suffix)
            if src.exists():
                shutil.copy2(src, str(tmp) + suffix)
        con = sqlite3.connect(tmp)
        out = {p: {} for p in wanted}
        for host, name, value, enc, expires in con.execute(
                "SELECT host_key, name, value, encrypted_value, expires_utc FROM cookies"):
            plat = None
            for p, doms in PLATFORM_DOMAINS.items():
                if p in wanted and any(host.endswith(d) for d in doms):
                    plat = p
                    break
            if not plat:
                continue
            if not value and enc:
                value = _decrypt(enc, key)
            if value:
                out[plat][name] = value
        con.close()
        return {p: v for p, v in out.items() if v}
    except Exception:
        return {}
    finally:
        shutil.rmtree(tmp.parent, ignore_errors=True)


# Chrome 时间戳（1601-01-01 起，微秒）→ Unix 秒
_CHROME_EPOCH_OFFSET = 11644473600


def _unix_expiry(expires_utc):
    """Chrome expires_utc → Unix 秒；0 表示 session cookie（返回 None）。"""
    if not expires_utc:
        return None
    try:
        sec = int(expires_utc) // 1_000_000 - _CHROME_EPOCH_OFFSET
        return sec if sec > 0 else None
    except Exception:
        return None


def read_profile_full(profile, wanted=None):
    """读 cookie 并保留过期时间等属性（同步到采集窗口时必须带 expiry，
    否则注入后全变成 session cookie，浏览器一关就丢）。

    返回 {platform: [{name, value, domain, path, expires, secure, httpOnly}]}
    """
    wanted = list(wanted or PLATFORM_DOMAINS.keys())
    key = _key()
    if not key:
        return {}
    db = _cookie_db(Path(profile))
    if not db:
        return {}
    tmp = Path(tempfile.mkdtemp()) / "Cookies"
    try:
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(db) + suffix)
            if src.exists():
                shutil.copy2(src, str(tmp) + suffix)
        con = sqlite3.connect(tmp)
        out = {p: [] for p in wanted}
        for host, name, value, enc, exp, secure, httponly, path in con.execute(
                "SELECT host_key,name,value,encrypted_value,expires_utc,"
                "is_secure,is_httponly,path FROM cookies"):
            plat = None
            for p, doms in PLATFORM_DOMAINS.items():
                if p in wanted and any(host.endswith(d) for d in doms):
                    plat = p
                    break
            if not plat:
                continue
            if not value and enc:
                value = _decrypt(enc, key)
            if not value:
                continue
            out[plat].append({"name": name, "value": value, "domain": host,
                              "path": path or "/",
                              "expires": _unix_expiry(exp),
                              "secure": bool(secure), "httpOnly": bool(httponly)})
        con.close()
        return {p: v for p, v in out.items() if v}
    except Exception:
        return {}
    finally:
        shutil.rmtree(tmp.parent, ignore_errors=True)


def extract(platforms=None):
    """返回 ({platform: {name: value}}, 来源说明)。自动遍历候选 profile。"""
    wanted = list(platforms or PLATFORM_DOMAINS.keys())
    key = _key()
    if not key:
        return {}, (WIN_MSG if IS_WINDOWS
                    else "无法读取系统钥匙串（Chrome Safe Storage），请改用粘贴 Cookie")
    merged, sources = {}, []
    for prof in PROFILES:
        if not prof.exists():
            continue
        got = _read_profile(prof, key, wanted)
        if got:
            label = str(prof).replace(str(Path.home()), "~")
            for p, ck in got.items():
                if p not in merged or len(ck) > len(merged[p]):
                    merged[p] = ck
            sources.append(f"{label}({','.join(got)})")
        if len(merged) == len(wanted):
            break
    return merged, "；".join(sources) or "未找到已登录的 Chrome 配置"


# 用户日常浏览器（同步登录态给程序专用窗口时用这一份）
if IS_WINDOWS:
    _UD = Path(os.environ.get("LOCALAPPDATA") or Path.home())
    USER_PROFILES = [
        _UD / "Google/Chrome/User Data",
        _UD / "Microsoft/Edge/User Data",
    ]
else:
    USER_PROFILES = [
        Path.home() / "Library/Application Support/Google/Chrome",
        Path.home() / "Library/Application Support/Microsoft Edge",
    ]

# 各平台"网页登录态"必需 cookie：缺了这些，浏览器会话一定判未登录
WEB_LOGIN_KEYS = {
    "zhaopin": ("x_passport_sid", "zp_passport_deepknow_sessionId", "c_user_id"),
    "liepin": ("lt_auth", "user_name", "imId_0"),
    "boss": ("bst", "wt2", "zp_at", "geek_zp_token"),
}


def extract_user(platforms=None):
    """只从用户日常浏览器读（不含程序专用窗口），用于把登录态同步过去。"""
    wanted = list(platforms or PLATFORM_DOMAINS.keys())
    key = _key()
    if not key:
        return {}, (WIN_MSG if IS_WINDOWS else "无法读取系统钥匙串")
    merged, sources = {}, []
    for prof in USER_PROFILES:
        if not prof.exists():
            continue
        got = _read_profile(prof, key, wanted)
        if got:
            for p, ck in got.items():
                if p not in merged or len(ck) > len(merged[p]):
                    merged[p] = ck
            sources.append(str(prof).replace(str(Path.home()), "~"))
    return merged, "；".join(sources) or "没找到日常浏览器配置"


def extract_window(platforms=None):
    """只读程序专用窗口的 Cookie（判断采集窗口是否已同步登录态）。

    注意：该 profile 的 cookie 可能由不同 Chrome 版本写入，本地密钥解不开，
    此时返回空——不影响同步流程（同步是注入方向，不需要解这里）。
    """
    wanted = list(platforms or PLATFORM_DOMAINS.keys())
    key = _key()
    if not key:
        return {}, (WIN_MSG if IS_WINDOWS else "无法读取系统钥匙串")
    got = _read_profile(Path(config.CHROME_PROFILE), key, wanted)
    return got, "专用窗口"


def login_state(platform, cookies):
    """判断这组 cookie 是否含网页登录态。返回 (是否像已登录, 命中的关键 cookie)。"""
    keys = WEB_LOGIN_KEYS.get(platform, ())
    hits = [k for k in keys if cookies.get(k)]
    return bool(hits), hits


def save_to_db(platforms=None):
    """提取并写入 settings.cookie_raw_<平台>，供 netio 使用。"""
    import db
    got, src = extract(platforms)
    saved = {}
    for p, ck in got.items():
        raw = "; ".join(f"{k}={v}" for k, v in ck.items())
        db.set_setting(f"cookie_raw_{p}", raw)
        saved[p] = len(ck)
    return saved, src
