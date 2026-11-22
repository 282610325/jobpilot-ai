"""智联城市表：在已登录页面上下文调用 search/base/data，解析 allCity。

该接口是智联搜索页自己就会发的请求，返回省→市→区树；我们只取「城市级」
（含 4 个直辖市），缓存到 settings.city_codes_zl，用户只填城市名即可。
"""
from browser import Blocked

JOBS_URL = "https://www.zhaopin.com/jobs/"

FETCH_JS = r"""
return (async()=>{
  let u=[...performance.getEntriesByType('resource')].map(e=>e.name)
    .find(x=>x.includes('/c/i/search/base/data'));
  if(!u){
    u='https://fe-api.zhaopin.com/c/i/search/base/data?platform=13&version=0.0.0';
  }
  const j=await (await fetch(u)).json();
  const ac=(j.data&&j.data.allCity)||[];
  const MUNI=['北京','上海','天津','重庆'];
  const out=[];
  for(const p of ac){
    if(MUNI.indexOf(p.name)>=0) out.push([p.name,String(p.code)]);
    for(const c of (p.sublist||[])){
      if(!/(区|县|旗)$/.test(c.name||'')) out.push([c.name,String(c.code)]);
    }
  }
  return out;
})();
"""


def fetch_zhaopin_cities(rt):
    """返回 {城市名: 智联 jl 码}。需要已登录的浏览器（走页面自身接口）。"""
    rt.goto(JOBS_URL)
    rt.page.wait_for_timeout(4500)
    rows = rt.js(FETCH_JS)
    if not rows:                      # base/data 可能还没发出，再等一轮
        rt.page.wait_for_timeout(3000)
        rows = rt.js(FETCH_JS)
    if not rows:
        raise Blocked("没拿到智联城市表（search/base/data 未返回）")
    out = {}
    for name, code in rows:
        try:
            out[str(name).strip()] = int(code)
        except (TypeError, ValueError):
            continue
    if not out:
        raise Blocked("智联城市表为空")
    return out
