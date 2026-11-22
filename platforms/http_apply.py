"""投递：BOSS 走 /wapi/zpgeek/friend/add.json 纯 HTTP（新鲜 securityId）。

调研结论（2026-11）：
- BOSS friend/add 被 job_hunter / boss-cli / Super_Employee / boss-agent-cli 四个
  开源项目实际使用，Cookie 直连可行；业务码 9/31/32/36/37/17 由 netio 统一刹停。
- 智联 cgate App 网关投递代码陈旧（硬编码 2024 时间戳、缺 x-zp-at 会话值），
  猎聘求职侧纯 API 投递无任何开源先例（ldms 签名需真页面）——不假装可行，
  这两平台默认仍走浏览器通道，想试 API 需先抓"投递请求"模板（后续扩展）。
"""
import citycfg
import config
import netio
from browser import Blocked

GREET_URL = "https://www.zhipin.com/wapi/zpgeek/friend/add.json"
CHAT_REFERER = "https://www.zhipin.com/web/geek/chat"


def boss_greet(job):
    """一次 HTTP 沟通。返回 (ok, msg)。风控码已由 netio 拦截为 Blocked。"""
    sid = job.get("security_id") or ""
    jid = job.get("ext_id") or ""
    if not sid or not jid:
        return False, "缺新鲜 securityId（链接过期），重新采集后再投"
    form = {"securityId": sid, "jobId": jid}
    if job.get("lid"):
        form["lid"] = job["lid"]
    _st, data = netio.request("boss", "POST", GREET_URL, form=form,
                              referer=CHAT_REFERER,
                              extra_headers=netio.site_headers("boss"))
    if not isinstance(data, dict):
        return False, "沟通接口返回异常"
    code = data.get("code")
    msg = str(data.get("message") or "")
    if code == 0 or msg in ("Success", "投递成功"):
        return True, "已发起沟通（API）"
    if "上限" in msg or "频繁" in msg:
        return False, f"沟通达上限：{msg[:50]}"
    if "已" in msg and ("沟通" in msg or "聊天" in msg):
        return False, "已沟通"
    return False, f"API 沟通失败 code={code} {msg[:60]}"


def probe(platform, log):
    """连通/登录态测试：真实调一次搜索接口（pageSize 尽量小）。"""
    import api_tpl
    from platforms import http_collect
    if platform == "boss":
        from platforms import boss_api
        _st, data = netio.request(
            "boss", "GET", api_tpl.builtin("boss")["url"],
            params={"query": "IT经理", "city": citycfg.map_boss().get("珠海"),
                    "page": 1, "pageSize": 5},
            referer=config.BOSS_JOBS, quiet=True,
            extra_headers=netio.site_headers("boss"))
        jobs = boss_api.parse_joblist(data if isinstance(data, dict) else {})
        if jobs:
            return True, f"BOSS OK：{len(jobs)} 条，样例「{jobs[0]['title']}」"
        code = data.get("code") if isinstance(data, dict) else "?"
        return False, f"BOSS 返回 0 条（code={code}）：Cookie 可能未登录或被限"
    tpl = api_tpl.get(platform)
    if not tpl:
        return False, "还没初始化接口（点「② 自动初始化」即可）"
    url, body = api_tpl.render(tpl, page=1)
    import json as _json
    kw_body = None
    form_body = None
    if body:
        try:
            kw_body = _json.loads(body)
        except ValueError:
            form_body = body
    st, data = netio.request(platform, tpl["method"], url,
                             json_body=kw_body,
                             form=None if kw_body is not None else form_body,
                             referer=tpl["url"].split("?")[0],
                             extra_headers={k: v for k, v in
                                            (tpl.get("headers") or {}).items()
                                            if k != "referer"}, quiet=True)
    if not isinstance(data, dict):
        return False, f"没拿到 JSON（status={st}）：模板/Cookie 失效，重新抓包"
    from platforms.http_collect import _find_rows
    rows = _find_rows(data)
    if rows:
        return True, (f"{config.PLATFORM_NAMES[platform]} OK：{len(rows)} 条，"
                      f"样例键 {sorted(list(rows[0].keys()))[:8]}")
    return False, "接口通了但列表为空（可能未登录/签名头过期，重新抓包）"
