import json
import os
import httpx
from typing import Any
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

# 初始化 MCP 服务
mcp = FastMCP(
    name="Weather",
    instructions="该服务提供全球城市实时天气查询能力，传入城市中英文名称即可获取温度、天气状况",
    version="1.0.0"
)

# API 配置 — 密钥从 .env 读取，不硬编码
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY", "")
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


@mcp.tool()
async def query_weather(city: str) -> str:
    """
    查询指定城市的天气信息。
    :param city: 城市名称（中文或英文，如 "Beijing" 或 "北京"）
    """
    data = await fetch_weather_data(city)
    if data is None:
        return "无法获取天气数据，请稍后再试。"
    return format_weather(data)


if __name__ == "__main__":
    # 启动 MCP 服务（stdio 模式适合终端运行，http 模式适合服务器部署）
    mcp.run(transport="stdio")
