import json
import sys
import httpx
from typing import Any
from openai import OpenAI
from config import OPENWEATHER_API_KEY, GROQ_API_KEY, GROQ_BASE_URL, LLM_MODEL

OPENWEATHER_BASE_URL = "https://api.openweathermap.org/data/2.5/weather"
USER_AGENT = "weather-app/1.0"


# 常见城市中英对照表（LLM 翻译失败时的兜底）
_CITY_MAP = {
    "北京": "Beijing", "上海": "Shanghai", "深圳": "Shenzhen",
    "广州": "Guangzhou", "杭州": "Hangzhou", "成都": "Chengdu",
    "武汉": "Wuhan", "南京": "Nanjing", "重庆": "Chongqing",
    "西安": "Xi'an", "天津": "Tianjin", "苏州": "Suzhou",
    "长沙": "Changsha", "郑州": "Zhengzhou", "东莞": "Dongguan",
    "青岛": "Qingdao", "沈阳": "Shenyang", "宁波": "Ningbo",
    "昆明": "Kunming", "大连": "Dalian", "厦门": "Xiamen",
    "合肥": "Hefei", "佛山": "Foshan", "福州": "Fuzhou",
    "哈尔滨": "Harbin", "济南": "Jinan", "温州": "Wenzhou",
    "长春": "Changchun", "石家庄": "Shijiazhuang", "常州": "Changzhou",
    "泉州": "Quanzhou", "南宁": "Nanning", "贵阳": "Guiyang",
    "南昌": "Nanchang", "太原": "Taiyuan", "烟台": "Yantai",
    "嘉兴": "Jiaxing", "南通": "Nantong", "金华": "Jinhua",
    "珠海": "Zhuhai", "惠州": "Huizhou", "徐州": "Xuzhou",
    "海口": "Haikou", "乌鲁木齐": "Urumqi", "兰州": "Lanzhou",
    "呼和浩特": "Hohhot", "银川": "Yinchuan", "西宁": "Xining",
    "拉萨": "Lhasa", "台北": "Taipei", "香港": "Hong Kong",
    "澳门": "Macau", "东京": "Tokyo", "首尔": "Seoul",
    "曼谷": "Bangkok", "新加坡": "Singapore", "伦敦": "London",
    "纽约": "New York", "巴黎": "Paris", "悉尼": "Sydney",
}


def translate_city(city: str) -> str:
    """将中文城市名翻译成英文（使用配置的 LLM API）。

    翻译失败或返回空时，回退为原文（OpenWeatherMap 支持中文城市名）。
    """
    # 1. 优先查本地对照表（零延迟、零成本）
    if city in _CITY_MAP:
        english = _CITY_MAP[city]
        print(f"   📖 本地翻译: {city} → {english}", file=sys.stderr, flush=True)
        return english

    # 2. 调用 LLM 翻译
    try:
        client = OpenAI(
            api_key=GROQ_API_KEY,
            base_url=GROQ_BASE_URL,
            timeout=10.0,
        )
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{
                "role": "user",
                "content": f"将以下城市名翻译成英文，只输出英文城市名，不要任何解释：{city}"
            }],
            temperature=0,
            max_tokens=20,
        )
        translated = resp.choices[0].message.content.strip()
        if translated:
            print(f"   🌐 LLM 翻译: {city} → {translated}", file=sys.stderr, flush=True)
            return translated
    except Exception as e:
        print(f"   ⚠️ 翻译失败: {e}，使用原文", file=sys.stderr, flush=True)

    # 3. 最终回退
    return city




async def fetch_weather_data(city: str) -> dict[str, Any] | None:
    """调用 OpenWeatherMap API 获取城市天气数据。"""
    city = translate_city(city)
    params = {
        "q": city,
        "appid": OPENWEATHER_API_KEY,
        "units": "metric",
        "lang": "zh_cn",
    }
    headers = {"User-Agent": USER_AGENT}
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(
                OPENWEATHER_BASE_URL, params=params, headers=headers, timeout=30.0
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            print(f"HTTP 错误: {e.response.status_code} - {e.response.text}", file=sys.stderr, flush=True)
        except httpx.RequestError as e:
            print(f"请求错误: {e}", file=sys.stderr, flush=True)
        except Exception as e:
            print(f"未知错误: {e}", file=sys.stderr, flush=True)
    return None


def format_weather(data: dict[str, Any] | str) -> str:
    """将天气 API 响应格式化为可读字符串。"""
    # 如果是字符串，先解析为字典
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            return "无法解析天气数据。"

    # 检查 API 是否返回了错误
    if "error" in data:
        return f"错误: {data['error']}"

    # 提取数据，做容错处理
    city = data.get("name", "未知城市")
    country = data.get("sys", {}).get("country", "未知国家")
    temperature = data.get("main", {}).get("temp", "未知温度")
    humidity = data.get("main", {}).get("humidity", "未知湿度")
    wind_speed = data.get("wind", {}).get("speed", "未知风速")
    weather_list = data.get("weather", [{}])
    weather_description = weather_list[0].get("description", "未知天气")

    return (
        f"城市: {city}, {country}\n"
        f"温度: {temperature}°C\n"
        f"湿度: {humidity}%\n"
        f"风速: {wind_speed} m/s\n"
        f"天气: {weather_description}"
    )
