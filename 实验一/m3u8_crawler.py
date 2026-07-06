# -*- coding: utf-8 -*-
"""
m3u8 流媒体视频爬取实验脚本。

本脚本围绕课堂 PDF 的要求编写，重点展示三件事：
1. 用 BeautifulSoup 解析网页结构，再用 re 正则表达式匹配 m3u8 地址。
2. 解析 m3u8 索引文件，按顺序下载其中记录的所有 ts 视频分片。
3. 如果 m3u8 使用 AES-128 加密，则读取 #EXT-X-KEY，下载密钥并解密分片。

示例：
    python 实验一/m3u8_crawler.py --page-url "https://www.acfun.cn/v/ac33856487" --output downloads/acfun_demo.mp4
    python 实验一/m3u8_crawler.py --m3u8-url "https://example.com/path/index.m3u8" --output downloads/video.mp4

注意：
    本脚本只用于课程实验和合法学习。请勿下载、传播没有授权的视频内容。
"""

from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import html
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote, urljoin

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError as exc:  # pragma: no cover - 这是给使用者看的友好报错。
    raise SystemExit(
        "缺少基础依赖。请先运行：pip install -r 实验一/requirements.txt"
    ) from exc


# 模拟真实浏览器请求，降低被网站直接拒绝的概率。
# 有些 m3u8 或 ts 服务器会检查 User-Agent / Referer，不带请求头可能返回 403。
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


# 正则要求：匹配 http/https 开头、后缀为 .m3u8 的地址。
# 末尾允许带查询参数，例如 index.m3u8?sign=xxx。
M3U8_PATTERN = re.compile(
    r"https?://[^\s\"'<>\\]+?\.m3u8(?:\?[^\s\"'<>\\]*)?",
    re.IGNORECASE,
)


# HLS 的属性行形如：
#   #EXT-X-KEY:METHOD=AES-128,URI="key.key",IV=0x1234
# 这里用正则把 METHOD / URI / IV 等键值拆出来。
ATTRIBUTE_PATTERN = re.compile(r"([A-Z0-9-]+)=(\"[^\"]*\"|[^,]*)")


@dataclasses.dataclass(frozen=True)
class Variant:
    """主播放列表中的清晰度选项。"""

    url: str
    bandwidth: int = 0
    resolution: str = ""


@dataclasses.dataclass(frozen=True)
class KeyDirective:
    """记录当前 ts 分片应使用的解密方式。"""

    method: str
    uri: str | None = None
    iv_hex: str | None = None


@dataclasses.dataclass(frozen=True)
class Segment:
    """单个 ts 分片的信息。"""

    index: int
    sequence: int
    url: str
    key: KeyDirective | None = None


@dataclasses.dataclass(frozen=True)
class Playlist:
    """解析后的 m3u8 内容。"""

    url: str
    text: str
    variants: list[Variant]
    segments: list[Segment]


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""

    parser = argparse.ArgumentParser(
        description="使用 BeautifulSoup + re 提取 m3u8，并下载普通或 AES-128 加密的 HLS 视频。"
    )
    parser.add_argument(
        "--page-url",
        default="https://www.acfun.cn/v/ac33856487",
        help="视频网页地址。默认使用 PDF 中提到的 AcFun 示例页面。",
    )
    parser.add_argument(
        "--m3u8-url",
        help="已知的 m3u8 地址。传入后会跳过网页提取步骤，直接下载该 m3u8。",
    )
    parser.add_argument(
        "--output",
        default="downloads/acfun_demo.mp4",
        help="最终输出文件，建议使用 .mp4 或 .ts 后缀。",
    )
    parser.add_argument(
        "--work-dir",
        default="downloads/work",
        help="保存 ts 分片和中间合并文件的工作目录。",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=8,
        help="并发下载 ts 分片的线程数。网络较慢时可以调小。",
    )
    parser.add_argument(
        "--variant",
        choices=("best", "first"),
        default="best",
        help="主 m3u8 中有多个清晰度时，best 选最高带宽，first 选第一个。",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=20,
        help="单次 HTTP 请求超时时间，单位为秒。",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="下载成功后删除 ts 分片和中间文件。默认保留，方便检查实验过程。",
    )
    return parser.parse_args()


