"""简历：SQLite 存取、纯文本化、A4 HTML 渲染、Chrome headless 导出 PDF。"""
import subprocess
import time
from pathlib import Path

import citycfg
import config
import db

EMPTY = {"name": "", "title": "", "intent": "", "lead": "", "contact": {},
         "meta": [], "metrics": [], "experience": [], "ai_projects": [],
         "other_projects": [], "early": [], "education": [], "certs": [],
         "focus": []}


def intent_cities(data=None):
    """从简历「求职意向」里提取城市（只保留系统支持的 5 城，按固定顺序）。"""
    d = data if data is not None else load()
    text = str((d or {}).get("intent") or "")
    return [c for c in citycfg.names() if c in text]


def load():
    d = db.resume_load()
    return {**EMPTY, **d} if d else dict(EMPTY)


def save(data):
    db.resume_save(data or {})
    return data


def to_text(data=None):
    d = data or load()
    L = []
    if d.get("name"):
        L.append(d["name"] + ("｜" + d["title"] if d.get("title") else ""))
    c = d.get("contact") or {}
    line = " ".join(filter(None, [c.get("city"), c.get("phone"), c.get("email"),
                                  c.get("status")]))
    if line:
        L.append(line)
    if d.get("intent"):
        L.append("求职意向：" + d["intent"])
    for m in d.get("metrics") or []:
        L.append(f"- {m.get('value','')} {m.get('label','')}")
    if d.get("lead"):
        L.append("概述：" + d["lead"])
    for e in d.get("experience") or []:
        L.append(f"\n{e.get('company','')} {e.get('role','')} {e.get('period','')}")
        for b in e.get("bullets") or []:
            L.append("- " + b)
    for p in d.get("ai_projects") or []:
        L.append(f"\n[AI项目] {p.get('title','')} {p.get('role','')} {p.get('period','')}")
        for b in p.get("points") or []:
            L.append("- " + b)
    for p in d.get("other_projects") or []:
        L.append(f"\n[项目] {p.get('name','')} {p.get('period','')}：{p.get('text','')}")
    for e in d.get("early") or []:
        L.append(f"\n{e.get('company','')} {e.get('role','')} {e.get('period','')}：{e.get('text','')}")
    for ed in d.get("education") or []:
        L.append(f"[教育] {ed.get('school','')} {ed.get('major','')} {ed.get('degree','')} {ed.get('period','')}")
    if d.get("certs"):
        L.append("[证书] " + "、".join(filter(None, map(lambda x: x if isinstance(x, str) else x.get("name"), d["certs"]))))
    return "\n".join(L).strip()


CSS = """
@page{size:A4;margin:12mm 14mm}
body{font:10.5pt/1.55 "PingFang SC","Microsoft YaHei",sans-serif;color:#1a202c;
     max-width:190mm;margin:0 auto;padding:6mm}
h1{font-size:17pt;margin:0 0 2pt}
.sub{color:#4a5568;font-size:9.5pt;margin-bottom:8pt}
h2{font-size:11.5pt;color:#1e4d8c;border-bottom:1.5px solid #cbd5e0;
   padding-bottom:2pt;margin:11pt 0 5pt}
ul{margin:3pt 0 3pt 14px;padding:0} li{margin:2.5pt 0}
.metrics{display:flex;flex-wrap:wrap;gap:6pt;margin:6pt 0}
.metrics div{background:#eef4fb;border-radius:4pt;padding:4pt 8pt;font-size:9.5pt}
.metrics b{color:#1e4d8c;font-size:11pt}
.job{margin:6pt 0} .job .h{font-weight:600} .job .p{color:#718096;font-size:9pt;float:right}
.lead{background:#f7fafc;border-left:3px solid #1e4d8c;padding:5pt 8pt;margin:6pt 0}
"""


