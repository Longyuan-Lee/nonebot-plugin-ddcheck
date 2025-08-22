import json
import math
import time
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Optional
import asyncio
import hashlib
from urllib.parse import quote_plus

import httpx
import jinja2
from nonebot.log import logger
from nonebot_plugin_apscheduler import scheduler
from nonebot_plugin_htmlrender import html_to_pic
from nonebot_plugin_localstore import get_cache_dir

from .config import ddcheck_config

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 "
        "Safari/537.36 Edg/126.0.0.0"
    ),
    "Referer": "https://www.bilibili.com/",
}

data_path = get_cache_dir("nonebot_plugin_ddcheck")
vtb_list_path = data_path / "vtb_list.json"

dir_path = Path(__file__).parent
template_path = dir_path / "template"
env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(template_path), enable_async=True
)

raw_cookie = ddcheck_config.bilibili_cookie
cookie = SimpleCookie()
cookie.load(raw_cookie)
cookies = {key: value.value for key, value in cookie.items()}

# 用于 WBI 签名的动态密钥
_cached_wbi_keys = {"img_key": "", "sub_key": "", "last_fetch": 0}

async def get_wbi_keys() -> tuple[str, str]:
    """获取用于 WBI 签名的动态密钥"""
    global _cached_wbi_keys
    current_time = time.time()
    
    # 密钥缓存有效期为30分钟
    if current_time - _cached_wbi_keys["last_fetch"] < 1800:
        return _cached_wbi_keys["img_key"], _cached_wbi_keys["sub_key"]
    
    wbi_url = "https://api.bilibili.com/x/web-interface/nav"
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(wbi_url, headers=HEADERS, cookies=cookies)
            resp.raise_for_status()
            data = resp.json()["data"]
            wbi_img_url = data["wbi_img"]["img_url"]
            wbi_sub_url = data["wbi_img"]["sub_url"]

            img_key = wbi_img_url.split("/")[-1].split(".")[0]
            sub_key = wbi_sub_url.split("/")[-1].split(".")[0]

            _cached_wbi_keys["img_key"] = img_key
            _cached_wbi_keys["sub_key"] = sub_key
            _cached_wbi_keys["last_fetch"] = current_time
            
            logger.info("成功获取并更新WBI签名密钥。")
            return img_key, sub_key
        except Exception as e:
            logger.error(f"获取WBI密钥失败: {e}")
            raise

def get_wbi_sign_params(params: dict) -> dict:
    """计算WBI签名参数"""
    img_key, sub_key = _cached_wbi_keys["img_key"], _cached_wbi_keys["sub_key"]
    
    mixinKeyEncTab = [
        46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
        33, 9, 42, 19, 29, 28, 14, 36, 17, 20, 34, 44, 57, 16, 26, 56, 1, 40, 52, 37,
        55, 11, 41, 4, 22, 24, 21, 25, 54, 59, 7, 60, 6, 61, 51, 62, 2, 53, 8, 23, 32,
        15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 36,
        17, 20, 34, 44, 57, 16, 26, 56, 1, 40, 52, 37, 55, 11, 41, 4, 22, 24, 21, 25,
        54, 59, 7, 60, 6, 61, 51, 62
    ]
    
    def get_mixin_key(orig_key):
        return "".join([orig_key[i] for i in mixinKeyEncTab[:64]])
        
    mixin_key = get_mixin_key(img_key + sub_key)
    
    params["wts"] = int(time.time())
    
    # 将参数进行键名排序
    sorted_params = dict(sorted(params.items()))
    
    # 拼接查询字符串并进行URL编码
    query = "&".join([
        f"{key}={quote_plus(str(value))}" 
        for key, value in sorted_params.items()
    ])
    
    # 计算签名
    hash_value = hashlib.md5((query + mixin_key).encode('utf-8')).hexdigest()
    
    sorted_params["w_rid"] = hash_value
    return sorted_params

async def update_vtb_list():
    vtb_list = []
    urls = [
        "https://api.vtbs.moe/v1/short",
        "https://cfapi.vtbs.moe/v1/short",
        "https://hkapi.vtbs.moe/v1/short",
        "https://kr.vtbs.moe/v1/short",
    ]
    async with httpx.AsyncClient() as client:
        for url in urls:
            try:
                resp = await client.get(url, timeout=20)
                result = resp.json()
                if not result:
                    continue
                for info in result:
                    if info.get("uid", None) and info.get("uname", None):
                        vtb_list.append(
                            {"mid": int(info["uid"]), "uname": info["uname"]}
                        )
                    if info.get("mid", None) and info.get("uname", None):
                        vtb_list.append(info)
                break
            except httpx.TimeoutException:
                logger.warning(f"Get {url} timeout")
            except Exception:
                logger.exception(f"Error when getting {url}, ignore")
    dump_vtb_list(vtb_list)

scheduler.add_job(
    update_vtb_list,
    "cron",
    hour=3,
    id="update_vtb_list",
)