def request_text(url: str, headers: dict[str, str], timeout: int) -> str:
    """请求文本资源，比如网页 HTML 或 m3u8 文件。"""

    response = requests.get(url, headers=headers, timeout=timeout)
    response.raise_for_status()

    # m3u8 通常是 UTF-8；网页可能没有正确声明编码，所以优先使用 requests 猜测值。
    if not response.encoding:
        response.encoding = response.apparent_encoding or "utf-8"
    return response.text


def request_bytes(
    url: str,
    headers: dict[str, str],
    timeout: int,
    retries: int = 3,
) -> bytes:
    """请求二进制资源，比如 ts 分片或 AES 密钥。"""

    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(url, headers=headers, timeout=timeout)
            response.raise_for_status()
            return response.content
        except requests.RequestException as exc:
            last_error = exc
            # 视频分片很多，偶发失败很常见；稍等后重试可以提高成功率。
            if attempt < retries:
                time.sleep(0.8 * attempt)
    raise RuntimeError(f"请求失败，已重试 {retries} 次：{url}") from last_error


def normalized_text_versions(text: str) -> set[str]:
    """
    生成多份“清洗过”的文本，方便正则匹配。

    很多视频网站会把 URL 放在 JSON 或 JS 变量中，常见写法包括：
        https:\\/\\/example.com\\/index.m3u8
        https:\\u002F\\u002Fexample.com\\u002Findex.m3u8
        https://example.com/index.m3u8&amp;token=xxx
    正则直接扫原文可能匹配不到，所以先把 HTML 实体和转义斜杠还原。
    """

    versions = {text}
    html_unescaped = html.unescape(text)
    versions.add(html_unescaped)
    versions.add(html_unescaped.replace("\\/", "/"))
    versions.add(html_unescaped.replace("\\u002F", "/").replace("\\u003A", ":"))
    versions.add(unquote(html_unescaped))
    return versions


def collect_structured_chunks(soup: BeautifulSoup, raw_html: str) -> list[str]:
    """
    使用 BeautifulSoup 从网页中提取“结构化文本块”。

    这里不是简单地对整页字符串蛮力搜索，而是先按 HTML 结构拆分：
    1. script 标签：视频网站常把播放信息塞进 JS 变量。
    2. src / href / data-* 等属性：视频、source、a 标签可能直接携带媒体地址。
    3. 原始 HTML：作为兜底，避免遗漏被特殊嵌套的内容。
    """

    chunks: list[str] = [raw_html]

    for script in soup.find_all("script"):
        chunks.append(script.get_text(" ", strip=False))

    for tag in soup.find_all(True):
        for value in tag.attrs.values():
            if isinstance(value, (list, tuple)):
                chunks.append(" ".join(str(item) for item in value))
            else:
                chunks.append(str(value))

    return chunks


def extract_m3u8_urls_from_page(
    page_url: str,
    headers: dict[str, str],
    timeout: int,
) -> list[str]:
    """
    从视频网页中提取 m3u8 地址。

    这一步同时满足实验要求中的 BeautifulSoup 和 re：
    - BeautifulSoup 负责把 HTML 解析成可遍历的结构。
    - re 负责在结构化文本块里匹配 .m3u8 地址。
    """

    raw_html = request_text(page_url, headers=headers, timeout=timeout)
    soup = BeautifulSoup(raw_html, "html.parser")
    chunks = collect_structured_chunks(soup, raw_html)

    found: list[str] = []
    seen: set[str] = set()

    for chunk in chunks:
        for text in normalized_text_versions(chunk):
            for match in M3U8_PATTERN.findall(text):
                # 如果匹配结果中混入了 HTML 实体或百分号编码，这里再还原一次。
                clean_url = unquote(html.unescape(match))
                clean_url = urljoin(page_url, clean_url)
                if clean_url not in seen:
                    found.append(clean_url)
                    seen.add(clean_url)

    return found


