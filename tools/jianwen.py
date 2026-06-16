#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
今日见闻池 · 数据层 + 灵感随记分析 + 归档进知识库
====================================================
连接外部信息源与知识库的中枢（你可以接自己的来源）：
  · 视频总结脚本 → 写入今日见闻池（add-video）
  · 灵感随记 → LLM 分析 → 写入今日见闻池（add-inspiration）
  · 看板每晚筛选 → 归档进「学习成长_知识库」/ 丢弃

数据落点:
  <项目根>/03_今日见闻/YYYY-MM-DD.json

CLI 用法:
  python jianwen.py add-video   --url <链接> --md <视频总结输出的.md路径>
  python jianwen.py add-inspiration "我的灵感原文"     # LLM 分析后入池，打印分析结果
  python jianwen.py list [YYYY-MM-DD]                  # 查看某天见闻池
  python jianwen.py archive --date <日期> --id <编号>  # 归档进知识库
  python jianwen.py discard --date <日期> --id <编号>  # 丢弃
"""

import os
import sys
import json
import argparse
import datetime
from pathlib import Path

# Windows 终端中文防乱码
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

# 复用 assistant.py（同目录）的路径与 LLM 调用
sys.path.insert(0, str(Path(__file__).resolve().parent))
import assistant as A  # noqa: E402
import prompts as P    # noqa: E402  LLM prompt 唯一来源

ROOT = A.ROOT
KB_DIR = A.KB_DIR                       # 学习成长_知识库（归档目标）
JIANWEN_DIR = ROOT / "03_今日见闻"      # 今日见闻池

VALID_TYPES = ("video", "inspiration")
VALID_STATUS = ("pending", "archived", "discarded")


# ── 工具 ────────────────────────────────────────────────
def today_str():
    return datetime.date.today().isoformat()


def now_hm():
    return datetime.datetime.now().strftime("%H:%M")


def pool_path(date=None):
    return JIANWEN_DIR / f"{date or today_str()}.json"


def load_pool(date=None):
    p = pool_path(date)
    if not p.exists():
        return {"date": date or today_str(), "items": []}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"date": date or today_str(), "items": []}


def save_pool(pool):
    JIANWEN_DIR.mkdir(parents=True, exist_ok=True)
    p = pool_path(pool["date"])
    p.write_text(json.dumps(pool, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def _next_id(pool):
    return max((it.get("id", 0) for it in pool["items"]), default=0) + 1


def add_item(item_type, title, summary, raw="", source_url="", platform="", date=None):
    """通用入池。返回写入的条目。"""
    if item_type not in VALID_TYPES:
        raise ValueError(f"未知类型 {item_type}，应为 {VALID_TYPES}")
    pool = load_pool(date)
    item = {
        "id": _next_id(pool),
        "time": now_hm(),
        "type": item_type,
        "platform": platform,
        "title": (title or "").strip()[:120] or "(无标题)",
        "summary": (summary or "").strip(),
        "raw": (raw or "").strip(),
        "source_url": (source_url or "").strip(),
        "status": "pending",
        "kb_file": None,
    }
    pool["items"].append(item)
    save_pool(pool)
    return item


def find_item(pool, item_id):
    for it in pool["items"]:
        if it.get("id") == int(item_id):
            return it
    return None


# ── 灵感随记：LLM 分析（prompt 见 prompts.inspiration）─────
def analyze_inspiration(raw):
    """调用 LLM 分析灵感，返回结构化 Markdown 文本。"""
    cfg = A.load_config()
    out = A.call_llm(cfg, [
        {"role": "system", "content": A.SYSTEM_ROLE},
        {"role": "user", "content": P.inspiration(raw)},
    ])
    return out


def add_inspiration(raw, date=None):
    """灵感随记：LLM 分析 → 入池。返回 (item, 分析文本)。"""
    if not raw.strip():
        raise ValueError("灵感原文为空")
    analysis = analyze_inspiration(raw)
    # 标题取原文首行/前 24 字
    first_line = raw.strip().splitlines()[0]
    title = first_line[:24] + ("…" if len(first_line) > 24 else "")
    item = add_item("inspiration", title=title, summary=analysis, raw=raw, date=date)
    return item, analysis


# ── 归档：结构化进知识库（prompt 见 prompts.archive）───────


def _safe_name(s):
    bad = '\\/:*?"<>|\n\r\t'
    for c in bad:
        s = s.replace(c, "_")
    return s.strip()[:48] or "知识"


def archive_item(date, item_id):
    """归档：LLM 结构化 → 写入学习成长_知识库 → 标记 archived。"""
    pool = load_pool(date)
    it = find_item(pool, item_id)
    if not it:
        raise ValueError(f"{date} 见闻池里找不到 id={item_id}")
    if it["status"] == "archived":
        return {"error": "该条已归档", "kb_file": it.get("kb_file")}

    body = it.get("summary") or it.get("raw") or it.get("title")
    cfg = A.load_config()
    structured = A.call_llm(cfg, [
        {"role": "system", "content": A.SYSTEM_ROLE},
        {"role": "user", "content": P.archive(
            title=it["title"],
            src=it.get("source_url") or it.get("platform") or "灵感随记",
            body=body[:8000])},
    ])

    KB_DIR.mkdir(parents=True, exist_ok=True)
    tag = "视频" if it["type"] == "video" else "灵感"
    fname = f"{tag}_{date}_{_safe_name(it['title'])}.md"
    kb_path = KB_DIR / fname
    header = f"# {it['title']}\n\n> 来源：{it.get('source_url') or it.get('platform') or '灵感随记'} · 归档于 {today_str()}\n\n"
    kb_path.write_text(header + structured, encoding="utf-8")

    it["status"] = "archived"
    it["kb_file"] = str(kb_path)
    save_pool(pool)
    return {"ok": True, "kb_file": str(kb_path), "text": structured}


def discard_item(date, item_id):
    pool = load_pool(date)
    it = find_item(pool, item_id)
    if not it:
        raise ValueError(f"{date} 见闻池里找不到 id={item_id}")
    it["status"] = "discarded"
    save_pool(pool)
    return {"ok": True}


# ── 视频入池（配合外部视频总结脚本输出的 .md）──────────────
def add_video_from_md(url, md_path, date=None):
    """读取视频总结脚本产出的 .md，抽标题+总结入池。"""
    p = Path(md_path)
    if not p.exists():
        raise ValueError(f"找不到视频总结文件：{md_path}")
    text = p.read_text(encoding="utf-8")
    # 标题：首个 # 行
    title = ""
    platform = ""
    for line in text.splitlines():
        if line.startswith("# ") and not title:
            title = line[2:].strip()
        if "哔哩哔哩" in line or "Bilibili" in line:
            platform = "bilibili"
        if "抖音" in line or "Douyin" in line:
            platform = "douyin"
    item = add_item("video", title=title or p.stem, summary=text,
                    raw="", source_url=url, platform=platform, date=date)
    return item


# ── CLI ─────────────────────────────────────────────────
def _print_json(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main():
    ap = argparse.ArgumentParser(description="今日见闻池工具")
    sub = ap.add_subparsers(dest="cmd")

    p_v = sub.add_parser("add-video", help="视频总结入池")
    p_v.add_argument("--url", required=True)
    p_v.add_argument("--md", required=True, help="视频总结脚本输出的 .md 路径")
    p_v.add_argument("--date")

    p_i = sub.add_parser("add-inspiration", help="灵感随记：LLM 分析后入池")
    p_i.add_argument("raw", help="灵感原文")
    p_i.add_argument("--date")

    p_l = sub.add_parser("list", help="查看见闻池")
    p_l.add_argument("date", nargs="?")

    p_a = sub.add_parser("archive", help="归档进知识库")
    p_a.add_argument("--date", required=True)
    p_a.add_argument("--id", required=True)

    p_d = sub.add_parser("discard", help="丢弃")
    p_d.add_argument("--date", required=True)
    p_d.add_argument("--id", required=True)

    args = ap.parse_args()

    if args.cmd == "add-video":
        item = add_video_from_md(args.url, args.md, args.date)
        print(f"✅ 已入池 [{item['id']}] {item['title']}")
        _print_json(item)
    elif args.cmd == "add-inspiration":
        item, analysis = add_inspiration(args.raw, args.date)
        print(f"✅ 灵感已入池 [{item['id']}] {item['title']}\n")
        print(analysis)
    elif args.cmd == "list":
        pool = load_pool(args.date)
        print(f"# 今日见闻池 · {pool['date']}（共 {len(pool['items'])} 条）\n")
        for it in pool["items"]:
            mark = {"pending": "⬜待筛选", "archived": "✅已归档", "discarded": "🗑已丢弃"}.get(it["status"], it["status"])
            print(f"[{it['id']}] {mark} · {it['type']} · {it['time']} · {it['title']}")
    elif args.cmd == "archive":
        _print_json(archive_item(args.date, args.id))
    elif args.cmd == "discard":
        _print_json(discard_item(args.date, args.id))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
