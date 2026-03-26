#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IPTV 播放列表聚合器
功能：
1. 用 output 复制替换 input
2. 使用 GitHub Token 搜索获取更多 M3U 和 TXT 文件
3. 合并所有数据并进行去重
4. 输出 M3U 和 TXT 格式文件
"""

import os
import re
import logging
import requests
import asyncio
import aiohttp
import time
import shutil
import argparse
import warnings
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Set
from dataclasses import dataclass, field

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

INPUT_M3U_FILE = 'input.m3u'
INPUT_TXT_FILE = 'input.txt'
OUTPUT_M3U_FILE = 'output.m3u'
OUTPUT_TXT_FILE = 'output.txt'
FAILED_LOG_FILE = 'failed_sources.log'
GITHUB_LOG_FILE = 'github_sources.log'
ALIAS_FILE = 'alias.txt'
DEMO_FILE = 'demo.txt'
ALIAS_DEMO_FILE = 'new-aliasdemo.txt'
README_FILE = 'README.md'
BLACKLIST_FILE = 'channel_blacklist.txt'
BLACKLIST_VALID_HOURS = 7 * 24
MAX_GITHUB_RESULTS = 500
MAX_RETRIES = 3

VALIDITY_CHECK_TIMEOUT = 2
VALIDITY_CHECK_CONCURRENCY = 100
VALIDITY_CHECK_BATCH_SIZE = 500

GITHUB_TOKEN = os.getenv("GH_TOKEN", "")

M3U_SEARCH_STRATEGIES = [
    {
        "name": "IPTV M3U 格式",
        "query": '#EXTM3U in:file extension:m3u',
        "format_type": "m3u"
    },
    {
        "name": "中文 IPTV 源",
        "query": '#EXTINF 央视 in:file extension:m3u',
        "format_type": "m3u"
    },
    {
        "name": "卫视 IPTV 源",
        "query": '#EXTINF 卫视 in:file extension:m3u',
        "format_type": "m3u"
    }
]

TXT_SEARCH_STRATEGIES = [
    {
        "name": "IPTV TXT 格式",
        "query": '#genre# in:file extension:txt iptv',
        "format_type": "txt"
    },
    {
        "name": "直播源 TXT 格式",
        "query": 'rtp:// in:file extension:txt',
        "format_type": "txt"
    },
    {
        "name": "直播源 TXT 格式2",
        "query": 'rtmp:// in:file extension:txt',
        "format_type": "txt"
    }
]


@dataclass
class Channel:
    """
    频道数据类
    存储单个频道的所有信息
    """
    name: str
    url: str
    tvg_id: str = ""
    tvg_logo: str = ""
    group_title: str = "未分类"
    extra_attrs: Dict[str, str] = field(default_factory=dict)
    
    def __hash__(self):
        return hash(self.url)
    
    def __eq__(self, other):
        if isinstance(other, Channel):
            return self.url == other.url
        return False


def replace_input_with_output():
    """
    用 output 文件替换 input 文件
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    
    output_m3u = os.path.join(script_dir, OUTPUT_M3U_FILE)
    output_txt = os.path.join(script_dir, OUTPUT_TXT_FILE)
    input_m3u = os.path.join(script_dir, INPUT_M3U_FILE)
    input_txt = os.path.join(script_dir, INPUT_TXT_FILE)
    
    if os.path.exists(output_m3u):
        shutil.copyfile(output_m3u, input_m3u)
        logger.info(f"✅ {OUTPUT_M3U_FILE} 已复制到 {INPUT_M3U_FILE}")
    
    if os.path.exists(output_txt):
        shutil.copyfile(output_txt, input_txt)
        logger.info(f"✅ {OUTPUT_TXT_FILE} 已复制到 {INPUT_TXT_FILE}")