def parse_attribute_list(value: str) -> dict[str, str]:
    """解析 HLS 属性列表，例如 METHOD=AES-128,URI="key.key"。"""

    attrs: dict[str, str] = {}
    for key, raw_value in ATTRIBUTE_PATTERN.findall(value):
        attrs[key.upper()] = raw_value.strip().strip('"')
    return attrs


def parse_m3u8(m3u8_url: str, text: str) -> Playlist:
    """
    解析 m3u8 文本。

    m3u8 有两类常见形态：
    1. 主播放列表：只列出不同清晰度的子 m3u8，例如 720P、1080P。
    2. 媒体播放列表：直接列出一堆 ts 分片和可选的加密信息。
    """

    variants: list[Variant] = []
    segments: list[Segment] = []
    current_key: KeyDirective | None = None
    pending_variant_attrs: dict[str, str] | None = None
    media_sequence = 0
    segment_sequence = 0

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            # 分片序号会影响加密 m3u8 的默认 IV。
            media_sequence = int(line.split(":", 1)[1])
            segment_sequence = media_sequence
            continue

        if line.startswith("#EXT-X-STREAM-INF:"):
            pending_variant_attrs = parse_attribute_list(line.split(":", 1)[1])
            continue

        if line.startswith("#EXT-X-KEY:"):
            attrs = parse_attribute_list(line.split(":", 1)[1])
            method = attrs.get("METHOD", "NONE").upper()
            if method == "NONE":
                current_key = None
            else:
                key_uri = attrs.get("URI")
                current_key = KeyDirective(
                    method=method,
                    uri=urljoin(m3u8_url, key_uri) if key_uri else None,
                    iv_hex=attrs.get("IV"),
                )
            continue

        if line.startswith("#"):
            # 其他 #EXTINF、#EXT-X-ENDLIST 等标签暂时不需要处理。
            continue

        absolute_url = urljoin(m3u8_url, line)

        if pending_variant_attrs is not None:
            bandwidth = int(pending_variant_attrs.get("BANDWIDTH", "0") or 0)
            resolution = pending_variant_attrs.get("RESOLUTION", "")
            variants.append(
                Variant(url=absolute_url, bandwidth=bandwidth, resolution=resolution)
            )
            pending_variant_attrs = None
            continue

        segments.append(
            Segment(
                index=len(segments),
                sequence=segment_sequence,
                url=absolute_url,
                key=current_key,
            )
        )
        segment_sequence += 1

    return Playlist(url=m3u8_url, text=text, variants=variants, segments=segments)


def choose_variant(variants: list[Variant], policy: str) -> Variant:
    """从主 m3u8 的多个清晰度里选择一个子 m3u8。"""

    if policy == "first":
        return variants[0]
    return max(variants, key=lambda item: item.bandwidth)


def load_media_playlist(
    m3u8_url: str,
    headers: dict[str, str],
    timeout: int,
    variant_policy: str,
) -> Playlist:
    """
    加载最终包含 ts 分片的媒体播放列表。

    如果传入的是主 m3u8，本函数会自动进入选中的子 m3u8，直到拿到真正的 ts 列表。
    """

    current_url = m3u8_url
    for _ in range(5):
        text = request_text(current_url, headers=headers, timeout=timeout)
        playlist = parse_m3u8(current_url, text)
        if playlist.segments:
            return playlist
        if not playlist.variants:
            return playlist

        chosen = choose_variant(playlist.variants, variant_policy)
        print(
            f"检测到主 m3u8，选择子 m3u8：{chosen.resolution or '未知分辨率'} "
            f"bandwidth={chosen.bandwidth} -> {chosen.url}"
        )
        current_url = chosen.url

    raise RuntimeError("m3u8 嵌套层级过深，已停止解析。")


def parse_iv(iv_hex: str | None, sequence: int) -> bytes:
    """
    计算 AES-128-CBC 解密所需的 16 字节 IV。

    HLS 规则：
    - 如果 #EXT-X-KEY 显式给了 IV=0x...，就使用该 IV。
    - 如果没有给 IV，就用当前 ts 分片的媒体序号，转成 16 字节大端整数。
    """

    if iv_hex:
        clean = iv_hex[2:] if iv_hex.lower().startswith("0x") else iv_hex
        return bytes.fromhex(clean.zfill(32))
    return sequence.to_bytes(16, byteorder="big")


