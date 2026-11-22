"""支持城市：只存城市名，城市码由系统自动匹配。

- BOSS：从官方公开城市列表（wapi/zpCommon/data/city.json）自动补齐并缓存到
  settings.city_codes_boss；用户永远不需要填写编码。
- 智联：保存城市时由 platforms/city_api.py 在在线模式下打开一次搜索页，
  调平台自身的 search/base/data 拿全量城市表，缓存到 settings.city_codes_zl；
  内置 config.ZL_CITY 只作为离线兜底。
"""
import config
import db
import requests

SETTING = "support_cities"
BOSS_CACHE = "city_codes_boss"
ZL_CACHE = "city_codes_zl"
BOSS_URL = "https://www.zhipin.com/wapi/zpCommon/data/city.json"

DEFAULT = list(config.CITIES)


def _clean_names(items):
    out = []
    for it in items or []:
        name = it.get("name") if isinstance(it, dict) else it
        name = str(name or "").strip()
        if name and name not in out:
            out.append(name)
    return out


def names():
    saved = db.setting(SETTING, None)
    if isinstance(saved, list):
        cleaned = _clean_names(saved)      # 兼容旧格式 [{name,zl,boss}]
        if cleaned:
            return cleaned
    return list(DEFAULT)


def save(items):
    cleaned = _clean_names(items)
    if not cleaned:
        raise ValueError("至少保留一个城市")
    db.set_setting(SETTING, cleaned)       # 只存名字
    _refresh_boss(cleaned)                 # 尽力补齐 BOSS 码
    return cleaned


def _boss_cache():
    c = db.setting(BOSS_CACHE, {})
    return dict(c) if isinstance(c, dict) else {}


def map_zl():
    """智联城市码：内置常用城市 + 自动抓取的智联城市表缓存。"""
    m = dict(config.ZL_CITY)
    c = db.setting(ZL_CACHE, {})
    if isinstance(c, dict):
        for k, v in c.items():
            try:
                m[k] = int(v)
            except (TypeError, ValueError):
                continue
    return m


def save_zl_codes(mapping):
    """把智联城市表（name→jl 码）合并进缓存。"""
    if not mapping:
        return
    cur = db.setting(ZL_CACHE, {})
    cur = dict(cur) if isinstance(cur, dict) else {}
    for k, v in (mapping or {}).items():
        try:
            cur[str(k)] = int(v)
        except (TypeError, ValueError):
            continue
    db.set_setting(ZL_CACHE, cur)


def map_boss():
    m = dict(config.BOSS_CITY)
    m.update({k: v for k, v in _boss_cache().items() if v})
    miss = [n for n in names() if n not in m]
    if miss:
        m.update(_refresh_boss(miss))
    return m


def _refresh_boss(wanted):
    """从 BOSS 公开城市列表补齐缺失城市码；失败静默返回 {}。"""
    cache = _boss_cache()
    miss = [n for n in wanted if n not in cache and n not in config.BOSS_CITY]
    if not miss:
        return {}
    try:
        r = requests.get(BOSS_URL, timeout=20)
        data = r.json()
    except Exception:
        return {}
    found = {}

    def walk(lst):
        for x in lst or []:
            if not isinstance(x, dict):
                continue
            nm = str(x.get("name") or "").strip()
            code = x.get("code")
            if nm in miss and code:
                c = str(code)
                # 同名有省级/市级：优先市级（BOSS 市级码以 100 结尾）
                if nm not in found or c.endswith("100"):
                    found[nm] = c
            walk(x.get("subLevelModelList"))

    zp = (data or {}).get("zpData") or {}
    for k in ("hotCityList", "cityList"):
        walk(zp.get(k))
    if found:
        cache.update(found)
        db.set_setting(BOSS_CACHE, cache)
    return found


def status():
    zl, boss = map_zl(), map_boss()
    return [{"name": n, "zl": bool(zl.get(n)), "boss": bool(boss.get(n))}
            for n in names()]