def render_html(d=None, target=""):
    d = d or load()
    c = d.get("contact") or {}
    parts = [f"<h1>{_e(d.get('name',''))}</h1>",
             f"<div class='sub'>{_e(d.get('title',''))} ｜ "
             + _e(" · ".join(filter(None, [c.get('city'), c.get('status'),
                                          c.get('phone'), c.get('email')]))) + "</div>"]
    if target:
        parts.append(f"<div class='sub'>应聘目标：{_e(target)}</div>")
    if d.get("intent"):
        parts.append(f"<div class='sub'>意向：{_e(d['intent'])}</div>")
    if d.get("metrics"):
        cells = "".join(
            f"<div><b>{_e(m.get('value',''))}</b> {_e(m.get('label',''))}</div>"
            for m in d["metrics"])
        parts.append(f"<div class='metrics'>{cells}</div>")
    if d.get("lead"):
        parts.append(f"<div class='lead'>{_e(d['lead'])}</div>")
    if d.get("experience"):
        parts.append("<h2>工作经历</h2>")
        for e in d["experience"]:
            parts.append(f"<div class='job'><div class='h'>{_e(e.get('company',''))} "
                         f"· {_e(e.get('role',''))}<span class='p'>{_e(e.get('period',''))}</span></div>")
            parts.append("<ul>" + "".join(f"<li>{_e(b)}</li>"
                                          for b in e.get("bullets") or []) + "</ul></div>")
    if d.get("ai_projects"):
        parts.append("<h2>AI / 数字化项目</h2>")
        for p in d["ai_projects"]:
            parts.append(f"<div class='job'><div class='h'>{_e(p.get('title',''))} · "
                         f"{_e(p.get('role',''))}<span class='p'>{_e(p.get('period',''))}</span></div>")
            parts.append("<ul>" + "".join(f"<li>{_e(b)}</li>"
                                          for b in p.get("points") or []) + "</ul></div>")
    for key, label in (("other_projects", "其他项目"), ("early", "早期经历")):
        if d.get(key):
            parts.append(f"<h2>{label}</h2>")
            for p in d[key]:
                nm = p.get("name") or p.get("company", "")
                parts.append(f"<div class='job'><div class='h'>{_e(nm)} · "
                             f"{_e(p.get('role',''))}<span class='p'>{_e(p.get('period',''))}</span></div>"
                             f"<div>{_e(p.get('text',''))}</div></div>")
    if d.get("education"):
        parts.append("<h2>教育</h2>")
        for ed in d["education"]:
            parts.append(f"<div>{_e(ed.get('school',''))} · {_e(ed.get('major',''))} · "
                         f"{_e(ed.get('degree',''))} <span class='p'>{_e(ed.get('period',''))}</span></div>")
    if d.get("certs"):
        names = [x if isinstance(x, str) else x.get("name", "") for x in d["certs"]]
        parts.append(f"<h2>证书与技能</h2><div>{_e('、'.join(filter(None, names)))}</div>")
    head = (f"求职简历 ｜ 生成 {time.strftime('%Y-%m-%d')}"
            + (f" ｜ {target}" if target else ""))
    return (f"<html><head><meta charset='utf-8'><title>{head}</title>"
            f"<style>{CSS}</style></head><body>" + "".join(parts) + "</body></html>")


def export(data=None, filename=None, pdf=True):
    """输出 HTML（必有）+ PDF（Chrome headless）到 EXPORT_DIR。返回 (html路径, pdf路径|None)。"""
    config.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    name = filename or f"简历-{(data or load()).get('name') or 'jobpilot'}-{time.strftime('%m%d-%H%M')}"
    html_path = config.EXPORT_DIR / (name + ".html")
    html_path.write_text(render_html(data or load()), encoding="utf-8")
    pdf_path = None
    if pdf and config.chrome_exists():
        pdf_path = config.EXPORT_DIR / (name + ".pdf")
        try:
            subprocess.run(
                [config.CHROME, "--headless", "--disable-gpu", "--no-pdf-header-footer",
                 f"--print-to-pdf={pdf_path}", str(html_path)],
                capture_output=True, timeout=90)
            if not pdf_path.exists():
                pdf_path = None
        except Exception:
            pdf_path = None
    return html_path, pdf_path


def pdf_error():
    """PDF 没生成时给用户的明确原因（不再静默只给 HTML）。"""
    if not config.chrome_exists():
        return ("未找到 Google Chrome，已生成 HTML，未生成 PDF。"
                "请安装 Chrome，或在 local_config.py / 环境变量 JOBPILOT_CHROME "
                "指定 chrome 路径后重试。")
    return ("PDF 生成失败（Chrome 打印超时或出错），HTML 已生成，"
            "可先用浏览器打开 HTML 再打印成 PDF。")


def _e(s):
    import html as H
    return H.escape(str(s or "").replace("\n", "<br>"))
