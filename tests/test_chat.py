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
