#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
个人总体设计部 · LLM 辅助工具
================================
脱离 Claude Code 独立运行,用你自己配置的 LLM API(DeepSeek/OpenAI/通义等)辅助复盘。

用法:
  python assistant.py review     # 每周复盘(交互式,跑完整闭环)
  python assistant.py pit        # 踩坑即时记一笔
  python assistant.py learn <文件或文本>   # 把一个输入沉淀进知识库
  python assistant.py checkup    # 月度体检
  python assistant.py test       # 测试 API 连通性

首次使用:
  1. cp config.env.example .env
  2. 编辑 .env 填入你的 API Key 和提供商地址
  3. python assistant.py test
"""

import os
import sys
import json
import uuid
import datetime
import urllib.request
import urllib.error
from pathlib import Path

import prompts as P  # 所有 LLM prompt 唯一来源(见 prompts.py),改一处三处生效

# Windows 终端中文输出防乱码(强制 stdout/stderr 用 UTF-8)
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ── 路径 ──────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent   # 项目根:个人总体设计部/
TOOLS = ROOT / "tools"
SUBSYS = ROOT / "01_子系统"
REVIEWS = ROOT / "02_每周复盘"
RULES = ROOT / "规则库.md"
KB_DIR = SUBSYS / "学习成长_知识库"

# ── 模型注册表(单一真相源;CLI + 网页统一从这里读 active)──────────
MODELS_JSON = TOOLS / "models.json"

def _parse_env():
    """读 tools/.env 成 dict(没有就空 dict)。仅用于首次种子化 models.json。"""
    env_path = TOOLS / ".env"
    cfg = {}
    if not env_path.exists():
        return cfg
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        cfg[k.strip()] = v.strip()
    return cfg

def _seed_models_from_env():
    """首次运行:把 .env 里那组配置种子化成 models.json 的第一个模型(标记 active)。"""
    env = _parse_env()
    entry = {
        "id": "default",
        "label": env.get("LLM_MODEL") or "默认模型",
        "model": env.get("LLM_MODEL") or "deepseek-chat",
        "base_url": env.get("LLM_BASE_URL") or "https://api.deepseek.com/v1",
        "api_key": env.get("LLM_API_KEY") or "",
        "temperature": env.get("LLM_TEMPERATURE") or "0.4",
    }
    data = {"active": "default", "models": [entry]}
    try:
        MODELS_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass  # 只读场景(权限)就只在内存里用
    return data

def load_models():
    """模型注册表。有 models.json 读它;否则从 .env 种子化(并落盘)。"""
    if MODELS_JSON.exists():
        try:
            data = json.loads(MODELS_JSON.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("models"), list) and data["models"]:
                return data
        except Exception:
            pass
    return _seed_models_from_env()

def save_models(data):
    MODELS_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def active_model(data=None):
    """返回当前 active 的模型条目;active 失效则回退第一个。"""
    data = data or load_models()
    aid = data.get("active")
    for m in data["models"]:
        if m.get("id") == aid:
            return m
    return data["models"][0] if data.get("models") else None

def _new_id(models):
    existing = {m.get("id") for m in models}
    for _ in range(32):
        i = uuid.uuid4().hex[:8]
        if i not in existing:
            return i
    return uuid.uuid4().hex

def add_model(label, model, base_url, api_key, temperature="0.4"):
    data = load_models()
    entry = {
        "id": _new_id(data["models"]),
        "label": (label or model or "未命名").strip(),
        "model": (model or "").strip(),
        "base_url": (base_url or "").strip(),
        "api_key": (api_key or "").strip(),
        "temperature": (temperature or "0.4").strip(),
    }
    if not entry["model"] or not entry["base_url"] or not entry["api_key"]:
        return {"error": "模型名 / base_url / api_key 都不能为空"}
    data["models"].append(entry)
    if not data.get("active"):
        data["active"] = entry["id"]
    save_models(data)
    return {"ok": True, "id": entry["id"]}

def update_model(mid, label=None, model=None, base_url=None, api_key=None, temperature=None):
    data = load_models()
    for m in data["models"]:
        if m.get("id") == mid:
            if label is not None:
                m["label"] = (label or m["label"]).strip()
            if model is not None:
                m["model"] = (model or m["model"]).strip()
            if base_url is not None:
                m["base_url"] = (base_url or m["base_url"]).strip()
            # api_key 仅在显式提供、且不是掩码占位(含 •)时才覆盖
            if api_key and "•" not in api_key:
                m["api_key"] = api_key.strip()
            if temperature is not None:
                m["temperature"] = (temperature or m["temperature"]).strip()
            save_models(data)
            return {"ok": True}
    return {"error": "找不到该模型"}

def delete_model(mid):
    data = load_models()
    if len(data["models"]) <= 1:
        return {"error": "至少保留一个模型"}
    data["models"] = [m for m in data["models"] if m.get("id") != mid]
    if data.get("active") == mid:
        data["active"] = data["models"][0]["id"]
    save_models(data)
    return {"ok": True}

def activate_model(mid):
    data = load_models()
    if not any(m.get("id") == mid for m in data["models"]):
        return {"error": "找不到该模型"}
    data["active"] = mid
    save_models(data)
    return {"ok": True}

# ── 配置加载(active 模型 → call_llm 用的 cfg)──────────────────────
def load_config():
    """统一入口:CLI 与网页都走这里。返回当前 active 模型的 cfg 字典。"""
    data = load_models()
    entry = active_model(data)
    if not entry or not entry.get("api_key") or str(entry["api_key"]).startswith("sk-xxx"):
        sys.exit(
            "❌ 没有可用模型。\n"
            "   方式一:在可视化看板「模型管理」里添加一个填了真实密钥的模型。\n"
            "   方式二:在 tools/.env 填 LLM_API_KEY(删掉 tools/models.json 让它重新种子化)。"
        )
    return {
        "LLM_API_KEY": entry["api_key"],
        "LLM_BASE_URL": entry.get("base_url") or "https://api.deepseek.com/v1",
        "LLM_MODEL": entry.get("model") or "deepseek-chat",
        "LLM_TEMPERATURE": entry.get("temperature") or "0.4",
    }

# ── LLM 调用(OpenAI 兼容接口,纯标准库)──────────────
def call_llm(cfg, messages):
    url = cfg["LLM_BASE_URL"].rstrip("/") + "/chat/completions"
    payload = {
        "model": cfg["LLM_MODEL"],
        "messages": messages,
        "temperature": float(cfg["LLM_TEMPERATURE"]),
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + cfg["LLM_API_KEY"],
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        sys.exit(f"❌ API 报错 {e.code}: {body[:400]}")
    except urllib.error.URLError as e:
        sys.exit(f"❌ 连接失败: {e.reason}（检查网络 / base_url 是否正确）")

# ── 读取系统上下文 ─────────────────────────────────
def read_file(p):
    p = Path(p)
    return p.read_text(encoding="utf-8") if p.exists() else ""

def system_context():
    parts = ["# 这是我的「个人总体设计部」系统当前状态\n"]
    parts.append("## 总纲领\n" + read_file(ROOT / "00_总纲领.md"))
    parts.append("## 规则库\n" + read_file(RULES))
    for name in ["研究工作", "学习成长", "复盘进化"]:
        parts.append(f"## 子系统:{name}\n" + read_file(SUBSYS / f"{name}.md"))
    latest = latest_review()
    if latest:
        parts.append(f"## 上一份复盘({latest.name})\n" + read_file(latest))
    return "\n\n".join(parts)

def latest_review():
    files = sorted(REVIEWS.glob("20*.md"))
    return files[-1] if files else None

def today_str():
    return datetime.date.today().isoformat()

# 由于 Claude Code 沙箱禁用了 Date.now(),这里在真实 Python 运行时无此限制,
# datetime 正常可用。

# ── 交互辅助 ───────────────────────────────────────
def ask(prompt_text):
    print("\n" + prompt_text)
    try:
        return input("> ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n已取消。")
        sys.exit(0)

SYSTEM_ROLE = (
    "你是用户的「个人总体设计部」AI 助手,这套系统基于钱学森系统工程思想。"
    "当前用户只聚焦 3 个子系统:研究工作、学习成长、复盘进化(健康/关系/财务已封存)。"
    "你的风格:直接、不客套、说真话,包括指出系统可能太重的结论。"
    "判断失败时区分「能力问题」vs「系统问题」,系统问题再归类("
    "目标不清/时间没留/工具差/边界乱/反馈少/标准不清)。"
    "提炼规则要可执行、不靠意志力,格式仿照规则库里的 R 编号条目。"
)

# ── 模式实现 ───────────────────────────────────────
def mode_test(cfg):
    print(f"提供商: {cfg['LLM_BASE_URL']}\n模型: {cfg['LLM_MODEL']}")
    out = call_llm(cfg, [{"role": "user", "content": "回复一个字:通"}])
    print("✅ API 连通,模型回复:", out.strip())

def mode_review(cfg):
    ctx = system_context()
    print("=== 本周复盘(AI 辅助)===")
    print("AI 会基于你的系统状态,一步步带你跑 Verify→Reflect→Patch→Plan。")
    # 让 AI 先基于上周 Plan 生成要问的问题
    q = call_llm(cfg, [
        {"role": "system", "content": SYSTEM_ROLE},
        {"role": "user", "content": P.verify_question(ctx)},
    ])
    print("\n【Verify】\n" + q)
    verify = ask("逐条回答(一段话即可):")

    r = call_llm(cfg, [
        {"role": "system", "content": SYSTEM_ROLE},
        {"role": "user", "content": P.reflect_patch(ctx, verify)},
    ])
    print("\n【Reflect + Patch】\n" + r)

    plan = call_llm(cfg, [
        {"role": "system", "content": SYSTEM_ROLE},
        {"role": "user", "content": P.plan(ctx, verify, r)},
    ])
    print("\n【下周 Plan】\n" + plan)

    # 写回复盘文件
    date = today_str()
    content = f"""# {date}(AI 辅助复盘)