def decrypt_aes128(segment_data: bytes, key: bytes, iv: bytes) -> bytes:
    """解密 AES-128-CBC 加密的 ts 分片。"""

    try:
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import unpad
    except ImportError as exc:  # pragma: no cover - 只有遇到加密视频时才会触发。
        raise RuntimeError(
            "检测到 AES-128 加密 m3u8，但缺少 pycryptodome。"
            "请运行：pip install -r 实验一/requirements.txt"
        ) from exc

    cipher = AES.new(key, AES.MODE_CBC, iv)
    decrypted = cipher.decrypt(segment_data)

    # HLS AES-128 分片通常会做 PKCS#7 padding。若某些站点没有标准 padding，
    # unpad 会抛出 ValueError，此时直接返回解密后的原始字节即可。
    try:
        return unpad(decrypted, AES.block_size)
    except ValueError:
        return decrypted


def prefetch_keys(
    segments: Iterable[Segment],
    headers: dict[str, str],
    timeout: int,
) -> dict[str, bytes]:
    """提前下载所有会用到的 AES 密钥，避免每个分片重复请求。"""

    key_cache: dict[str, bytes] = {}
    for segment in segments:
        if not segment.key or segment.key.method == "NONE":
            continue
        if segment.key.method != "AES-128":
            raise RuntimeError(f"暂不支持的加密方式：{segment.key.method}")
        if not segment.key.uri:
            raise RuntimeError("加密 m3u8 中缺少 KEY URI，无法下载密钥。")
        if segment.key.uri not in key_cache:
            print(f"下载 AES 密钥：{segment.key.uri}")
            key_cache[segment.key.uri] = request_bytes(
                segment.key.uri,
                headers=headers,
                timeout=timeout,
            )
    return key_cache


def safe_segment_name(index: int) -> str:
    """生成固定宽度的分片文件名，保证合并时按名称排序也不会乱序。"""

    return f"{index:06d}.ts"


def download_one_segment(
    segment: Segment,
    output_dir: Path,
    headers: dict[str, str],
    timeout: int,
    key_cache: dict[str, bytes],
) -> Path:
    """下载并按需解密一个 ts 分片。"""

    output_path = output_dir / safe_segment_name(segment.index)
    if output_path.exists() and output_path.stat().st_size > 0:
        return output_path

    data = request_bytes(segment.url, headers=headers, timeout=timeout)

    if segment.key and segment.key.method == "AES-128":
        assert segment.key.uri is not None
        key = key_cache[segment.key.uri]
        iv = parse_iv(segment.key.iv_hex, segment.sequence)
        data = decrypt_aes128(data, key=key, iv=iv)

    # 先写 .part 临时文件，写完再重命名。这样中途失败时不会留下半截 ts。
    part_path = output_path.with_suffix(".part")
    part_path.write_bytes(data)
    part_path.replace(output_path)
    return output_path


def download_segments(
    segments: list[Segment],
    output_dir: Path,
    headers: dict[str, str],
    timeout: int,
    max_workers: int,
) -> list[Path]:
    """并发下载全部 ts 分片，并返回按播放顺序排列的本地文件路径。"""

    output_dir.mkdir(parents=True, exist_ok=True)
    key_cache = prefetch_keys(segments, headers=headers, timeout=timeout)

    print(f"开始下载 {len(segments)} 个 ts 分片，线程数：{max_workers}")
    paths: list[Path] = [output_dir / safe_segment_name(i) for i in range(len(segments))]

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(
                download_one_segment,
                segment,
                output_dir,
                headers,
                timeout,
                key_cache,
            ): segment
            for segment in segments
        }

        for done_count, future in enumerate(
            concurrent.futures.as_completed(future_map),
            start=1,
        ):
            segment = future_map[future]
            paths[segment.index] = future.result()
            print(f"\r下载进度：{done_count}/{len(segments)}", end="", flush=True)

    print()
    return paths


