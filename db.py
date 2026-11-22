"""SQLite 数据层：岗位库、投递日志、黑名单、跟进、采集计划、设置、配额、冷却、事件。

- 每次操作短连接 + WAL，Flask 读线程与后台任务写线程可并发。
- jobs 按 (platform, url_key) 去重；url_key=URL 路径（BOSS 的 securityId 是 query，
  token 刷新不会造成重复入库，同时会把最新 security_id 更新进记录）。
- settings 表存所有"页面即时可调"参数；config.py 只留底稿。
"""
import json
import sqlite3
from datetime import date, datetime

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  platform TEXT NOT NULL, ext_id TEXT, url TEXT NOT NULL, url_key TEXT NOT NULL,
  title TEXT, company TEXT, salary_text TEXT, salary_cap INTEGER,
  city TEXT, area TEXT, edu TEXT, exp TEXT, publish TEXT,
  match_note TEXT, tier TEXT, score INTEGER,
  status TEXT DEFAULT '待投', reason TEXT,
  jd TEXT, jd_at TEXT, security_id TEXT, lid TEXT,
  plan_id INTEGER, customized INTEGER DEFAULT 0,
  first_seen TEXT, last_seen TEXT,
  UNIQUE(platform, url_key));
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(platform, status);
CREATE INDEX IF NOT EXISTS idx_jobs_cap ON jobs(salary_cap);

CREATE TABLE IF NOT EXISTS apps(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER, platform TEXT, action TEXT, ok INTEGER, msg TEXT, at TEXT);
CREATE INDEX IF NOT EXISTS idx_apps_job ON apps(job_id);

CREATE TABLE IF NOT EXISTS blacklist(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  company TEXT UNIQUE, source TEXT, reason TEXT,
  active INTEGER DEFAULT 1, created_at TEXT);

CREATE TABLE IF NOT EXISTS follow_up(
  job_id INTEGER PRIMARY KEY,
  state TEXT, read_at TEXT, last_msg_at TEXT, attempts INTEGER DEFAULT 0,
  next_check_at TEXT);
CREATE INDEX IF NOT EXISTS idx_fu_due ON follow_up(state, next_check_at);

CREATE TABLE IF NOT EXISTS resume(
  id INTEGER PRIMARY KEY CHECK (id=1), json TEXT, text TEXT, updated_at TEXT);

CREATE TABLE IF NOT EXISTS resume_version(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER, target TEXT, json TEXT, notes TEXT, gaps TEXT,
  warnings TEXT, created_at TEXT);

CREATE TABLE IF NOT EXISTS plan(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  platform TEXT, keyword TEXT, city TEXT, filters_json TEXT, reason TEXT,
  source TEXT DEFAULT 'ai', enabled INTEGER DEFAULT 0, priority INTEGER DEFAULT 0,
  last_run_at TEXT, last_result TEXT);

CREATE TABLE IF NOT EXISTS settings(
  key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);

CREATE TABLE IF NOT EXISTS quota(
  day TEXT, platform TEXT, kind TEXT, used INTEGER DEFAULT 0,
  PRIMARY KEY(day, platform, kind));

CREATE TABLE IF NOT EXISTS cooldown(
  platform TEXT PRIMARY KEY, until_ts REAL, reason TEXT, trips INTEGER DEFAULT 0,
  trip_day TEXT, updated_at TEXT);

CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT, level TEXT DEFAULT 'info', platform TEXT, kind TEXT, msg TEXT);

CREATE TABLE IF NOT EXISTS hr_msg(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  platform TEXT, company TEXT, job_id INTEGER, text TEXT, hit_kw TEXT, at TEXT);

CREATE TABLE IF NOT EXISTS wizard(
  key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);

CREATE TABLE IF NOT EXISTS api_tpl(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  platform TEXT, kind TEXT, url TEXT, method TEXT,
  headers TEXT, body TEXT, anchor TEXT, note TEXT, saved_at TEXT,
  UNIQUE(platform, kind));
