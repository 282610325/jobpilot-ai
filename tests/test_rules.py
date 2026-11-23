import rules


def test_salary_cap_forms():
    assert rules.salary_cap("1.5-3万·14薪") == 30000
    assert rules.salary_cap("20-30K") == 30000
    assert rules.salary_cap("8000-12000元") == 12000
    assert rules.salary_cap("面议") is None
    assert rules.salary_cap("30-45K·13薪") == 45000


def test_verdict_hard_cap_20k():
    # 上限≤20K 固定过滤，底线调低也不救
    st, why = rules.verdict({"title": "IT经理", "city": "珠海", "salary": "15-20K"}, floor=15000)
    assert st == "不投-薪资低"
    assert "硬过滤" in why


def test_verdict_below_floor_normal():
    st, why = rules.verdict({"title": "IT经理", "city": "深圳", "salary": "20-25K"}, floor=27000)
    assert st == "不投-薪资低"


def test_verdict_director_low_kept():
    st, _ = rules.verdict({"title": "信息化总监", "city": "珠海", "salary": "20-25K"}, floor=27000)
    assert st == "保留-低于底线(总监级)"
    st, _ = rules.verdict({"title": "IT总监", "city": "中山", "salary": "22-26K"}, floor=27000)
    assert st == "保留-低于底线(总监级)"
    # 广州总监级不保留
    st, _ = rules.verdict({"title": "IT总监", "city": "广州", "salary": "22-26K"}, floor=27000)
    assert st == "不投-薪资低"


def test_verdict_hard_gate_and_blacklist():
    st, why = rules.verdict({"title": "IT经理", "city": "珠海", "salary": "30-40K",
                             "edu": "统招本科"}, floor=27000)
    assert st == "不投-硬门槛"
    st, why = rules.verdict({"title": "运维工程师", "city": "珠海", "salary": "30-40K"},
                            floor=27000)
    assert st == "不投-岗位偏离"


def test_verdict_ok_and_unmarked():
    st, _ = rules.verdict({"title": "数字化经理", "city": "珠海", "salary": "30-40K"},
                          floor=27000)
    assert st == "待投"
    st, _ = rules.verdict({"title": "IT经理", "city": "珠海", "salary": "面议"}, floor=27000)
    assert st == "待定-薪资未标"


def test_hr_reject_hit():
    assert rules.hr_reject_hit("不好意思，您的经历不太合适") in ("不合适", "不太合适")
    assert rules.hr_reject_hit("方便发个简历吗") is None
    # 实际 BOSS 回复的措辞：「暂时与该职位不够匹配」
    assert rules.hr_reject_hit(
        "感谢你的关注，暂时与该职位不够匹配，有合适机会之后再沟通～") is not None
    assert rules.hr_reject_hit("好的，明天下午方便面试吗？") is None


def test_score_tiers():
    tier, s = rules.score("IT总监", "珠海", "40-60K")
    assert tier == "★★★"
    tier, s2 = rules.score("项目经理", "东莞", "15-20K")
    assert s2 < s
