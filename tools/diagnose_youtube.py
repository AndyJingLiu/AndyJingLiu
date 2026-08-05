"""Diagnose why the site is not showing the channel's latest videos.

Run from the project root:

    .venv/bin/python tools/diagnose_youtube.py

Prints nothing secret, so the output is safe to paste or screenshot.
"""

import sys
import re
import urllib.request
import urllib.error
import xml.etree.ElementTree as ElementTree
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as A  # noqa: E402

ATOM = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
}


def main() -> None:
    channel_id = A.app.config["YOUTUBE_CHANNEL_ID"]
    print("=" * 62)
    print("1. 配置")
    print(f"   YOUTUBE_CHANNEL_ID = {channel_id}")
    print(f"   长度 = {len(channel_id)}  (应为 24)")

    ok = re.fullmatch(r"UC[A-Za-z0-9_-]{22}", channel_id)
    print(f"   通过 app.py 的格式校验 = {bool(ok)}")
    if not ok:
        print("\n   >>> 结论：频道 ID 格式不对，fetch_youtube_videos() 直接返回空。")
        return

    print(f"   数据库路径 = {A.app.config['DATABASE']}")

    print("\n2. 抓取 RSS")
    url = "https://www.youtube.com/feeds/videos.xml?channel_id=" + channel_id
    print(f"   {url}")
    data = None
    try:
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/atom+xml, application/xml;q=0.9",
                "User-Agent": "AndyJingLiu.com/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = resp.status
            data = resp.read()
        print(f"   HTTP {status}，收到 {len(data)} 字节")
    except urllib.error.HTTPError as exc:
        print(f"   HTTP {exc.code}：RSS 不可用，将继续测试频道页面备用通道。")
    except Exception as exc:  # noqa: BLE001
        print(f"   >>> 网络失败：{type(exc).__name__}: {exc}")
        print("   将继续测试频道页面备用通道。")

    print("\n3. 解析 feed")
    entries = []
    parsed = []
    if data is None:
        print("   跳过：RSS 没有返回数据。")
    else:
        try:
            root = ElementTree.fromstring(data)
        except ElementTree.ParseError as exc:
            print(f"   >>> XML 解析失败：{exc}")
        else:
            print(f"   feed 标题 = {root.findtext('atom:title', namespaces=ATOM)}")
            entries = root.findall("atom:entry", ATOM)
            print(f"   条目总数 = {len(entries)}")

            if entries:
                print("\n   逐条（看 URL 里有没有 /shorts/）：")
                for entry in entries[:15]:
                    title = entry.findtext("atom:title", default="", namespaces=ATOM)
                    link = entry.find("atom:link[@rel='alternate']", ATOM)
                    href = link.get("href", "") if link is not None else ""
                    mark = "SHORT" if "/shorts/" in href else "  ok "
                    print(f"     [{mark}] {title[:44]:46s} {href}")
            parsed = A.parse_youtube_feed(data)

    print("\n4. 过 app.py 自己的过滤器")
    print(f"   parse_youtube_feed() 返回 {len(parsed)} 条")

    print("\n5. 测试频道页面备用通道")
    try:
        page_videos = A.fetch_youtube_channel_page(channel_id)
    except Exception as exc:  # noqa: BLE001
        page_videos = []
        print(f"   >>> 备用通道失败：{type(exc).__name__}: {exc}")
    print(f"   fetch_youtube_channel_page() 返回 {len(page_videos)} 条")
    for video in page_videos[:5]:
        print(f"     - {video['title'][:60]}")

    print("\n6. 站点实际会显示什么")
    A.youtube_feed_cache.update({"channel_id": "", "expires_at": 0, "videos": []})
    shown = A.latest_videos(5)
    print(f"   latest_videos(5) 返回 {len(shown)} 条")
    for video in shown:
        print(f"     - {video['title'][:60]}")

    print("\n" + "=" * 62)
    print("结论")
    if parsed:
        print("  RSS 抓取正常。")
    elif page_videos:
        print("  RSS 不可用，但频道页面备用同步正常。")
    elif entries and not parsed:
        print("  你的视频全部被 Shorts 过滤器滤掉了。")
    else:
        print("  RSS 和频道页面都不可用，需要检查网络或 YouTube 页面结构。")

    if not shown:
        print("  网站当前没有可显示的视频。")
    elif shown:
        print("  网站同步链路正常；内容最多缓存 15 分钟。")


if __name__ == "__main__":
    main()