def concat_ts_files(segment_paths: list[Path], output_ts: Path) -> None:
    """把所有 ts 分片按顺序合并为一个大的 ts 文件。"""

    output_ts.parent.mkdir(parents=True, exist_ok=True)
    with output_ts.open("wb") as writer:
        for path in segment_paths:
            writer.write(path.read_bytes())


def convert_ts_to_mp4(input_ts: Path, output_mp4: Path) -> bool:
    """
    调用 ffmpeg 把 ts 容器转换为 mp4。

    这里使用 -c copy，表示只换容器不重新编码，速度快且不会损失画质。
    """

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        print(f"未找到 ffmpeg，已保留合并后的 ts：{input_ts}")
        return False

    output_mp4.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg,
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(input_ts),
        "-c",
        "copy",
        str(output_mp4),
    ]
    result = subprocess.run(command, text=True, capture_output=True)
    if result.returncode != 0:
        print("ffmpeg 转换失败，已保留合并后的 ts 文件。错误信息：")
        print(result.stderr.strip())
        return False
    return True


def sanitize_stem(name: str) -> str:
    """把输出文件名转换成适合作为目录名的安全字符串。"""

    return re.sub(r"[^0-9A-Za-z_.-]+", "_", name).strip("_") or "video"


def run() -> None:
    """主流程：提取 m3u8 -> 解析 -> 下载 -> 解密 -> 合并 -> 转换。"""

    args = parse_args()
    output_path = Path(args.output)
    work_dir = Path(args.work_dir)

    headers = DEFAULT_HEADERS.copy()

    if args.m3u8_url:
        m3u8_url = args.m3u8_url
    else:
        # 从网页提取时，把网页地址作为 Referer，后续访问 m3u8/ts 时更像真实播放请求。
        headers["Referer"] = args.page_url
        candidates = extract_m3u8_urls_from_page(
            args.page_url,
            headers=headers,
            timeout=args.timeout,
        )
        if not candidates:
            raise RuntimeError(
                "没有在网页中匹配到 m3u8 地址。"
                "可以打开浏览器 F12 -> Network，筛选 m3u8 后用 --m3u8-url 传入。"
            )

        print("从网页中匹配到的 m3u8 地址：")
        for index, candidate in enumerate(candidates, start=1):
            print(f"  {index}. {candidate}")
        m3u8_url = candidates[0]

    print(f"使用 m3u8：{m3u8_url}")
    playlist = load_media_playlist(
        m3u8_url,
        headers=headers,
        timeout=args.timeout,
        variant_policy=args.variant,
    )
    if not playlist.segments:
        raise RuntimeError("m3u8 中没有解析到 ts 分片，无法继续下载。")

    encrypted_count = sum(
        1 for segment in playlist.segments if segment.key and segment.key.method != "NONE"
    )
    print(f"解析到 ts 分片：{len(playlist.segments)} 个")
    print(f"其中加密分片：{encrypted_count} 个")

    segment_dir = work_dir / sanitize_stem(output_path.stem)
    segment_paths = download_segments(
        playlist.segments,
        output_dir=segment_dir,
        headers=headers,
        timeout=args.timeout,
        max_workers=args.max_workers,
    )

    if output_path.suffix.lower() == ".ts":
        merged_ts = output_path
    else:
        merged_ts = work_dir / f"{sanitize_stem(output_path.stem)}.merged.ts"

    concat_ts_files(segment_paths, merged_ts)
    print(f"ts 合并完成：{merged_ts}")

    if output_path.suffix.lower() == ".mp4":
        if convert_ts_to_mp4(merged_ts, output_path):
            print(f"mp4 转换完成：{output_path}")
    elif output_path.suffix.lower() != ".ts":
        print(f"输出后缀不是 .mp4 或 .ts，已保留合并后的 ts：{merged_ts}")

    if args.cleanup:
        shutil.rmtree(segment_dir, ignore_errors=True)
        if merged_ts != output_path and merged_ts.exists():
            merged_ts.unlink()
        print("已清理中间分片文件。")


def main() -> int:
    """程序入口，统一捕获错误并给出易读提示。"""

    try:
        run()
    except KeyboardInterrupt:
        print("\n用户中断下载。")
        return 130
    except Exception as exc:
        print(f"运行失败：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