def get_blacklist_path():
    """获取黑名单文件路径"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(script_dir, BLACKLIST_FILE)


def load_blacklist():
    """
    加载黑名单并检查是否在有效期内
    
    Returns:
        (blacklist_urls: set, is_valid: bool, created_time: datetime or None)
        - blacklist_urls: 黑名单中的URL集合
        - is_valid: 黑名单是否在有效期内
        - created_time: 黑名单创建时间（北京时间）
    """
    blacklist_path = get_blacklist_path()
    
    if not os.path.exists(blacklist_path):
        logger.info("📋 黑名单文件不存在，将创建新黑名单")
        return set(), False, None
    
    try:
        with open(blacklist_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except Exception as e:
        logger.warning(f"⚠️ 读取黑名单文件失败: {e}，将创建新黑名单")
        return set(), False, None
    
    if not lines:
        return set(), False, None
    
    created_time = None
    blacklist_urls = set()
    
    for line in lines:
        line = line.strip()
        if not line or line.startswith('#') or line.startswith('-'):
            if 'Created:' in line:
                try:
                    time_str = line.split('Created:')[1].strip()
                    created_time = datetime.strptime(time_str, '%Y-%m-%d %H:%M:%S')
                except:
                    pass
            continue
        
        if '|' in line:
            url = line.split('|')[0].strip()
            if url:
                blacklist_urls.add(url)
    
    if not blacklist_urls:
        return set(), False, None
    
    if created_time is None:
        logger.info(f"📋 黑名单无创建时间记录，视为过期，将重新生成")
        return blacklist_urls, False, None
    
    now_beijing = datetime.now() + timedelta(hours=8)
    created_beijing = created_time
    hours_diff = (now_beijing - created_beijing).total_seconds() / 3600
    
    is_valid = hours_diff < BLACKLIST_VALID_HOURS
    
    if is_valid:
        logger.info(f"📋 黑名单有效: {len(blacklist_urls)} 个URL, 剩余有效期: {BLACKLIST_VALID_HOURS - hours_diff:.1f} 小时")
    else:
        logger.info(f"📋 黑名单已过期 (已存在 {hours_diff:.1f} 小时)，将重新生成")
    
    return blacklist_urls, is_valid, created_time


def save_blacklist(blacklist_urls):
    """
    保存黑名单到文件
    
    Args:
        blacklist_urls: 黑名单URL集合
    """
    blacklist_path = get_blacklist_path()
    now_beijing = datetime.now() + timedelta(hours=8)
    now_str = now_beijing.strftime('%Y-%m-%d %H:%M:%S')
    
    lines = []
    lines.append(f"# 黑名单生成时间（UTC+8）\n")
    lines.append(f"# Created: {now_str}\n")
    lines.append(f"# 有效期（小时）: {BLACKLIST_VALID_HOURS}\n")
    lines.append(f"# -------------------\n")
    
    for url in sorted(blacklist_urls):
        lines.append(f"{url}|{now_str}\n")
    
    with open(blacklist_path, 'w', encoding='utf-8') as f:
        f.writelines(lines)
    
    logger.info(f"💾 已保存黑名单: {len(blacklist_urls)} 个URL到 {BLACKLIST_FILE}")


def filter_channels_by_blacklist(channels, blacklist_urls):
    """
    用黑名单过滤频道
    
    Args:
        channels: 频道列表
        blacklist_urls: 黑名单URL集合
        
    Returns:
        过滤后的频道列表（不在黑名单中的）
    """
    if not blacklist_urls:
        return channels
    
    filtered_channels = []
    filtered_count = 0
    
    for channel in channels:
        if channel.url not in blacklist_urls:
            filtered_channels.append(channel)
        else:
            filtered_count += 1
    
    if filtered_count > 0:
        logger.info(f"  🚫 黑名单过滤: 过滤了 {filtered_count} 个无效频道")
    
    return filtered_channels


def add_to_blacklist(blacklist_urls, channels):
    """
    将无效频道追加到黑名单
    
    Args:
        blacklist_urls: 现有黑名单URL集合
        channels: 被测试为无效的频道列表
        
    Returns:
        更新后的黑名单集合
    """
    added_count = 0
    for channel in channels:
        if channel.url not in blacklist_urls:
            blacklist_urls.add(channel.url)
            added_count += 1
    
    if added_count > 0:
        logger.info(f"  ➕ 黑名单新增: {added_count} 个无效频道")
    
    return blacklist_urls


def validate_token(token):
    """
    验证 GitHub Token 是否有效
    
    Args:
        token: GitHub Token
    """
    if not token:
        logger.warning("⚠️ 未设置 GitHub Token，将使用匿名访问（速率限制较低）")
        return
    try:
        r = requests.get(
            "https://api.github.com/search/code?q=test",
            headers={"Authorization": f"token {token}"},
            params={"per_page": 1}
        )
        if r.status_code in [200, 422]:
            logger.info("✅ GitHub Token 验证成功")
        elif r.status_code == 401:
            logger.error(f"❌ Token 无效: {r.status_code} - {r.text}")
        elif r.status_code == 403:
            logger.warning("⚠️ Token 权限受限（可能是 GITHUB_TOKEN），但搜索功能可用")
        else:
            logger.warning(f"⚠️ Token 验证返回: {r.status_code}")
    except Exception as e:
        logger.error(f"❌ Token 请求异常: {e}")


def github_headers():
    """
    获取 GitHub API 请求头
    
    Returns:
        请求头字典
    """
    return {
        "Accept": "application/vnd.github.v3+json",
        "Authorization": f"token {GITHUB_TOKEN}"
    }


def log_failed(url, reason):
    """
    记录失败的请求
    
    Args:
        url: 请求的 URL
        reason: 失败原因
    """
    timestamp = datetime.now()
    timestamp_beijing = timestamp + timedelta(hours=8)
    timestamp_str = timestamp_beijing.strftime('%Y-%m-%d %H:%M:%S')
    script_dir = os.path.dirname(os.path.abspath(__file__))
    log_path = os.path.join(script_dir, FAILED_LOG_FILE)
    with open(log_path, 'a', encoding='utf-8') as f:
        f.write(f"[{timestamp_str}] {url} - {reason}\n")


def log_github_source(url, status, extra=None):
    """
    记录 GitHub 源信息
    
    Args:
        url: GitHub 文件 URL
        status: 状态
        extra: 额外信息
    """
    timestamp = datetime.now()
    timestamp_beijing = timestamp + timedelta(hours=8)
    timestamp_str = timestamp_beijing.strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{timestamp_str}] {status} - {url}"
    if extra:
        line += f" - {extra}"
    script_dir = os.path.dirname(os.path.abspath(__file__))
    log_path = os.path.join(script_dir, GITHUB_LOG_FILE)
    with open(log_path, 'a', encoding='utf-8') as f:
        f.write(line + "\n")


def search_github_files(strategy):
    """
    根据搜索策略执行 GitHub 搜索
    
    Args:
        strategy: 搜索策略
        
    Returns:
        (搜索结果列表, 格式类型)
    """
    url = "https://api.github.com/search/code"
    params = {
        "q": strategy["query"],
        "sort": "indexed",
        "order": "desc",
        "per_page": 100
    }
    
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(url, headers=github_headers(), params=params, timeout=15)
            response.raise_for_status()
            items = response.json().get("items", [])[:MAX_GITHUB_RESULTS]
            logger.info(f"  📦 策略[{strategy['name']}] 找到 {len(items)} 个文件")
            return items, strategy["format_type"]
        except Exception as e:
            logger.warning(f"⚠️ GitHub 搜索失败（尝试 {attempt}/{MAX_RETRIES}）：{e}")
            time.sleep(2)
    
    return [], strategy["format_type"]


def fetch_github_file(item):
    """
    获取 GitHub 文件内容
    
    Args:
        item: GitHub 搜索结果项
        
    Returns:
        文件内容字符串
    """
    raw_url = item["html_url"].replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")
    repo = item.get("repository", {})
    author = repo.get("owner", {}).get("login", "unknown")
    updated_at = repo.get("updated_at", "unknown")
    size_kb = item.get("size", 0) / 1024
    
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(raw_url, headers=github_headers(), timeout=10)
            response.raise_for_status()
            content = response.content.decode('utf-8-sig')
            log_github_source(
                raw_url,
                "✅ 获取成功",
                f"作者: {author}, 更新时间: {updated_at}, 大小: {size_kb:.1f} KB"
            )
            return content, raw_url
        except Exception as e:
            if attempt == MAX_RETRIES:
                log_github_source(
                    raw_url,
                    "❌ 请求失败",
                    f"作者: {author}, 错误: {str(e)}"
                )
            time.sleep(1)
    
    return None, raw_url


def parse_m3u_content(content, source_url=""):
    """
    解析 M3U 格式内容
    
    Args:
        content: M3U 文件内容
        source_url: 来源 URL
        
    Returns:
        频道列表
    """
    channels = []
    
    if not content:
        return channels
    
    lines = content.strip().split('\n')
    current_channel = None
    
    for line in lines:
        line = line.strip()
        
        if not line:
            continue
            
        if line.startswith('#EXTM3U'):
            continue
            
        if line.startswith('#EXTINF'):
            current_channel = parse_extinf_line(line)
        elif line.startswith('#EXTVLCOPT:'):
            if current_channel:
                parse_vlcopt_line(line, current_channel)
        elif line.startswith('http') or line.startswith('rtmp') or line.startswith('rtsp') or line.startswith('rtp'):
            if current_channel:
                current_channel.url = line
                channels.append(current_channel)
                current_channel = None
    
    return channels


def parse_extinf_line(line):
    """
    解析 #EXTINF 行
    
    Args:
        line: #EXTINF 行内容
        
    Returns:
        频道对象
    """
    channel = Channel(name="", url="")
    
    tvg_id_match = re.search(r'tvg-id="([^"]*)"', line)
    if tvg_id_match:
        channel.tvg_id = tvg_id_match.group(1)
    
    tvg_logo_match = re.search(r'tvg-logo="([^"]*)"', line)
    if tvg_logo_match:
        channel.tvg_logo = tvg_logo_match.group(1)
    
    group_title_match = re.search(r'group-title="([^"]*)"', line)
    if group_title_match:
        channel.group_title = group_title_match.group(1)
    
    http_referrer_match = re.search(r'http-referrer="([^"]*)"', line)
    if http_referrer_match:
        channel.extra_attrs['http-referrer'] = http_referrer_match.group(1)
    
    http_user_agent_match = re.search(r'http-user-agent="([^"]*)"', line)
    if http_user_agent_match:
        channel.extra_attrs['http-user-agent'] = http_user_agent_match.group(1)
    
    last_comma_pos = line.rfind(',')
    if last_comma_pos != -1:
        channel.name = line[last_comma_pos + 1:].strip()
    
    return channel


def parse_vlcopt_line(line, channel):
    """
    解析 #EXTVLCOPT 行
    
    Args:
        line: #EXTVLCOPT 行内容
        channel: 频道对象
    """
    if 'http-referrer=' in line:
        match = re.search(r'http-referrer=(.+)', line)
        if match:
            channel.extra_attrs['http-referrer'] = match.group(1).strip()
    elif 'http-user-agent=' in line:
        match = re.search(r'http-user-agent=(.+)', line)
        if match:
            channel.extra_attrs['http-user-agent'] = match.group(1).strip()


def parse_txt_content(content, source_url=""):
    """
    解析 TXT 格式内容
    
    Args:
        content: TXT 文件内容
        source_url: 来源 URL
        
    Returns:
        频道列表
    """
    channels = []
    current_group = "未分类"
    
    if not content:
        return channels
    
    lines = content.strip().split('\n')
    
    for line in lines:
        line = line.strip()
        
        if not line:
            continue
            
        if '#genre#' in line:
            genre_match = re.match(r'^(.+?),#genre#', line)
            if genre_match:
                current_group = genre_match.group(1).strip()
            continue
        
        if line.startswith('#'):
            continue
        
        if line.startswith('◆') or line.startswith('http'):
            continue
        
        if ',' in line:
            parts = line.split(',', 1)
            if len(parts) == 2:
                name = parts[0].strip()
                url = parts[1].strip()
                
                if url.startswith('http') or url.startswith('rtmp') or url.startswith('rtsp') or url.startswith('rtp'):
                    if ' ' not in url and '\t' not in url:
                        channel = Channel(
                            name=name,
                            url=url,
                            group_title=current_group
                        )
                        channels.append(channel)
    
    return channels


def parse_m3u_file(file_path):
    """
    解析 M3U 格式文件
    
    Args:
        file_path: M3U 文件路径
        
    Returns:
        频道列表
    """
    channels = []
    
    if not os.path.exists(file_path):
        logger.warning(f"文件不存在: {file_path}")
        return channels
    
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except UnicodeDecodeError:
        try:
            with open(file_path, 'r', encoding='gbk') as f:
                content = f.read()
        except Exception as e:
            logger.error(f"无法读取文件 {file_path}: {e}")
            return channels
    
    channels = parse_m3u_content(content, file_path)
    logger.info(f"从 {file_path} 解析出 {len(channels)} 个频道")
    return channels


def parse_txt_file(file_path):
    """
    解析 TXT 格式文件
    
    Args:
        file_path: TXT 文件路径
        
    Returns:
        频道列表
    """
    channels = []
    
    if not os.path.exists(file_path):
        logger.warning(f"文件不存在: {file_path}")
        return channels
    
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except UnicodeDecodeError:
        try:
            with open(file_path, 'r', encoding='gbk') as f:
                content = f.read()
        except Exception as e:
            logger.error(f"无法读取文件 {file_path}: {e}")
            return channels
    
    channels = parse_txt_content(content, file_path)
    logger.info(f"从 {file_path} 解析出 {len(channels)} 个频道")
    return channels


def merge_and_deduplicate(channel_lists):
    """
    合并多个频道列表并进行去重
    
    Args:
        channel_lists: 多个频道列表
        
    Returns:
        去重后的频道列表
    """
    seen_urls = set()
    merged_channels = []
    
    for channels in channel_lists:
        for channel in channels:
            if channel.url and channel.url not in seen_urls:
                seen_urls.add(channel.url)
                merged_channels.append(channel)
    
    logger.info(f"合并后共 {len(merged_channels)} 个频道（已去重）")
    return merged_channels


async def check_single_channel(session, channel, semaphore):
    """
    检查单个频道的有效性（异步）
    
    Args:
        session: aiohttp ClientSession
        channel: Channel 对象
        semaphore: 异步信号量
        
    Returns:
        (channel, is_valid): 频道和是否有效
    """
    async with semaphore:
        url = channel.url
        if not url:
            return channel, False
        
        try:
            timeout = aiohttp.ClientTimeout(total=VALIDITY_CHECK_TIMEOUT)
            async with session.head(url, timeout=timeout, allow_redirects=True, ssl=False) as response:
                if response.status < 400:
                    return channel, True
                if response.status == 404:
                    async with session.get(url, timeout=timeout, allow_redirects=True, ssl=False) as get_response:
                        if get_response.status < 400:
                            return channel, True
                return channel, False
        except asyncio.TimeoutError:
            return channel, False
        except Exception:
            return channel, False


async def check_channels_validity(channels: List[Channel]) -> List[Channel]:
    """
    异步批量检查频道有效性
    
    Args:
        channels: 频道列表
        
    Returns:
        有效的频道列表
    """
    semaphore = asyncio.Semaphore(VALIDITY_CHECK_CONCURRENCY)
    valid_channels = []
    total = len(channels)
    
    connector = aiohttp.TCPConnector(limit=VALIDITY_CHECK_CONCURRENCY, ssl=False)
    
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = []
        for channel in channels:
            task = asyncio.create_task(check_single_channel(session, channel, semaphore))
            tasks.append(task)
        
        completed = 0
        for coro in asyncio.as_completed(tasks):
            channel, is_valid = await coro
            if is_valid:
                valid_channels.append(channel)
            completed += 1
            if completed % 1000 == 0 or completed == total:
                logger.info(f"  进度: {completed}/{total} ({completed*100//total}%)")
    
    return valid_channels


def group_channels_by_category(channels):
    """
    按分类对频道进行分组
    
    Args:
        channels: 频道列表
        
    Returns:
        按分类分组的频道字典
    """
    groups = {}
    
    for channel in channels:
        group = channel.group_title or "未分类"
        if group not in groups:
            groups[group] = []
        groups[group].append(channel)
    
    return groups


def generate_m3u_output(channels, output_path):
    """
    生成 M3U 格式输出文件
    
    Args:
        channels: 频道列表
        output_path: 输出文件路径
    """
    lines = ['#EXTM3U\n']
    
    for channel in channels:
        extinf_parts = []
        extinf_parts.append(f'tvg-id="{channel.tvg_id}"')
        extinf_parts.append(f'tvg-logo="{channel.tvg_logo}"')
        extinf_parts.append(f'group-title="{channel.group_title}"')
        
        for key, value in channel.extra_attrs.items():
            if key not in ['tvg-id', 'tvg-logo', 'group-title', 'http-referrer', 'http-user-agent']:
                extinf_parts.append(f'{key}="{value}"')
        
        extinf_line = f'#EXTINF:-1 {" ".join(extinf_parts)},{channel.name}\n'
        lines.append(extinf_line)
        
        if 'http-referrer' in channel.extra_attrs:
            lines.append(f'#EXTVLCOPT:http-referrer={channel.extra_attrs["http-referrer"]}\n')
        if 'http-user-agent' in channel.extra_attrs:
            lines.append(f'#EXTVLCOPT:http-user-agent={channel.extra_attrs["http-user-agent"]}\n')
        
        lines.append(f'{channel.url}\n')
    
    with open(output_path, 'w', encoding='utf-8') as f:
        f.writelines(lines)
    
    logger.info(f"已生成 M3U 文件: {output_path}")


def generate_txt_output(channels, output_path):
    """
    生成 TXT 格式输出文件
    
    Args:
        channels: 频道列表
        output_path: 输出文件路径
    """
    groups = group_channels_by_category(channels)
    lines = []
    
    sorted_groups = sorted(groups.items(), key=lambda x: x[0])
    
    for group_name, group_channels in sorted_groups:
        lines.append(f'{group_name},#genre#\n')
        
        sorted_channels = sorted(group_channels, key=lambda x: x.name)
        for channel in sorted_channels:
            lines.append(f'{channel.name},{channel.url}\n')
        
        lines.append('\n')
    
    with open(output_path, 'w', encoding='utf-8') as f:
        f.writelines(lines)
    
    logger.info(f"已生成 TXT 文件: {output_path}")


def get_local_input_files(directory):
    """
    获取本地 input 文件
    
    Args:
        directory: 目录路径
        
    Returns:
        (input.m3u 路径, input.txt 路径)
    """
    input_m3u = os.path.join(directory, INPUT_M3U_FILE)
    input_txt = os.path.join(directory, INPUT_TXT_FILE)
    
    return input_m3u, input_txt


def parse_alias_file(file_path):
    """
    解析别名文件
    
    Args:
        file_path: 别名文件路径
        
    Returns:
        别名字典 {主名: set(别名列表)} 和 正则表达式列表 [(主名, 正则表达式)]
    """
    alias_dict = {}
    regex_list = []
    
    if not os.path.exists(file_path):
        logger.warning(f"别名文件不存在: {file_path}")
        return alias_dict, regex_list
    
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except UnicodeDecodeError:
        try:
            with open(file_path, 'r', encoding='gbk') as f:
                lines = f.readlines()
        except Exception as e:
            logger.error(f"无法读取别名文件 {file_path}: {e}")
            return alias_dict, regex_list
    
    for line in lines:
        line = line.strip()
        
        if not line or line.startswith('#'):
            continue
        
        main_name = None
        aliases = set()
        parts = []
        current_part = ""
        in_regex = False
        regex_buffer = ""
        
        for i, char in enumerate(line):
            if char == ',' and not in_regex:
                parts.append(current_part)
                current_part = ""
            else:
                current_part += char
                if current_part.startswith('re:'):
                    in_regex = True
                    regex_buffer = current_part[3:]
                    try:
                        with warnings.catch_warnings():
                            warnings.simplefilter("ignore")
                            re.compile(regex_buffer)
                        in_regex = False
                    except re.error:
                        pass
        
        if current_part:
            parts.append(current_part)
        
        if len(parts) < 2:
            continue
        
        main_name = parts[0].strip()
        
        for alias in parts[1:]:
            alias = alias.strip()
            if not alias:
                continue
            
            if alias.startswith('re:'):
                regex_pattern = alias[3:]
                try:
                    flags = 0 if '(?i)' in regex_pattern else re.IGNORECASE
                    compiled = re.compile(regex_pattern, flags)
                    regex_list.append((main_name, compiled))
                except re.error as e:
                    logger.warning(f"正则表达式错误: {regex_pattern} - {e}")
            else:
                aliases.add(alias.lower())
        
        if main_name not in alias_dict:
            alias_dict[main_name] = set()
        alias_dict[main_name].add(main_name.lower())
        alias_dict[main_name].update(aliases)
    
    logger.info(f"从 {file_path} 解析出 {len(alias_dict)} 个主名，{len(regex_list)} 个正则表达式")
    return alias_dict, regex_list


def parse_demo_file(file_path):
    """
    解析 demo.txt 分类文件
    
    Args:
        file_path: demo.txt 文件路径
        
    Returns:
        分类字典 {分类名: set(频道名列表)}
    """
    categories = {}
    current_category = "未分类"
    
    if not os.path.exists(file_path):
        logger.warning(f"分类文件不存在: {file_path}")
        return categories
    
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except UnicodeDecodeError:
        try:
            with open(file_path, 'r', encoding='gbk') as f:
                lines = f.readlines()
        except Exception as e:
            logger.error(f"无法读取分类文件 {file_path}: {e}")
            return categories
    
    for line in lines:
        line = line.strip()
        
        if not line:
            continue
        
        if '#genre#' in line:
            genre_match = re.match(r'^(.+?),#genre#', line)
            if genre_match:
                current_category = genre_match.group(1).strip()
                if current_category not in categories:
                    categories[current_category] = set()
        elif not line.startswith('#'):
            if current_category not in categories:
                categories[current_category] = set()
            categories[current_category].add(line.lower())
    
    logger.info(f"从 {file_path} 解析出 {len(categories)} 个分类")
    return categories


def match_channel_name(channel_name, alias_dict, regex_list):
    """
    匹配频道名称到主名
    
    Args:
        channel_name: 频道名称
        alias_dict: 别名字典
        regex_list: 正则表达式列表
        
    Returns:
        匹配到的主名，如果未匹配则返回 None
    """
    channel_name_lower = channel_name.lower()
    channel_name_stripped = channel_name.strip()
    
    for main_name, aliases in alias_dict.items():
        if channel_name_lower in aliases:
            return main_name
    
    for main_name, compiled_regex in regex_list:
        if compiled_regex.search(channel_name_stripped):
            return main_name
    
    return None


def generate_alias_demo_report(channels, alias_dict, regex_list, demo_categories, output_path):
    """
    生成别名分类报告（简洁汇总版）
    
    Args:
        channels: 频道列表
        alias_dict: 别名字典
        regex_list: 正则表达式列表
        demo_categories: demo.txt 分类字典
        output_path: 输出文件路径
        
    Returns:
        dict: 包含 unknown_categories 和 alias_suggestions 的字典
    """
    known_channels = {}
    unknown_channels = {}
    
    for channel in channels:
        main_name = match_channel_name(channel.name, alias_dict, regex_list)
        
        if main_name:
            if main_name not in known_channels:
                known_channels[main_name] = set()
            known_channels[main_name].add(channel.name)
        else:
            group = channel.group_title or "未分类"
            if group not in unknown_channels:
                unknown_channels[group] = set()
            unknown_channels[group].add(channel.name)
    
    demo_all_names = set()
    for names in demo_categories.values():
        demo_all_names.update(n.lower() for n in names)
    
    known_categories = set()
    unknown_categories = set()
    
    for main_name in known_channels:
        if main_name.lower() in demo_all_names:
            for cat, names in demo_categories.items():
                if main_name.lower() in [n.lower() for n in names]:
                    known_categories.add(cat)
                    break
        else:
            unknown_categories.add(f"[待分类] {main_name}")
    
    for group in unknown_channels:
        if group not in demo_categories:
            unknown_categories.add(group)
    
    suggested_aliases = {}
    for group, names in unknown_channels.items():
        for name in names:
            base_name = re.sub(r'[-_\s]*(HD|4K|8K|高清|超清|标清|直播|卫视|频道|电视台).*$', '', name, flags=re.IGNORECASE)
            base_name = re.sub(r'\[.*?\]', '', base_name).strip()
            base_name = re.sub(r'「.*?」', '', base_name).strip()
            
            if base_name not in suggested_aliases:
                suggested_aliases[base_name] = {'count': 0, 'samples': set(), 'category': group}
            suggested_aliases[base_name]['count'] += 1
            if len(suggested_aliases[base_name]['samples']) < 3:
                suggested_aliases[base_name]['samples'].add(name)
    
    suggested_aliases = {k: v for k, v in suggested_aliases.items() if v['count'] >= 3}
    
    timestamp = datetime.now()
    timestamp_beijing = timestamp + timedelta(hours=8)
    timestamp_str = timestamp_beijing.strftime('%Y-%m-%d %H:%M:%S')
    
    lines = []
    lines.append(f"# 别名分类报告 - 生成时间: {timestamp_str}\n")
    lines.append(f"# 总频道数: {len(channels)}\n")
    lines.append(f"# 已知别名频道: {sum(len(v) for v in known_channels.values())} 个（去重后 {len(known_channels)} 个主名）\n")
    lines.append(f"# 未知别名频道: {sum(len(v) for v in unknown_channels.values())} 个（去重后 {len(set().union(*unknown_channels.values()))} 个名称）\n")
    lines.append("\n")
    
    lines.append("=" * 80 + "\n")
    lines.append("一、分类名汇总\n")
    lines.append("=" * 80 + "\n\n")
    
    lines.append("【已知分类】（demo.txt 中已定义且匹配到的分类）\n")
    if known_categories:
        for cat in sorted(known_categories):
            lines.append(f"  ✓ {cat}\n")
    else:
        lines.append("  （无）\n")
    lines.append("\n")
    
    lines.append("【未知分类】（需要添加到 demo.txt 的分类）\n")
    if unknown_categories:
        for cat in sorted(unknown_categories):
            lines.append(f"  ✗ {cat}\n")
    else:
        lines.append("  （无）\n")
    lines.append("\n")
    
    lines.append("=" * 80 + "\n")
    lines.append("二、频道名汇总\n")
    lines.append("=" * 80 + "\n\n")
    
    lines.append("【已知频道】（alias.txt 中已定义的别名，按分类统计）\n")
    categorized_known = {}
    uncategorized_known = []
    
    for main_name, name_set in known_channels.items():
        found = False
        for cat, names in demo_categories.items():
            if main_name.lower() in [n.lower() for n in names]:
                if cat not in categorized_known:
                    categorized_known[cat] = []
                categorized_known[cat].append((main_name, len(name_set)))
                found = True
                break
        if not found:
            uncategorized_known.append((main_name, len(name_set)))
    
    for cat in sorted(categorized_known.keys()):
        lines.append(f"\n  📁 {cat}\n")
        for main_name, count in sorted(categorized_known[cat]):
            lines.append(f"      {main_name} ({count} 个变体)\n")
    
    if uncategorized_known:
        lines.append(f"\n  📁 [待分类]\n")
        for main_name, count in sorted(uncategorized_known):
            lines.append(f"      {main_name} ({count} 个变体)\n")
    lines.append("\n")
    
    lines.append("【未知频道】（需要添加到 alias.txt 的别名）\n")
    
    lines.append("\n  ── 建议别名组（出现 3 次以上的相似名称）──\n")
    if suggested_aliases:
        sorted_suggestions = sorted(suggested_aliases.items(), key=lambda x: -x[1]['count'])
        for base_name, info in sorted_suggestions[:50]:
            samples = ', '.join(sorted(info['samples']))
            lines.append(f"    🔸 {base_name}\n")
            lines.append(f"        出现次数: {info['count']}, 原始分类: {info['category']}\n")
            lines.append(f"        示例: {samples}\n")
    else:
        lines.append("    （无高频未知频道）\n")
    
    lines.append("\n  ── 按原始分类统计 ──\n")
    for group in sorted(unknown_channels.keys()):
        count = len(unknown_channels[group])
        lines.append(f"    📂 {group}: {count} 个频道\n")
    
    lines.append("\n")
    lines.append("=" * 80 + "\n")
    lines.append("三、迭代建议\n")
    lines.append("=" * 80 + "\n\n")
    
    lines.append("【需要添加到 demo.txt 的分类】\n")
    for cat in sorted(unknown_categories):
        lines.append(f"  {cat},#genre#\n")
    
    lines.append("\n【需要添加到 alias.txt 的别名】\n")
    sorted_suggestions = sorted(suggested_aliases.items(), key=lambda x: -x[1]['count'])
    for base_name, info in sorted_suggestions:
        samples = ','.join(sorted(info['samples'])[:3])
        lines.append(f"  {base_name},{samples}\n")
    
    with open(output_path, 'w', encoding='utf-8') as f:
        f.writelines(lines)
    
    logger.info(f"已生成别名分类报告: {output_path}")
    logger.info(f"  已知别名频道: {sum(len(v) for v in known_channels.values())} 个")
    logger.info(f"  未知别名频道: {sum(len(v) for v in unknown_channels.values())} 个")
    
    return {
        'unknown_categories': sorted(unknown_categories),
        'alias_suggestions': [(name, info['count'], ','.join(sorted(info['samples'])[:3])) 
                             for name, info in sorted(suggested_aliases.items(), key=lambda x: -x[1]['count'])]
    }


def generate_readme_report(channels, alias_dict, regex_list, demo_categories, failed_count, github_count, extra_data, output_path):
    """
    生成 README.md 报告文件
    
    Args:
        channels: 频道列表
        alias_dict: 别名字典
        regex_list: 正则表达式列表
        demo_categories: 分类字典
        failed_count: 失败源数量
        github_count: GitHub 来源数量
        extra_data: 额外的报告数据（包含 unknown_categories 和 alias_suggestions）
        output_path: 输出路径
    """
    total_channels = len(channels)
    known_alias_count = sum(len(v) for v in alias_dict.values()) if alias_dict else 0
    
    category_count = {}
    for channel in channels:
        group = channel.group_title or "未分类"
        if group not in category_count:
            category_count[group] = 0
        category_count[group] += 1
    
    top_categories = sorted(category_count.items(), key=lambda x: -x[1])[:15]
    
    unknown_categories = extra_data.get('unknown_categories', [])
    alias_suggestions = extra_data.get('alias_suggestions', [])
    
    timestamp = datetime.now()
    timestamp_beijing = timestamp + timedelta(hours=8)
    timestamp_str = timestamp_beijing.strftime('%Y-%m-%d %H:%M:%S')
    
    lines = [
        "# IPTV 播放列表\n",
        "\n",
        f"> 最后更新：{timestamp_str} (北京时间)\n",
        "\n",
        "## 📊 统计概览\n",
        "\n",
        f"| 项目 | 数量 |\n",
        f"|------|------|\n",
        f"| 总频道数 | **{total_channels}** |\n",
        f"| 已知别名 | {known_alias_count} |\n",
        f"| 分类数量 | {len(category_count)} |\n",
        f"| GitHub 来源 | {github_count} |\n",
        f"| 失败源数 | {failed_count} |\n",
        "\n",
        "## 📺 频道分类 TOP 15\n",
        "\n",
    ]
    
    for i, (cat, count) in enumerate(top_categories, 1):
        lines.append(f"{i}. **{cat}** - {count} 个频道\n")
    
    lines.extend([
        "\n",
        "## 📥 下载地址\n",
        "\n",
        "- [output.m3u](output.m3u) - M3U 格式\n",
        "- [output.txt](output.txt) - TXT 格式\n",
        "\n",
        "## 📝 报告文件\n",
        "\n",
        "- [new-aliasdemo.txt](new-aliasdemo.txt) - 详细分类报告\n",
        "- [failed_sources.log](failed_sources.log) - 失败源日志\n",
        "- [github_sources.log](github_sources.log) - GitHub 来源日志\n",
        "\n",
        "## ⚠️ 需要人工处理的分类 (TOP 30)\n",
        "\n",
        "以下分类未在 demo.txt 中定义，建议添加：\n",
        "\n",
    ])
    
    for i, cat in enumerate(unknown_categories[:30], 1):
        lines.append(f"{i}. `{cat}`\n")
    
    if unknown_categories:
        lines.append(f"\n> 共 {len(unknown_categories)} 个未知分类，完整列表见 [new-aliasdemo.txt](new-aliasdemo.txt)\n")
    
    lines.extend([
        "\n",
        "## 📝 待添加到 alias.txt 的别名建议\n",
        "\n",
        "以下频道名称建议添加别名映射：\n",
        "\n",
    ])
    
    for name, count, samples in alias_suggestions[:20]:
        if name:
            lines.append(f"- **{name}** (出现 {count} 次): {samples}\n")
        else:
            lines.append(f"- 无主名 (出现 {count} 次): {samples}\n")
    
    if len(alias_suggestions) > 20:
        lines.append(f"\n> 共 {len(alias_suggestions)} 条建议，完整列表见 [new-aliasdemo.txt](new-aliasdemo.txt)\n")
    
    with open(output_path, 'w', encoding='utf-8') as f:
        f.writelines(lines)
    
    logger.info(f"已生成 README 报告: {output_path}")


def run_full_mode():
    """
    完整运行模式
    执行完整的 IPTV 播放列表聚合处理流程
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    
    validate_token(GITHUB_TOKEN)
    
    logger.info("\n📂 第一步：用 output 替换 input...")
    replace_input_with_output()
    
    all_channels = []
    
    logger.info("\n📂 第二步：加载本地 input 文件...")
    input_m3u, input_txt = get_local_input_files(script_dir)
    
    if os.path.exists(input_m3u):
        channels = parse_m3u_file(input_m3u)
        all_channels.append(channels)
        logger.info(f"  ✅ input.m3u: {len(channels)} 个频道")
    
    if os.path.exists(input_txt):
        channels = parse_txt_file(input_txt)
        all_channels.append(channels)
        logger.info(f"  ✅ input.txt: {len(channels)} 个频道")
    
    logger.info("\n🔍 第三步：从 GitHub 搜索 M3U 文件...")
    seen_urls = set()
    m3u_channels = []
    
    for strategy in M3U_SEARCH_STRATEGIES:
        logger.info(f"\n  🔎 搜索策略: {strategy['name']}")
        items, format_type = search_github_files(strategy)
        
        for item in items:
            raw_url = item["html_url"].replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")
            
            if raw_url in seen_urls:
                continue
            seen_urls.add(raw_url)
            
            content, url = fetch_github_file(item)
            if content:
                channels = parse_m3u_content(content, url)
                m3u_channels.extend(channels)
    
    all_channels.append(m3u_channels)
    logger.info(f"\n  📊 M3U 搜索结果: {len(m3u_channels)} 个频道")
    
    logger.info("\n🔍 第四步：从 GitHub 搜索 TXT 文件...")
    txt_channels = []
    
    for strategy in TXT_SEARCH_STRATEGIES:
        logger.info(f"\n  🔎 搜索策略: {strategy['name']}")
        items, format_type = search_github_files(strategy)
        
        for item in items:
            raw_url = item["html_url"].replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")
            
            if raw_url in seen_urls:
                continue
            seen_urls.add(raw_url)
            
            content, url = fetch_github_file(item)
            if content:
                channels = parse_txt_content(content, url)
                txt_channels.extend(channels)
    
    all_channels.append(txt_channels)
    logger.info(f"\n  📊 TXT 搜索结果: {len(txt_channels)} 个频道")
    
    logger.info("\n🔄 第五步：合并去重所有频道...")
    merged_channels = merge_and_deduplicate(all_channels)
    logger.info(f"  合并后共 {len(merged_channels)} 个频道")

    logger.info("\n🔍 第六步：黑名单过滤与有效性筛选...")
    blacklist_urls, is_blist_valid, _ = load_blacklist()
    
    if is_blist_valid and blacklist_urls:
        logger.info(f"  📋 黑名单有效期内，使用黑名单预过滤...")
        prefiltered_count = len(merged_channels)
        merged_channels = filter_channels_by_blacklist(merged_channels, blacklist_urls)
        logger.info(f"  📋 预过滤后剩余: {len(merged_channels)} 个频道 (过滤了 {prefiltered_count - len(merged_channels)} 个)")
    
    logger.info(f"  筛选参数: 超时={VALIDITY_CHECK_TIMEOUT}秒, 并发={VALIDITY_CHECK_CONCURRENCY}")
    valid_channels = asyncio.run(check_channels_validity(merged_channels))
    invalid_count = len(merged_channels) - len(valid_channels)
    logger.info(f"  ✅ 有效频道: {len(valid_channels)} 个, ❌ 无效频道: {invalid_count} 个")

    invalid_channels = [ch for ch in merged_channels if ch not in valid_channels]
    if invalid_channels:
        blacklist_urls = add_to_blacklist(blacklist_urls, invalid_channels)
        save_blacklist(blacklist_urls)
        logger.info(f"  💾 已更新黑名单，共 {len(blacklist_urls)} 个无效URL")

    if invalid_count > 0:
        logger.info(f"  💾 记录无效源到 failed_sources.log...")
        for channel in invalid_channels:
            log_failed(channel.url, "快速有效性检测失败")

    logger.info("\n💾 第七步：生成输出文件...")
    output_m3u = os.path.join(script_dir, OUTPUT_M3U_FILE)
    output_txt = os.path.join(script_dir, OUTPUT_TXT_FILE)
    
    generate_m3u_output(valid_channels, output_m3u)
    generate_txt_output(valid_channels, output_txt)

    logger.info("\n📊 第八步：生成别名分类报告...")
    alias_file = os.path.join(script_dir, ALIAS_FILE)
    demo_file = os.path.join(script_dir, DEMO_FILE)
    alias_demo_file = os.path.join(script_dir, ALIAS_DEMO_FILE)
    
    alias_dict, regex_list = parse_alias_file(alias_file)
    demo_categories = parse_demo_file(demo_file)
    
    extra_data = generate_alias_demo_report(valid_channels, alias_dict, regex_list, demo_categories, alias_demo_file)

    readme_file = os.path.join(script_dir, README_FILE)
    failed_count = 0
    github_count = 0
    if os.path.exists(os.path.join(script_dir, FAILED_LOG_FILE)):
        with open(os.path.join(script_dir, FAILED_LOG_FILE), 'r', encoding='utf-8') as f:
            failed_count = len(f.readlines())
    if os.path.exists(os.path.join(script_dir, GITHUB_LOG_FILE)):
        with open(os.path.join(script_dir, GITHUB_LOG_FILE), 'r', encoding='utf-8') as f:
            github_count = len(f.readlines())
    generate_readme_report(valid_channels, alias_dict, regex_list, demo_categories, failed_count, github_count, extra_data, readme_file)

    logger.info(f"\n✅ 处理完成！共 {len(valid_channels)} 个有效频道（筛选前: {len(merged_channels)} 个）")
    logger.info(f"📄 失败日志: {os.path.join(script_dir, FAILED_LOG_FILE)}")
    logger.info(f"📄 GitHub 日志: {os.path.join(script_dir, GITHUB_LOG_FILE)}")
    logger.info(f"📄 别名分类报告: {alias_demo_file}")
    logger.info(f"📄 README 报告: {readme_file}")


