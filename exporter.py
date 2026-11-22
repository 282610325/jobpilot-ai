"""SQLite → Excel 一键导出（jobs+apps+blacklist 三表，写到 config.EXPORT_DIR）。"""
import time

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font

import config
import db

JOB_COLS = ["id", "platform", "status", "title", "company", "salary_text", "city",
            "area", "edu", "exp", "tier", "score", "reason", "url", "jd_at",
            "first_seen", "last_seen"]
JOB_HDR = ["ID", "平台", "状态", "职位", "公司", "薪资", "城市", "区域", "学历",
           "经验", "匹配", "分数", "备注/原因", "链接", "JD读取", "首见", "最近"]
APP_HDR = ["时间", "平台", "动作", "结果", "说明", "岗位ID"]
BL_HDR = ["公司", "来源", "原因", "启用", "时间"]


def to_xlsx():
    config.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    name = f"jobpilot-岗位-{time.strftime('%Y-%m-%d-%H%M')}.xlsx"
    path = config.EXPORT_DIR / name
    wb = Workbook()
    ws = wb.active
    ws.title = "岗位"
    ws.append(JOB_HDR)
    for j in db.list_jobs():
        ws.append([j.get(c) if j.get(c) is not None else "" for c in JOB_COLS])
    a = wb.create_sheet("投递记录")
    a.append(APP_HDR)
    with db.connect() as con:
        for r in con.execute("SELECT at, platform, action, ok, msg, job_id "
                             "FROM apps ORDER BY id DESC"):
            a.append([r[0], r[1], r[2], "成功" if r[3] else "未成", r[4], r[5]])
    b = wb.create_sheet("黑名单")
    b.append(BL_HDR)
    for x in db.blacklist_list(active_only=False):
        b.append([x["company"], x["source"], x["reason"],
                  "是" if x["active"] else "否", x["created_at"]])
    for sheet in (ws, a, b):
        for cell in sheet[1]:
            cell.font = Font(bold=True)
        for row in sheet.iter_rows():
            for cell in row:
                cell.alignment = Alignment(vertical="top", wrap_text=True)
    wb.save(path)
    return str(path)
