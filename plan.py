"""采集计划：校验、预算分配、启用状态。"""
import citycfg
import config


def validate(items):
    """清洗 AI/人工编辑的计划行，返回 (合法列表, 丢弃说明)。"""
    ok, dropped = [], []
    for it in items or []:
        p = (it.get("platform") or "").strip()
        if p not in ("zhaopin", "liepin", "boss"):
            dropped.append(f"未知平台 {p}")
            continue
        kw = (it.get("keyword") or "").strip()
        if p != "liepin" and not kw:
            dropped.append(f"{p} 缺关键词")
            continue
        if p == "zhaopin" and len(kw) > 12:
            dropped.append(f"智联关键词过长：{kw}")
            continue
        city = (it.get("city") or "").strip()
        if city and city not in citycfg.names():
            dropped.append(f"不支持城市 {city}")
            continue
        it = dict(it, platform=p, keyword=kw, city=city or None)
        ok.append(it)
    return ok, dropped


def budget(platform, n_combos):
    """页数预算：单平台单次 ≤ SESSION_PAGE_BUDGET 页。
    返回每个(词×城)组合允许翻几页（≥1）。BOSS 另受 pages_per_round 限制。"""
    n = max(1, int(n_combos))
    pages = max(1, min(int(config.RATE[platform]["pages_per_round"]),
                       config.SESSION_PAGE_BUDGET // n))
    return pages


def summary(items):
    out = {}
    for it in items:
        out.setdefault(it["platform"], []).append(it)
    return {p: len(v) for p, v in out.items()}