## 一、Verify
{verify}

## 二、Reflect + 三、Patch
{r}

## 四、下周 Plan
{plan}

---
> 本份由 tools/assistant.py 生成。如产出新规则,请确认后手动加入 规则库.md
"""
    out_path = REVIEWS / f"{date}.md"
    if out_path.exists():
        out_path = REVIEWS / f"{date}_AI.md"
    out_path.write_text(content, encoding="utf-8")
    print(f"\n✅ 已写入 {out_path}")
    print("💡 如果上面产出了值得长期保留的规则,记得手动加进 规则库.md(我没替你自动改,避免误写)。")

def mode_pit(cfg):
    pit = ask("描述你踩的坑:")
    out = call_llm(cfg, [
        {"role": "system", "content": SYSTEM_ROLE},
        {"role": "user", "content": P.pit(read_file(RULES), pit)},
    ])
    print("\n" + out)

def mode_learn(cfg, arg):
    src = read_file(arg) if Path(arg).exists() else arg
    if not src:
        sys.exit("❌ 没有内容。用法: python assistant.py learn <文件路径或直接粘文本>")
    out = call_llm(cfg, [
        {"role": "system", "content": SYSTEM_ROLE},
        {"role": "user", "content": P.learn(src)},
    ])
    KB_DIR.mkdir(exist_ok=True)
    # 用 AI 输出首行做文件名兜底
    title = "知识_" + today_str()
    path = KB_DIR / f"{title}.md"
    path.write_text(out, encoding="utf-8")
    print("\n" + out)
    print(f"\n✅ 已存到 {path}(可自行改名为更贴切的主题)")

def mode_checkup(cfg):
    ctx = system_context()
    recent = sorted(REVIEWS.glob("20*.md"))[-4:]
    revs = "\n\n".join(f"### {p.name}\n{read_file(p)}" for p in recent)
    out = call_llm(cfg, [
        {"role": "system", "content": SYSTEM_ROLE},
        {"role": "user", "content": P.checkup(ctx, revs)},
    ])
    print("\n" + out)

# ── 入口 ───────────────────────────────────────────
def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cfg = load_config()
    mode = sys.argv[1]
    if mode == "test":
        mode_test(cfg)
    elif mode == "review":
        mode_review(cfg)
    elif mode == "pit":
        mode_pit(cfg)
    elif mode == "learn":
        mode_learn(cfg, sys.argv[2] if len(sys.argv) > 2 else "")
    elif mode == "checkup":
        mode_checkup(cfg)
    else:
        print(__doc__)

if __name__ == "__main__":
    main()