def run_report_mode():
    """
    报告模式
    仅从现有 output 文件生成别名分类报告
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    
    output_m3u = os.path.join(script_dir, OUTPUT_M3U_FILE)
    output_txt = os.path.join(script_dir, OUTPUT_TXT_FILE)
    
    all_channels = []
    
    logger.info("\n📂 加载现有 output 文件...")
    
    if os.path.exists(output_m3u):
        channels = parse_m3u_file(output_m3u)
        all_channels.append(channels)
        logger.info(f"  ✅ output.m3u: {len(channels)} 个频道")
    else:
        logger.warning(f"  ⚠️ output.m3u 不存在")
    
    if os.path.exists(output_txt):
        channels = parse_txt_file(output_txt)
        all_channels.append(channels)
        logger.info(f"  ✅ output.txt: {len(channels)} 个频道")
    else:
        logger.warning(f"  ⚠️ output.txt 不存在")
    
    if not all_channels:
        logger.error("❌ 没有找到任何 output 文件，请先运行完整模式")
        return
    
    logger.info("\n🔄 合并频道数据...")
    merged_channels = merge_and_deduplicate(all_channels)
    
    logger.info("\n📊 生成别名分类报告...")
    alias_file = os.path.join(script_dir, ALIAS_FILE)
    demo_file = os.path.join(script_dir, DEMO_FILE)
    alias_demo_file = os.path.join(script_dir, ALIAS_DEMO_FILE)
    
    alias_dict, regex_list = parse_alias_file(alias_file)
    demo_categories = parse_demo_file(demo_file)
    
    extra_data = generate_alias_demo_report(merged_channels, alias_dict, regex_list, demo_categories, alias_demo_file)
    
    readme_file = os.path.join(script_dir, README_FILE)
    failed_count = 0
    github_count = 0
    if os.path.exists(os.path.join(script_dir, FAILED_LOG_FILE)):
        with open(os.path.join(script_dir, FAILED_LOG_FILE), 'r', encoding='utf-8') as f:
            failed_count = len(f.readlines())
    if os.path.exists(os.path.join(script_dir, GITHUB_LOG_FILE)):
        with open(os.path.join(script_dir, GITHUB_LOG_FILE), 'r', encoding='utf-8') as f:
            github_count = len(f.readlines())
    generate_readme_report(merged_channels, alias_dict, regex_list, demo_categories, failed_count, github_count, extra_data, readme_file)
    
    logger.info(f"\n✅ 报告生成完成！共 {len(merged_channels)} 个频道")
    logger.info(f"📄 别名分类报告: {alias_demo_file}")
    logger.info(f"📄 README 报告: {readme_file}")


def run_validity_check_mode():
    """
    有效性筛选模式
    仅从现有 output 文件进行有效性筛选测试
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))

    output_m3u = os.path.join(script_dir, OUTPUT_M3U_FILE)
    output_txt = os.path.join(script_dir, OUTPUT_TXT_FILE)

    logger.info("\n📂 加载现有 output 文件...")

    if os.path.exists(output_m3u):
        channels = parse_m3u_file(output_m3u)
        logger.info(f"  ✅ output.m3u: {len(channels)} 个频道")
    elif os.path.exists(output_txt):
        channels = parse_txt_file(output_txt)
        logger.info(f"  ✅ output.txt: {len(channels)} 个频道")
    else:
        logger.error("❌ 没有找到任何 output 文件，请先运行完整模式")
        return

    logger.info("\n🔍 开始有效性筛选测试...")
    logger.info(f"  筛选参数: 超时={VALIDITY_CHECK_TIMEOUT}秒, 并发={VALIDITY_CHECK_CONCURRENCY}")

    start_time = time.time()
    valid_channels = asyncio.run(check_channels_validity(channels))
    elapsed_time = time.time() - start_time

    invalid_count = len(channels) - len(valid_channels)
    valid_rate = len(valid_channels) / len(channels) * 100 if channels else 0

    logger.info(f"\n📊 有效性筛选结果:")
    logger.info(f"  ✅ 有效频道: {len(valid_channels)} 个 ({valid_rate:.1f}%)")
    logger.info(f"  ❌ 无效频道: {invalid_count} 个 ({100-valid_rate:.1f}%)")
    logger.info(f"  ⏱️  耗时: {elapsed_time:.1f} 秒")
    logger.info(f"  📈 处理速度: {len(channels)/elapsed_time:.1f} 个/秒")

    logger.info(f"\n💾 保存有效频道到 output 文件...")
    output_m3u_valid = os.path.join(script_dir, 'output_valid.m3u')
    output_txt_valid = os.path.join(script_dir, 'output_valid.txt')

    generate_m3u_output(valid_channels, output_m3u_valid)
    generate_txt_output(valid_channels, output_txt_valid)

    logger.info(f"\n✅ 有效性筛选完成！")
    logger.info(f"  📄 有效源文件: {output_m3u_valid}, {output_txt_valid}")


