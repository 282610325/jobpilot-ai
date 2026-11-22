"""简历文件解析：pdf/docx/html/txt/md → 纯文本。"""
import re
import zipfile


def extract_text(path):
    p = str(path).lower()
    if p.endswith(".pdf"):
        return _pdf(path)
    if p.endswith(".docx"):
        return _docx(path)
    if p.endswith((".html", ".htm")):
        return _html(open(path, encoding="utf-8", errors="ignore").read())
    return open(path, encoding="utf-8", errors="ignore").read()


def _pdf(path):
    from pypdf import PdfReader
    r = PdfReader(path)
    return "\n".join((pg.extract_text() or "") for pg in r.pages)


def _docx(path):
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", "ignore")
    xml = re.sub(r"</w:p>", "\n", xml)
    return _html(xml)


def _html(s):
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<br[^>]*>|</p>|</div>|</li>|</h\d>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
    s = s.replace("&gt;", ">").replace("&quot;", '"')
    return re.sub(r"[ \t]+", " ", s).strip()