"""


def connect():
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(config.DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init():
    with connect() as con:
        con.executescript(SCHEMA)
        for col in ("ai_tier TEXT", "ai_why TEXT", "ai_at TEXT"):
            try:
                con.execute(f"ALTER TABLE jobs ADD COLUMN {col}")
            except Exception:
                pass      # 已存在
        # 跟进看板：stage=所处阶段，note=备注，reply_text=HR 回复原文
        for col in ("stage TEXT", "note TEXT", "reply_text TEXT",
                    "updated_at TEXT"):
            try:
                con.execute(f"ALTER TABLE follow_up ADD COLUMN {col}")
            except Exception:
                pass


def tpl_save(t):
    """HTTP 模板保存（platform+kind 唯一，重复保存=覆盖）。"""
    with connect() as con:
        con.execute(
            "INSERT INTO api_tpl(platform,kind,url,method,headers,body,anchor,note,saved_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(platform,kind) DO UPDATE SET"
            " url=excluded.url, method=excluded.method, headers=excluded.headers,"
            " body=excluded.body, anchor=excluded.anchor, note=excluded.note,"
            " saved_at=excluded.saved_at",
            (t["platform"], t.get("kind") or "search", t["url"],
             (t.get("method") or "GET").upper(),
             json.dumps(t.get("headers") or {}, ensure_ascii=False),
             t.get("body") or "",
             json.dumps(t.get("anchor") or {}, ensure_ascii=False),
             t.get("note") or "", _now()))


def tpl_get(platform, kind="search"):
    with connect() as con:
        r = con.execute("SELECT * FROM api_tpl WHERE platform=? AND kind=?",
                        (platform, kind)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["headers"] = json.loads(d["headers"] or "{}")
        d["anchor"] = json.loads(d["anchor"] or "{}")
        return d


def tpl_list():
    with connect() as con:
        out = []
        for r in con.execute("SELECT * FROM api_tpl ORDER BY platform, kind"):
            d = dict(r)
            d["headers"] = json.loads(d["headers"] or "{}")
            d["anchor"] = json.loads(d["anchor"] or "{}")
            out.append(d)
        return out


def tpl_delete(id_):
    with connect() as con:
        con.execute("DELETE FROM api_tpl WHERE id=?", (id_,))


def delete_empty_jobs():
    """清理采集异常产生的空壳行（无标题）。返回条数。"""
    with connect() as con:
        return con.execute("DELETE FROM jobs WHERE title IS NULL OR title=''").rowcount


def set_ai(job_id, tier, why):
    with connect() as con:
        con.execute("UPDATE jobs SET ai_tier=?, ai_why=?, ai_at=? WHERE id=?",
                    (tier, why[:200], _now(), job_id))


def jobs_for_ai(platform=None):
    """需要 AI 判定的行：规则放行、还没判过。"""
    q = ("SELECT * FROM jobs WHERE status IN ('待投','待定-薪资未标','保留-低于底线(总监级)') "
         "AND (ai_tier IS NULL OR ai_tier='')")
    a = []
    if platform:
        q += " AND platform=?"
        a.append(platform)
    q += " ORDER BY id LIMIT 200"
    with connect() as con:
        return [dict(r) for r in con.execute(q, a)]


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------- jobs ----------------

def url_key(url):
    u = str(url or "").strip()
    if not u:
        return ""
    from urllib.parse import urlsplit
    try:
        return urlsplit(u).path or u[:120]
    except Exception:
        return u[:120]


def upsert_job(platform, job):
    """插入或刷新一条岗位。job: dict(title,company,salary,city,...,url,security_id,lid)。
    返回 (job_id, 'added'|'exists')。"""
    key = url_key(job.get("url"))
    if not key:
        return None, "bad"
    fields = dict(
        platform=platform, ext_id=job.get("ext_id") or key, url=job.get("url"),
        url_key=key, title=job.get("title") or "", company=job.get("company") or "",
        salary_text=job.get("salary") or "", salary_cap=job.get("salary_cap"),
        city=job.get("city") or "", area=job.get("area") or "",
        edu=job.get("edu") or "", exp=job.get("exp") or "",
        publish=job.get("publish") or "", match_note=job.get("match") or "",
        tier=job.get("tier") or "★★", score=job.get("score") or 0,
        security_id=job.get("security_id") or "", lid=job.get("lid") or "",
        plan_id=job.get("plan_id"), last_seen=_now())
    with connect() as con:
        row = con.execute("SELECT id, status FROM jobs WHERE platform=? AND url_key=?",
                          (platform, key)).fetchone()
        if row:
            sets = ", ".join(f"{k}=CASE WHEN ?='' THEN {k} ELSE ? END"
                             for k in fields if k not in ("platform", "url_key"))
            vals = []
            for k, v in fields.items():
                if k in ("platform", "url_key"):
                    continue
                vals += [v, v]
            con.execute(f"UPDATE jobs SET {sets}, first_seen=first_seen WHERE id=?",
                        vals + [row["id"]])
            return row["id"], "exists"
    cols = ", ".join(fields)
    marks = ", ".join("?" * len(fields))
    with connect() as con:
        cur = con.execute(
            f"INSERT INTO jobs({cols}, status, reason, first_seen) "
            f"VALUES({marks}, 'new', ?, ?)",
            list(fields.values()) + [job.get("status_reason") or "", _now()])
        return cur.lastrowid, "added"


def set_job_status(job_id, status, reason=""):
    with connect() as con:
        con.execute("UPDATE jobs SET status=?, reason=? WHERE id=?",
                    (status, reason, job_id))


def get_job(job_id):
    with connect() as con:
        r = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return dict(r) if r else None


def list_jobs(platform=None, status=None, min_cap=None):
    q = "SELECT * FROM jobs WHERE 1=1"
    a = []
    if platform:
        q += " AND platform=?"
        a.append(platform)
    if status:
        q += " AND status=?"
        a.append(status)
    if min_cap:
        q += " AND salary_cap>=?"
        a.append(int(min_cap))
    q += " ORDER BY score DESC, id DESC"
    with connect() as con:
        return [dict(r) for r in con.execute(q, a)]


def save_jd(job_id, jd):
    with connect() as con:
        con.execute("UPDATE jobs SET jd=?, jd_at=? WHERE id=?", (jd, _now(), job_id))


def delete_jobs(status="已关闭", platform=None):
    q = "DELETE FROM jobs WHERE status=?"
    a = [status]
    if platform:
        q += " AND platform=?"
        a.append(platform)
    with connect() as con:
        return con.execute(q, a).rowcount


def job_stats():
    with connect() as con:
        rows = con.execute("SELECT platform, status, COUNT(*) c FROM jobs "
                           "GROUP BY platform, status").fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["platform"], {})[r["status"]] = r["c"]
    return out


# ---------------- apps / quota ----------------

def log_app(job_id, platform, action, ok, msg=""):
    with connect() as con:
        con.execute("INSERT INTO apps(job_id, platform, action, ok, msg, at) "
                    "VALUES(?,?,?,?,?,?)", (job_id, platform, action, int(bool(ok)),
                                            msg, _now()))


def apps_recent(limit=50):
    with connect() as con:
        return [dict(r) for r in con.execute(
            "SELECT a.*, j.title, j.company FROM apps a "
            "LEFT JOIN jobs j ON j.id=a.job_id ORDER BY a.id DESC LIMIT ?", (limit,))]


def quota_used(platform, kind, day=None):
    with connect() as con:
        r = con.execute("SELECT used FROM quota WHERE day=? AND platform=? AND kind=?",
                        (day or date.today().isoformat(), platform, kind)).fetchone()
    return r["used"] if r else 0


def quota_add(platform, kind, n=1):
    with connect() as con:
        con.execute(
            "INSERT INTO quota(day, platform, kind, used) VALUES(?,?,?,?) "
            "ON CONFLICT(day, platform, kind) DO UPDATE SET used=used+excluded.used",
            (date.today().isoformat(), platform, kind, n))


def quota_left(platform, kind, cap):
    return max(0, int(cap) - quota_used(platform, kind))


# ---------------- settings / wizard ----------------

def setting(key, default=None):
    with connect() as con:
        r = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if r is None:
        return default
    try:
        return json.loads(r["value"])
    except Exception:
        return r["value"]


def set_setting(key, value):
    with connect() as con:
        con.execute("INSERT INTO settings(key, value, updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                    "updated_at=excluded.updated_at",
                    (key, json.dumps(value, ensure_ascii=False), _now()))


def all_settings():
    with connect() as con:
        rows = con.execute("SELECT key, value FROM settings").fetchall()
    out = {}
    for r in rows:
        try:
            out[r["key"]] = json.loads(r["value"])
        except Exception:
            out[r["key"]] = r["value"]
    return out


def wizard_get(key, default=None):
    with connect() as con:
        r = con.execute("SELECT value FROM wizard WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def wizard_set(key, value):
    with connect() as con:
        con.execute("INSERT INTO wizard(key, value, updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, str(value), _now()))


# ---------------- blacklist / resume / plan / events ----------------

def blacklist_add(company, source="auto", reason=""):
    company = str(company or "").strip()
    if not company:
        return
    with connect() as con:
        con.execute("INSERT INTO blacklist(company, source, reason, active, created_at) "
                    "VALUES(?,?,?,1,?) ON CONFLICT(company) DO UPDATE SET "
                    "active=1, reason=excluded.reason",
                    (company, source, reason, _now()))


def blacklist_has(company):
    company = str(company or "").strip()
    if not company:
        return False
    with connect() as con:
        r = con.execute("SELECT 1 FROM blacklist WHERE company=? AND active=1",
                        (company,)).fetchone()
    return r is not None


def blacklist_list(active_only=True):
    q = "SELECT * FROM blacklist" + (" WHERE active=1" if active_only else "") + \
        " ORDER BY id DESC"
    with connect() as con:
        return [dict(r) for r in con.execute(q)]


def blacklist_deactivate(company):
    with connect() as con:
        con.execute("UPDATE blacklist SET active=0 WHERE company=?", (company,))


def resume_save(data):
    with connect() as con:
        con.execute("INSERT INTO resume(id, json, text, updated_at) VALUES(1,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET json=excluded.json, "
                    "text=excluded.text, updated_at=excluded.updated_at",
                    (json.dumps(data, ensure_ascii=False),
                     json.dumps(data, ensure_ascii=False)[:2], _now()))


def resume_load():
    with connect() as con:
        r = con.execute("SELECT json FROM resume WHERE id=1").fetchone()
    if not r:
        return {}
    try:
        return json.loads(r["json"])
    except Exception:
        return {}


def save_version(job_id, target, data, notes, gaps, warnings):
    with connect() as con:
        cur = con.execute(
            "INSERT INTO resume_version(job_id, target, json, notes, gaps, warnings, "
            "created_at) VALUES(?,?,?,?,?,?,?)",
            (job_id, target, json.dumps(data, ensure_ascii=False),
             json.dumps(notes, ensure_ascii=False), json.dumps(gaps, ensure_ascii=False),
             json.dumps(warnings, ensure_ascii=False), _now()))
        con.execute("UPDATE jobs SET customized=1 WHERE id=?", (job_id,))
        return cur.lastrowid


def versions(job_id=None):
    q = "SELECT id, job_id, target, notes, gaps, warnings, created_at FROM resume_version"
    a = []
    if job_id:
        q += " WHERE job_id=?"
        a.append(job_id)
    q += " ORDER BY id DESC LIMIT 50"
    with connect() as con:
        return [dict(r) for r in con.execute(q, a)]


def plan_list(only_enabled=False):
    q = "SELECT * FROM plan" + (" WHERE enabled=1" if only_enabled else "") + \
        " ORDER BY platform, priority, id"
    with connect() as con:
        return [dict(r) for r in con.execute(q)]


def plan_save(items, replace_platform=None):
    """items: [{platform, keyword, city, filters, reason, priority}]"""
    with connect() as con:
        if replace_platform:
            con.execute("DELETE FROM plan WHERE platform=?", (replace_platform,))
        for it in items:
            con.execute(
                "INSERT INTO plan(platform, keyword, city, filters_json, reason, "
                "source, enabled, priority) VALUES(?,?,?,?,?,?,?,?)",
                (it.get("platform"), it.get("keyword") or "", it.get("city") or "",
                 json.dumps(it.get("filters") or {}, ensure_ascii=False),
                 it.get("reason") or "", it.get("source") or "ai",
                 int(it.get("enabled", 0)), int(it.get("priority", 0))))


def plan_set(id_, **fields):
    allow = ("enabled", "keyword", "city", "priority", "filters_json", "reason")
    sets = {k: v for k, v in fields.items() if k in allow}
    if not sets:
        return
    cols = ", ".join(f"{k}=?" for k in sets)
    with connect() as con:
        con.execute(f"UPDATE plan SET {cols} WHERE id=?", list(sets.values()) + [id_])


def plan_done(id_, result):
    with connect() as con:
        con.execute("UPDATE plan SET last_run_at=?, last_result=? WHERE id=?",
                    (_now(), str(result)[:200], id_))


def plan_delete_many(ids):
    """批量删除计划行，返回实际删除数。"""
    ids = [int(i) for i in ids or []]
    if not ids:
        return 0
    q = ",".join("?" * len(ids))
    with connect() as con:
        cur = con.execute(f"DELETE FROM plan WHERE id IN ({q})", ids)
        return cur.rowcount or 0


def event(level, platform, kind, msg):
    with connect() as con:
        con.execute("INSERT INTO events(ts, level, platform, kind, msg) VALUES(?,?,?,?,?)",
                    (_now(), level, platform or "", kind, str(msg)[:400]))


def events(limit=200, level=None, platform=None):
    q = "SELECT * FROM events WHERE 1=1"
    a = []
    if level:
        q += " AND level=?"
        a.append(level)
    if platform:
        q += " AND platform=?"
        a.append(platform)
    q += f" ORDER BY id DESC LIMIT {int(limit)}"
    with connect() as con:
        return [dict(r) for r in con.execute(q, a)][::-1]


def hr_msg_log(platform, company, job_id, text, hit_kw):
    with connect() as con:
        con.execute("INSERT INTO hr_msg(platform, company, job_id, text, hit_kw, at) "
                    "VALUES(?,?,?,?,?,?)",
                    (platform, company, job_id, str(text)[:400], hit_kw, _now()))


# ---------------- follow_up ----------------

def fu_get(job_id):
    with connect() as con:
        r = con.execute("SELECT * FROM follow_up WHERE job_id=?", (job_id,)).fetchone()
    return dict(r) if r else None


def fu_set(job_id, **fields):
    allow = ("state", "read_at", "last_msg_at", "attempts", "next_check_at")
    cur = fu_get(job_id) or {"job_id": job_id, "state": "", "read_at": "",
                             "last_msg_at": "", "attempts": 0, "next_check_at": ""}
    for k, v in fields.items():
        if k in allow:
            cur[k] = v
    with connect() as con:
        con.execute(
            "INSERT INTO follow_up(job_id, state, read_at, last_msg_at, attempts, "
            "next_check_at) VALUES(?,?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET "
            "state=excluded.state, read_at=excluded.read_at, "
            "last_msg_at=excluded.last_msg_at, attempts=excluded.attempts, "
            "next_check_at=excluded.next_check_at",
            (job_id, cur["state"], cur["read_at"], cur["last_msg_at"],
             cur["attempts"], cur["next_check_at"]))


def fu_due(now_iso):
    with connect() as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM follow_up WHERE state='read_no_reply' "
            "AND next_check_at<=? ORDER BY next_check_at", (now_iso,))]


# ---------------- 跟进看板（零触达：全部由用户手动更新） ----------------

STAGES = ["applied", "read", "replied", "interview", "offer", "rejected", "closed"]
STAGE_NAMES = {"applied": "已投递", "read": "已读", "replied": "HR有回复",
               "interview": "面试中", "offer": "Offer", "rejected": "已婉拒",
               "closed": "关闭"}

# AI 匹配度排序权重（越小越靠前）
TIER_ORDER = {"强匹配": 0, "匹配": 1, "弱匹配": 2, "不匹配": 3}


def pipeline_set(job_id, stage=None, note=None, reply_text=None):
    """更新一条岗位的跟进阶段。只写本地库，不访问平台。"""
    cur = fu_get(job_id) or {"job_id": job_id, "state": "", "read_at": "",
                             "last_msg_at": "", "attempts": 0, "next_check_at": "",
                             "stage": "", "note": "", "reply_text": ""}
    if stage is not None:
        cur["stage"] = stage if stage in STAGES else ""
    if note is not None:
        cur["note"] = str(note)[:500]
    if reply_text is not None:
        cur["reply_text"] = str(reply_text)[:2000]
    with connect() as con:
        con.execute(
            "INSERT INTO follow_up(job_id, state, read_at, last_msg_at, attempts, "
            "next_check_at, stage, note, reply_text, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(job_id) DO UPDATE SET stage=excluded.stage, "
            "note=excluded.note, reply_text=excluded.reply_text, "
            "updated_at=excluded.updated_at",
            (job_id, cur.get("state") or "", cur.get("read_at") or "",
             cur.get("last_msg_at") or "", cur.get("attempts") or 0,
             cur.get("next_check_at") or "", cur.get("stage") or "",
             cur.get("note") or "", cur.get("reply_text") or "", _now()))
    return cur


def _job_fu_join(where="", args=(), order="", limit=None):
    """jobs LEFT JOIN follow_up 的通用查询。"""
    sql = ("SELECT j.*, f.stage AS p_stage, f.note AS p_note, "
           "f.reply_text AS p_reply, f.updated_at AS p_at "
           "FROM jobs j LEFT JOIN follow_up f ON f.job_id=j.id " + where + order)
    if limit:
        sql += f" LIMIT {int(limit)}"
    with connect() as con:
        return [dict(r) for r in con.execute(sql, args)]


def _tier_rank(col="j.ai_tier"):
    cases = " ".join(f"WHEN {col}='{k}' THEN {v}" for k, v in TIER_ORDER.items())
    return f"CASE {cases} ELSE 4 END"


def pipeline_list(stage=None):
    """看板：只列已进入看板的岗位（stage 非空）。stage=None 表示全部阶段。"""
    if stage:
        where, args = "WHERE f.stage=?", [stage]
    else:
        where, args = "WHERE f.stage IS NOT NULL AND f.stage<>''", []
    order = (f" ORDER BY {_tier_rank()} ASC, j.score DESC, j.id DESC")
    return _job_fu_join(where, args, order)


def pipeline_counts():
    """各阶段计数（用于看板表头）。"""
    out = {s: 0 for s in STAGES}
    with connect() as con:
        for r in con.execute("SELECT stage, COUNT(*) n FROM follow_up "
                             "WHERE stage IS NOT NULL AND stage<>'' GROUP BY stage"):
            if r["stage"] in out:
                out[r["stage"]] = r["n"]
    return out


def apply_list(platform=None, limit=None, tiers=("强匹配", "匹配")):
    """今日投递清单：可投状态 + 按 AI 匹配度排序。零触达，纯本地查询。"""
    where = ("WHERE (j.status='待投' OR j.status LIKE '保留%') "
             "AND (f.stage IS NULL OR f.stage NOT IN "
             "('interview','offer','rejected','closed'))")
    args = []
    if tiers:
        ph = ",".join("?" * len(tiers))
        where += f" AND (j.ai_tier IN ({ph}) OR j.ai_tier IS NULL OR j.ai_tier='')"
        args += list(tiers)
    if platform:
        where += " AND j.platform=?"
        args.append(platform)
    order = f" ORDER BY {_tier_rank()} ASC, j.score DESC, j.salary_cap DESC"
    return _job_fu_join(where, args, order, limit)