def main():
    """
    主函数
    支持三种运行模式：
    - 无参数：完整运行模式
    - --report 或 -r：仅生成报告模式
    - --validity 或 -v：仅有效性筛选模式
    """
    parser = argparse.ArgumentParser(
        description='IPTV 播放列表聚合器',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
使用示例:
  python main.py              # 完整运行模式（搜索、合并、去重、生成报告）
  python main.py --report     # 仅生成报告模式（从现有 output 文件生成报告）
  python main.py -r           # 同上
  python main.py --validity   # 仅有效性筛选模式（测试有效性检测功能）
  python main.py -v           # 同上
        '''
    )
    parser.add_argument(
        '-r', '--report',
        action='store_true',
        help='仅生成别名分类报告（不从 GitHub 搜索新数据）'
    )
    parser.add_argument(
        '-v', '--validity',
        action='store_true',
        help='仅进行有效性筛选（从现有 output 文件测试有效性检测）'
    )
    
    args = parser.parse_args()
    
    if args.report:
        logger.info("🎯 运行模式：仅生成报告")
        run_report_mode()
    elif args.validity:
        logger.info("🎯 运行模式：仅有效性筛选")
        run_validity_check_mode()
    else:
        logger.info("🎯 运行模式：完整运行")
        run_full_mode()


if __name__ == '__main__':
    main()
