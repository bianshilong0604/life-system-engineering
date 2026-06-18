#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
聊天机器人 → 日记接收桥（带 AI 自动分析）
======================================
从聊天机器人接收消息，写入今日日记 + AI 自动分析（关键词、情感、分类）。
"""

import sys, os, json, datetime, urllib.request, urllib.error, re
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from assistant import DIARY, today_str, load_models, active_model, call_llm
import prompts as P

KB_DIR = DIARY.parent / "05_知识库"
KB_DIR.mkdir(parents=True, exist_ok=True)


def append_diary_with_ai(text):
    """追加日记 + AI 分析标签"""
    text = (text or "").strip()
    if not text:
        return {"error": "日记内容不能为空"}
    
    DIARY.mkdir(parents=True, exist_ok=True)
    path = DIARY / f"{today_str()}.md"
    if not path.exists():
        path.write_text(f"# {today_str()} 日记\n", encoding="utf-8")
    
    stamp = datetime.datetime.now().strftime("%H:%M")
    tags = ""
    
    try:
        cfg_data = load_models()
        m = active_model(cfg_data)
        if m and m.get("api_key"):
            cfg = {
                "LLM_BASE_URL": m["base_url"],
                "LLM_MODEL": m["model"],
                "LLM_API_KEY": m["api_key"],
                "LLM_TEMPERATURE": m.get("temperature", "0.4"),
            }
            resp = call_llm(cfg, [{"role": "user", "content": P.diary_analyze(text)}])
            match = re.search(r'\{[^}]+\}', resp, re.DOTALL)
            if match:
                data = json.loads(match.group(0))
                kw = ",".join(data.get("keywords", []))
                emo = data.get("emotion", "")
                cat = data.get("category", "")
                imp = data.get("importance", 3)
                tags = f" `{cat}` `{emo}` `{kw}` `重要度{imp}`"
    except Exception as e:
        print(f"[WARN] AI分析失败: {e}", file=sys.stderr)
    
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"- {stamp} {text}{tags}\n")

    return {"ok": True, "path": str(path), "date": today_str()}


# ── HTTP 服务（webhook 接收）───────────────────────
def serve_http(port=8769):
    """启动轻量 HTTP 服务，接收 POST /diary"""
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code, data):
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8")
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                payload = {"text": body}

            text = payload.get("text", "").strip()
            msg_type = payload.get("type", "diary")

            if not text:
                text = payload.get("content", payload.get("message", "")).strip()

            if not text:
                return self._reply(400, {"error": "缺少 text 字段"})

            # 灵感已合并进日记,统一走 diary,灵感自己开头加 💡
            r = append_diary_with_ai(text)

            if r.get("ok"):
                self._reply(200, {"ok": True, "path": r["path"]})
            else:
                self._reply(500, r)

        def do_GET(self):
            if self.path == "/health":
                return self._reply(200, {"status": "ok", "diary": today_str()})
            self._reply(404, {"error": "not found"})

        def log_message(self, fmt, *args):
            pass

    server = HTTPServer(("0.0.0.0", port), Handler)
    print(f"📖 日记接收端已启动 → http://0.0.0.0:{port}")
    print(f"    POST /diary   — 写日记（含AI分析,灵感开头加💡）")
    print(f"    GET  /health  — 健康检查")
    print()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n接收端已关闭。")
        server.server_close()


# ── 入口 ────────────────────────────────────────
if __name__ == "__main__":
    if "--serve" in sys.argv:
        port = 8769
        for i, a in enumerate(sys.argv):
            if a.startswith("--port="):
                port = int(a.split("=")[1])
            elif a == "--port" and i + 1 < len(sys.argv):
                port = int(sys.argv[i + 1])
        serve_http(port)
        sys.exit(0)

    # CLI 模式
    entry_type = "diary"
    args = sys.argv[1:]

    if args and args[0] in ("--type", "-t"):
        if len(args) >= 2:
            entry_type = args[1]
            args = args[2:]
        else:
            args = []

    text = " ".join(args).strip() if args else None
    if not text:
        try:
            text = sys.stdin.read().strip()
        except Exception:
            pass

    if not text:
        print("❌ 用法: python diary_add.py \"日记内容\"")
        print("  或: echo \"内容\" | python diary_add.py")
        print("  或: python diary_add.py --serve  (HTTP 接收模式)")
        print("  灵感?开头加 💡 就行")
        sys.exit(1)

    # 灵感已合并进日记,统一走 diary
    r = append_diary_with_ai(text)
    label = "📝 日记"

    if r.get("ok"):
        print(f"✅ {label}已记录 → {r['path']}")
        if r.get("kb_path"):
            print(f"   知识库 → {r['kb_path']}")
    else:
        print(f"❌ 记录失败: {r.get('error')}")
        sys.exit(1)
