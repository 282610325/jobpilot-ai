"""聊天机制：HR 拒绝识别、公司名清洗、时间窗过滤。"""
import datetime

import chat


def test_clean_company_strips_hr_title():
    assert chat.clean_company("吴女士传音控股") == "传音控股"
    assert chat.clean_company("李小姐某某集团") == "某某集团"
    assert chat.clean_company("深圳市某某科技有限公司") == "深圳市某某科技有限公司"
    assert chat.clean_company("张先生") == "张先生"


def test_valid_company_filters_dates_and_names():
    assert chat.valid_company("传音控股")
    assert chat.valid_company("深圳市某某科技有限公司")
    assert not chat.valid_company("11月21日")
    assert not chat.valid_company("昨天")
    assert not chat.valid_company("吴女士")
    assert not chat.valid_company("招聘主管")      # 职位不是公司


def test_chat_job_ok_rejects_hr_labels():
    assert not chat._chat_job_ok("HR")
    assert not chat._chat_job_ok("猎头")
    assert not chat._chat_job_ok("11月21日")
    assert not chat._chat_job_ok("统招")          # 猎聘头部解析误把学历词当岗位
    assert not chat._chat_job_ok("本科")
    assert chat._chat_job_ok("数字化发展部总监")


def test_import_external_filters_and_counts(monkeypatch):
    """只补录公司+岗位都有效的会话；脏数据跳过。"""
    calls = []
    monkeypatch.setattr(chat.db, "import_chat_job",
                        lambda p, c, j, note="": calls.append((p, c, j)) or "added")
    monkeypatch.setattr(chat.time, "sleep", lambda *a: None)
    rows = [
        {"company": "天虹股份", "job": "数字化发展部总监", "txt": "你好"},
        {"company": "", "job": "项目经理", "txt": "x"},
        {"company": "某公司", "job": "HR", "txt": "x"},
        {"company": "11月21日", "job": "项目经理", "txt": "x"},
    ]
    added, linked = chat._import_external(rows, lambda m: None, "zhaopin")
    assert (added, linked) == (1, 0)
    assert calls == [("zhaopin", "天虹股份", "数字化发展部总监")]


def test_parse_lp_header_extracts_applied_job():
    txt = ("梁女士 华为 · 财经数字化工程师 IT经理/主管 本科 3-5年 "
           "30-50k·16薪 华为 深圳-龙岗区")
    assert chat._parse_lp_header(txt) == "IT经理/主管"
    assert chat._parse_lp_header("") == ""


def test_import_deep_skips_known_company_and_caps(monkeypatch):
    """已在库的公司不点开；每轮只点开上限条数。"""
    calls = []
    monkeypatch.setattr(chat.db, "company_has_job",
                        lambda p, c: c == "已在库公司")
    monkeypatch.setattr(chat, "_boss_open_job", lambda rt, n, c: "项目经理")
    monkeypatch.setattr(chat.db, "import_chat_job",
                        lambda p, c, j, note="": calls.append((c, j)) or "added")
    monkeypatch.setattr(chat.time, "sleep", lambda *a: None)
    monkeypatch.setattr(chat, "DEEP_OPEN_MAX", 2)
    rows = [
        {"name": "甲", "company": "已在库公司"},
        {"name": "乙", "company": "新公司A"},
        {"name": "丙", "company": "新公司B"},
        {"name": "丁", "company": "新公司C"},
    ]
    added, linked = chat._import_deep(object(), rows, lambda m: None, "boss")
    assert (added, linked) == (2, 0)
    assert calls == [("新公司A", "项目经理"), ("新公司B", "项目经理")]


def test_row_recent_accepts_platform_formats():
    """智联「昨天 09:49」、猎聘「11月22日」、BOSS「星期日」都要认出时间窗。"""
    now = datetime.datetime(2026, 11, 24, 10, 0)   # 周一
    assert chat.row_recent("昨天 09:49", 3, now)
    assert chat.row_recent("昨天 04:34", 3, now)
    assert chat.row_recent("11月22日", 3, now)
    assert chat.row_recent("11月21日", 3, now)
    assert not chat.row_recent("11月20日", 3, now)
    assert not chat.row_recent("11月18日", 3, now)
    assert chat.row_recent("星期日", 3, now)         # 昨天是周日
    assert chat.row_recent("刚刚", 3, now)
    assert not chat.row_recent("", 3, now)           # 识别不了 → 不纳入


def test_is_self_msg_filters_my_templates():
    assert chat._is_self_msg("😄 你好呀，这个岗位我很感兴趣，请看下我的在线简历")
    assert chat._is_self_msg("[未读]这是我的简历，合适的话可以随时联系我～")
    assert chat._is_self_msg("在线简历")
    assert not chat._is_self_msg("感谢你的关注，很遗憾岗位与你不是很匹配")


def test_row_recent_by_time_window():
    now = datetime.datetime(2026, 11, 23, 15, 0)
    assert chat.row_recent("14:37", 3, now)          # 今天
    assert chat.row_recent("昨天", 3, now)
    assert chat.row_recent("前天", 3, now)
    assert chat.row_recent("11-21", 3, now)          # 2 天前
    assert not chat.row_recent("11-18", 3, now)      # 5 天前
    assert chat.row_recent("2026-11-20", 3, now)
    assert not chat.row_recent("2026-11-10", 3, now)
    assert not chat.row_recent("更早", 3, now)