def load_vtb_list() -> list[dict]:
    if vtb_list_path.exists():
        with vtb_list_path.open("r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.decoder.JSONDecodeError:
                logger.warning("vtb列表解析错误，将重新获取")
                vtb_list_path.unlink()
    return []

def dump_vtb_list(vtb_list: list[dict]):
    data_path.mkdir(parents=True, exist_ok=True)
    json.dump(
        vtb_list,
        vtb_list_path.open("w", encoding="utf-8"),
        indent=4,
        separators=(",", ": "),
        ensure_ascii=False,
    )

async def get_vtb_list() -> list[dict]:
    vtb_list = load_vtb_list()
    if not vtb_list:
        await update_vtb_list()
    return load_vtb_list()

async def get_uid_by_name(name: str) -> Optional[int]:
    """通过用户名获取UID"""
    await get_wbi_keys()
    
    url = "https://api.bilibili.com/x/web-interface/wbi/search/type"
    params = {"search_type": "bili_user", "keyword": name}
    params_signed = get_wbi_sign_params(params)

    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(url, params=params_signed, headers=HEADERS, cookies=cookies)
            resp.raise_for_status()
            result = resp.json()

            if result["code"] == 0 and result["data"] and result["data"]["result"]:
                for user in result["data"]["result"]:
                    if user["uname"] == name:
                        return user["mid"]
                logger.warning(f"搜索用户 '{name}' 成功，但未找到精确匹配项。")
            else:
                logger.error(f"API请求失败: {result.get('message', '未知错误')}")
        except Exception as e:
            logger.error(f"通过用户名获取UID失败: {e}")
    return None

async def get_user_info(uid: int) -> dict:
    """通过UID获取用户基本信息"""
    url = "https://api.bilibili.com/x/web-interface/card"
    params = {"mid": uid}
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, params=params, headers=HEADERS)
        result = resp.json()

        if result.get("code") == 0 and "data" in result:
            return result["data"]["card"]
        else:
            message = result.get("message", "无法从 API 获取用户信息。")
            raise ConnectionError(message)

async def get_user_attentions(uid: int) -> list[dict]:
    """使用WBI签名API获取完整的关注列表，返回包含用户名和UID的列表"""
    logger.info(f"正在使用WBI签名API获取用户 {uid} 的关注列表...")
    attentions_data = []
    page = 1
    page_size = 50
    try:
        await get_wbi_keys()
        
        async with httpx.AsyncClient(timeout=30) as client:
            while True:
                params = {
                    "vmid": uid,
                    "pn": page,
                    "ps": page_size,
                    "order": "desc",
                    "jsonp": "jsonp"
                }
                
                params_signed = get_wbi_sign_params(params)
                url = "https://api.bilibili.com/x/relation/followings"
                
                resp = await client.get(url, params=params_signed, headers=HEADERS, cookies=cookies)
                resp.raise_for_status()
                
                data = resp.json()["data"]
                
                if data["list"]:
                    for user in data["list"]:
                        # 核心修改：返回包含'uname'和'mid'的字典
                        attentions_data.append({"mid": user["mid"], "uname": user["uname"]})
                    
                    if len(data["list"]) < page_size:
                        break
                    page += 1
                else:
                    break

        logger.info(f"成功通过WBI签名API获取 {len(attentions_data)} 个关注者。")
        return attentions_data

    except Exception as e:
        logger.error(f"通过WBI签名API获取失败，请检查您的Cookie或等待Bilibili API更新。错误：{e}")
        return []

async def get_medal_list(uid: int) -> list[dict]:
    """获取粉丝勋章列表"""
    url = "https://api.live.bilibili.com/xlive/web-ucenter/user/MedalWall"
    params = {"target_id": uid}
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, params=params, headers=HEADERS, cookies=cookies)
        result = resp.json()
        if result.get("code") == 0 and result.get("data"):
            return result["data"]["list"]
        return []

def format_color(color: int) -> str:
    return f"#{color:06X}"

def format_vtb_info(info: dict, medal_dict: dict) -> dict:
    name = info["uname"]
    uid = info["mid"]
    medal = {}
    if name in medal_dict:
        medal_info = medal_dict[name]["medal_info"]
        medal = {
            "name": medal_info["medal_name"],
            "level": medal_info["level"],
            "color_border": format_color(medal_info["medal_color_border"]),
            "color_start": format_color(medal_info["medal_color_start"]),
            "color_end": format_color(medal_info["medal_color_end"]),
        }
    return {"name": name, "uid": uid, "medal": medal}

async def render_ddcheck_image(
    user_info: dict[str, Any], vtb_list: list[dict], attentions: list[int], medal_list: list[dict]
) -> bytes:
    follows_num = int(user_info["attention"])

    vtb_dict = {info["mid"]: info for info in vtb_list}
    medal_dict = {medal["target_name"]: medal for medal in medal_list}
    
    vtbs = [info for uid, info in vtb_dict.items() if uid in attentions]
    vtbs = [format_vtb_info(info, medal_dict) for info in vtbs]

    vtbs_num = len(vtbs)
    percent = vtbs_num / follows_num * 100 if follows_num else 0
    num_per_col = math.ceil(vtbs_num / math.ceil(vtbs_num / 100)) if vtbs_num else 1
    result = {
        "name": user_info["name"],
        "uid": user_info["mid"],
        "face": user_info["face"],
        "fans": user_info["fans"],
        "follows": follows_num,
        "percent": f"{percent:.2f}% ({vtbs_num}/{follows_num})",
        "vtbs": vtbs,
        "num_per_col": num_per_col,
    }
    template = env.get_template("info.html")
    content = await template.render_async(info=result)
    return await html_to_pic(content, wait=0, viewport={"width": 100, "height": 100})
