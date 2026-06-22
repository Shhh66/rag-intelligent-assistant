import json
import httpx
from typing import Any
from config import OPENWEATHER_API_KEY

OPENWEATHER_BASE_URL = "https://api.openweathermap.org/data/2.5/weather"
USER_AGENT = "weather-app/1.0"



async def fetch_weather_data(city: str) -> dict[str, Any] | None:
    """调用 OpenWeatherMap API 获取城市天气数据。"""
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
            print(f"HTTP 错误: {e.response.status_code} - {e.response.text}")
        except httpx.RequestError as e:
            print(f"请求错误: {e}")
        except Exception as e:
            print(f"未知错误: {e}")
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
