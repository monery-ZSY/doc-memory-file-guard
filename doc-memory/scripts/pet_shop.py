#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文档拾忆 · 宠物商店与签到积分
每日签到领积分 → 商店买宠物粮食 → 喂食互动；天气查询（宠物装扮用）。
数据存 data/pet_points.json；天气查询仅获取当地天气（IP 定位），不涉及本机任何文件数据。
"""

import json
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from indexer import DATA_DIR  # noqa: E402

POINTS_FILE = DATA_DIR / "pet_points.json"

# 粮食目录（价格/描述/吃后行为由前端播放）
FOODS = [
    {"id": "fish",   "name": "小鱼干",   "price": 10,  "emoji": "🐟",
     "desc": "经典零食，猫猫无法抗拒",
     "say": "小鱼干最香了！喵呜～一口一个！", "anim": "fish",  "fx": "spark"},
    {"id": "kibble", "name": "营养猫粮", "price": 20,  "emoji": "🍚",
     "desc": "日常主粮，吃饱又踏实",
     "say": "咔嚓咔嚓…日常口粮，稳稳的幸福！", "anim": "kibble", "fx": "note"},
    {"id": "jerky",  "name": "鸡肉冻干", "price": 30,  "emoji": "🍗",
     "desc": "酥脆高蛋白，精神百倍",
     "say": "酥酥脆脆！耳朵都竖起来了，精神百倍！", "anim": "jerky", "fx": "spark"},
    {"id": "can",    "name": "丰盛罐头", "price": 50,  "emoji": "🥫",
     "desc": "大餐级别，幸福冒泡",
     "say": "罐头大餐！幸福的味道在冒泡～", "anim": "can", "fx": "star"},
    {"id": "feast",  "name": "豪华妙鲜包", "price": 100, "emoji": "💝",
     "desc": "顶级美味，爱心雨来袭",
     "say": "哇！！主人最好啦！比心比心～", "anim": "feast", "fx": "heart"},
]
FOOD_MAP = {f["id"]: f for f in FOODS}

_lock = threading.Lock()
_weather_caches = {}   # {"auto": {ts,data}, "<城市>": {ts,data}}，各自 30 分钟


def _load():
    if not POINTS_FILE.exists():
        return {"points": 0, "streak": 0, "last": "", "hist": [], "inv": {}, "feeds": 0}
    try:
        d = json.loads(POINTS_FILE.read_text(encoding="utf-8"))
        for k, v in (("points", 0), ("streak", 0), ("last", ""), ("hist", []),
                     ("inv", {}), ("feeds", 0)):
            d.setdefault(k, v)
        return d
    except (OSError, ValueError):
        return {"points": 0, "streak": 0, "last": "", "hist": [], "inv": {}, "feeds": 0}


def _save(d):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        POINTS_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def _today():
    return date.today().isoformat()


def signin_gain(streak):
    """基础 10 分 + 连签加成（每天 +2，封顶 +20）。"""
    return 10 + min(20, (streak - 1) * 2)


def status():
    """签到页/商店页所需的全部状态。"""
    with _lock:
        d = _load()
    t = _today()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    # 补签窗口：若 last 是更早的日期，streak 显示不影响，签到时归 1
    today_signed = d["last"] == t
    week = []
    for i in range(6, -1, -1):
        dt = date.today() - timedelta(days=i)
        week.append({"date": dt.isoformat(), "day": "周" + "一二三四五六日"[dt.weekday()],
                     "signed": dt.isoformat() in d["hist"]})
    inv = []
    for f in FOODS:
        inv.append({"id": f["id"], "name": f["name"], "emoji": f["emoji"],
                    "count": int(d["inv"].get(f["id"], 0))})
    # next_gain：下一次签到实际能得的积分。
    # · 今日已签 → 下一次是明天，连签延续（streak+1）
    # · 昨天签过 → 下一次是今天，连签延续（streak+1）
    # · 从未签过（第一天）或断签 → 归 1（基础 10 分）
    if d["last"] in (t, yesterday):
        next_gain = signin_gain(d["streak"] + 1)
    else:
        next_gain = signin_gain(1)
    return {"points": d["points"], "streak": d["streak"], "last": d["last"],
            "today_signed": today_signed, "feeds": d["feeds"],
            "next_gain": next_gain,
            "week": week, "inventory": inv, "foods": FOODS}


def do_signin():
    with _lock:
        d = _load()
        t = _today()
        if d["last"] == t:
            return {"error": "今天已经签到过啦，明天再来～", "points": d["points"],
                    "streak": d["streak"], "today_signed": True}
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        d["streak"] = d["streak"] + 1 if d["last"] == yesterday else 1
        gained = signin_gain(d["streak"])
        d["points"] += gained
        d["last"] = t
        d["hist"].append(t)
        d["hist"] = d["hist"][-90:]
        _save(d)
    return {"ok": True, "gained": gained, "points": d["points"], "streak": d["streak"]}


def buy(food_id):
    with _lock:
        d = _load()
        f = FOOD_MAP.get(food_id)
        if not f:
            return {"error": "没有这种粮食～"}
        if d["points"] < f["price"]:
            return {"error": f"积分不够（还差 {f['price'] - d['points']} 分），先去签到吧～",
                    "points": d["points"]}
        d["points"] -= f["price"]
        d["inv"][food_id] = int(d["inv"].get(food_id, 0)) + 1
        _save(d)
    return {"ok": True, "name": f["name"], "points": d["points"],
            "count": d["inv"][food_id]}


def feed(food_id):
    with _lock:
        d = _load()
        f = FOOD_MAP.get(food_id)
        if not f:
            return {"error": "没有这种粮食～"}
        if int(d["inv"].get(food_id, 0)) <= 0:
            return {"error": "库存里没有「%s」了，先去商店买点吧～" % f["name"]}
        d["inv"][food_id] = int(d["inv"][food_id]) - 1
        d["feeds"] += 1
        _save(d)
    return {"ok": True, "name": f["name"], "say": f["say"], "anim": f["anim"],
            "fx": f["fx"], "count": d["inv"][food_id]}


# ---------------- 天气（仅查询当地天气，30 分钟缓存，失败静默降级） ----------------

def _wmo_type(code, wind_kmh):
    if code in (71, 73, 75, 77, 85, 86):
        return "snow"
    if code in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99):
        return "rain"
    if wind_kmh is not None and wind_kmh >= 30:
        return "wind"
    return "sun"


def _get(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (doc-memory pet)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def _zh_city(en):
    """常见城市英文→中文（定位漂移到境外时也给出可读名）。"""
    m = {"beijing": "北京", "shanghai": "上海", "guangzhou": "广州", "shenzhen": "深圳",
         "hangzhou": "杭州", "nanjing": "南京", "chengdu": "成都", "chongqing": "重庆",
         "wuhan": "武汉", "xian": "西安", "suzhou": "苏州", "tianjin": "天津",
         "changsha": "长沙", "zhengzhou": "郑州", "qingdao": "青岛", "dalian": "大连",
         "xiamen": "厦门", "fuzhou": "福州", "hefei": "合肥", "kunming": "昆明",
         "shenyang": "沈阳", "harbin": "哈尔滨", "shijiazhuang": "石家庄", "nanning": "南宁",
         "guiyang": "贵阳", "lanzhou": "兰州", "taiyuan": "太原", "haikou": "海口",
         "hong kong": "香港", "kowloon": "九龙", "taipa": "氹仔", "macau": "澳门",
         "taipei": "台北", "chung kwai chung": "葵青", "tsuen wan": "荃湾",
         "shatin": "沙田", "yuen long": "元朗", "tuen mun": "屯门", "kwai chung": "葵涌"}
    return m.get((en or "").strip().lower(), en)


def _weather_by_city(city):
    """按城市名查天气：Open-Meteo 地理编码（支持中文）→ 经纬度 → 当前天气。"""
    city = (city or "").strip()
    if not city:
        return None
    try:
        g = _get("https://geocoding-api.open-meteo.com/v1/search?name="
                 + urllib.parse.quote(city) + "&count=1&language=zh", 10)
        res = (g.get("results") or [None])[0]
        if not res:
            return {"type": "unknown", "temp": "", "city": city, "label": "",
                    "error": f"没找到城市「{city}」，换个写法试试（如：郑州 / 上海）"}
        lat, lon = res["latitude"], res["longitude"]
        w = _get("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s"
                 "&current=weather_code,wind_speed_10m,temperature_2m&timezone=auto"
                 % (lat, lon), 10)
        cur = w.get("current", {})
        code = int(cur.get("weather_code", 0))
        wind = cur.get("wind_speed_10m")
        t = _wmo_type(code, wind * 3.6 if wind is not None else None)
        # 城市名：地理编码返回的中文名（含省/国时拼接更明确）
        name = res.get("name") or city
        admin = res.get("admin1") or ""
        disp = (admin + "·" + name) if admin and admin != name else name
        return {"type": t, "temp": round(cur.get("temperature_2m", 0)),
                "city": disp,
                "label": {"sun": "晴", "rain": "雨", "snow": "雪", "wind": "大风"}.get(t, "")}
    except Exception:
        return None


def _weather_online():
    # IP 自动定位（出口走代理/VPN 时可能漂移到境外，仅供未填城市时兜底）
    try:
        ip = _get("http://ip-api.com/json/?fields=status,lat,lon,city", 8)
        if ip.get("status") != "success":
            return None
        w = _get("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s"
                 "&current=weather_code,wind_speed_10m,temperature_2m&timezone=auto"
                 % (ip["lat"], ip["lon"]), 10)
        cur = w.get("current", {})
        code = int(cur.get("weather_code", 0))
        wind = cur.get("wind_speed_10m")
        t = _wmo_type(code, wind * 3.6 if wind is not None else None)
        return {"type": t, "temp": round(cur.get("temperature_2m", 0)),
                "city": _zh_city(ip.get("city", "")) + "（IP定位，可能漂移）",
                "label": {"sun": "晴", "rain": "雨", "snow": "雪", "wind": "大风"}.get(t, "")}
    except Exception:
        pass
    try:
        j = _get("https://wttr.in/?format=j1", 10)
        cur = j["current_condition"][0]
        code = int(cur.get("weatherCode", 0))
        wind = float(cur.get("windspeedKmph", 0) or 0)
        t = _wmo_type(code, wind)
        try:
            city = _zh_city(j["nearest_area"][0]["areaName"][0]["value"])
        except (KeyError, IndexError):
            city = ""
        return {"type": t, "temp": cur.get("temp_C", ""), "city": city + "（IP定位，可能漂移）",
                "label": {"sun": "晴", "rain": "雨", "snow": "雪", "wind": "大风"}.get(t, "")}
    except Exception:
        return None


def get_weather(city=None, force=False):
    """city 为空 → 用调用方传入的默认城市；均无则 IP 自动定位。
    成功结果缓存 30 分钟；失败结果只缓存 60 秒（网络恢复后快速自愈），
    且整体重试一次再判定失败。"""
    key = (city or "").strip().lower() or "auto"
    now = time.time()
    cache = _weather_caches.get(key)
    ttl = 60 if (cache and cache["data"] and cache["data"].get("error")) else 1800
    if not force and cache and cache["data"] and now - cache["ts"] < ttl:
        return cache["data"]
    has_city = bool((city or "").strip())
    data = _weather_by_city(city) if has_city else _weather_online()
    if data is None:  # 网络抖动常见：整体重试一次
        data = _weather_by_city(city) if has_city else _weather_online()
    if data is None:
        data = {"type": "unknown", "temp": "", "city": (city or "").strip(),
                "label": "", "error": "天气服务暂时连不上（可能是网络/代理波动），稍后再试～"}
    _weather_caches[key] = {"ts": now, "data": data}
    return data
