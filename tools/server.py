#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
个人总体设计部 · 可视化看板（本地服务器）
============================================
纯 Python 标准库，零 pip 依赖。读你的真实 MD 文件渲染看板，
并复用 assistant.py 调用 SenseNova 触发 AI 复盘。

用法:
  python server.py            # 启动并自动打开浏览器
  python server.py --no-open  # 启动但不自动开浏览器
  端口默认 8770，被占用时自动 +1 往上找。

功能（范围：看板内查看 + 就地编辑 + 触发AI复盘）:
  · 查看：硬约束 / 3 个子系统 / 规则库 / 复盘历史 / 知识库 / 核心闭环图
  · 点任意条目 → 阅读弹窗看原文，「✏️ 编辑」就地把 markdown 写回（不依赖外部编辑器）
  · 硬约束：每条可就地编辑 / 删除 / 新增（写回总纲领.md）
  · AI 按钮：月度体检 / 踩坑记一笔 / 沉淀知识 / 本周复盘（带写回）
"""

import sys
import json
import datetime
import html
import webbrowser
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Windows 终端中文防乱码
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

# 复用 assistant.py（同目录）
sys.path.insert(0, str(Path(__file__).resolve().parent))
import assistant as A  # noqa: E402
import jianwen as J  # noqa: E402  今日见闻池
import prompts as P  # noqa: E402  LLM prompt 唯一来源

ROOT = A.ROOT
SUBSYS = A.SUBSYS
REVIEWS = A.REVIEWS
RULES = A.RULES
KB_DIR = A.KB_DIR
DIARY_DIR = A.DIARY
CHARTER = ROOT / "00_总纲领.md"

# 知识库规模"绊线":达到即提醒可考虑启动 GBrain（见 GBrain启动预案.md）
GBRAIN_THRESHOLD = 300

# ── 解析：把真实 MD 文件读成看板数据 ───────────────────
def parse_constraints():
    """从总纲领抓硬约束（- [ ] / - [x] 行，仅"硬约束"小节内）。"""
    text = A.read_file(CHARTER)
    out, in_section = [], False
    for line in text.splitlines():
        if line.startswith("## ") and "硬约束" in line:
            in_section = True
            continue
        if in_section and line.startswith("## "):
            break
        s = line.strip()
        if in_section and (s.startswith("- [ ]") or s.startswith("- [x]")):
            done = s.startswith("- [x]")
            out.append({"done": done, "text": s[5:].strip()})
    return out


def _find_constraint_section(lines):
    """返回 (start_idx, end_idx)。start=硬约束标题行号;end=下一个 ## 行号或 len(lines)。找不到返回 None。"""
    start = None
    for i, line in enumerate(lines):
        if line.startswith("## ") and "硬约束" in line:
            start = i
            break
    if start is None:
        return None
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("## "):
            return start, j
    return start, len(lines)


def _rewrite_constraint(action, index=None, done=None, text=None):
    """按行改写总纲领的硬约束小节。index 口径与 parse_constraints 一致。
    add/delete/update 三种操作,全部就地改 总纲领.md。git 基线是后悔药。"""
    import re
    const_re = re.compile(r"^(\s*-\s*\[)([ xX])(\]\s*)(.*)$")
    raw_bytes = CHARTER.read_bytes()
    nl = "\r\n" if b"\r\n" in raw_bytes else "\n"
    lines = raw_bytes.decode("utf-8").split(nl)
    sec = _find_constraint_section(lines)
    if sec is None:
        return {"error": "总纲领里找不到「硬约束」小节"}
    start, end = sec
    idx_line = [ln for ln in range(start + 1, end) if const_re.match(lines[ln])]

    if action == "add":
        if not (text or "").strip():
            return {"error": "约束内容不能为空"}
        newline = "- [ ] " + text.strip()
        lines.insert((idx_line[-1] + 1) if idx_line else (start + 1), newline)
    elif action == "delete":
        if index is None or not (0 <= index < len(idx_line)):
            return {"error": "索引越界"}
        del lines[idx_line[index]]
    elif action == "update":
        if index is None or not (0 <= index < len(idx_line)):
            return {"error": "索引越界"}
        ln = idx_line[index]
        m = const_re.match(lines[ln])
        is_done = bool(done) if done is not None else (m.group(2) in ("x", "X"))
        new_text = (text if text is not None else m.group(4)).strip()
        if not new_text:
            return {"error": "约束内容不能为空"}
        lines[ln] = "- [" + ("x" if is_done else " ") + "] " + new_text
    else:
        return {"error": f"未知操作 {action}"}

    CHARTER.write_bytes(nl.join(lines).encode("utf-8"))
    return {"ok": True}


# ── 规则库：逐条块级改写（每条规则是一个 ## R{n} 块，整块文本框编辑）──
_RULE_HDR_RE = None  # 编译一次(模块级 import re as _re 已在下方，但本函数可能更早被调用时定义，故用延迟)

def _rule_header_re():
    global _RULE_HDR_RE
    if _RULE_HDR_RE is None:
        _RULE_HDR_RE = _re.compile(r"^##\s+R(\d+)\s*[—-]\s*(.+)$")
    return _RULE_HDR_RE

def _rule_blocks(lines):
    """把 规则库.md 拆成 [(header_idx, end_idx, n, title)] 列表。
    每条规则的块 = [header_idx, end_idx)，end_idx = 下一条 R 标题 / 首个 `---` 分隔符 / 文件末尾。
    用 `---` 作边界,使最后一条规则的块不含结尾的『下面留空』说明,add 自然落在 `---` 之前。
    跳过『## 格式』模板行(非数字编号)。"""
    hdr = _rule_header_re()
    starts = [i for i, l in enumerate(lines) if hdr.match(l)]
    blocks = []
    for k, s in enumerate(starts):
        e = len(lines)
        for j in range(s + 1, len(lines)):
            if hdr.match(lines[j]) or lines[j].strip() == "---":
                e = j
                break
        m = hdr.match(lines[s])
        blocks.append((s, e, m.group(1), m.group(2).strip()))
    return blocks

def _content_end(lines, s, e):
    """块 [s:e) 的内容末行索引(剥离尾随空行后)。新增/删除的插入点用它,保分隔空行。"""
    k = e
    while k > s and lines[k - 1].strip() == "":
        k -= 1
    return k

def rewrite_rule(action, index=None, text=None):
    """按块改写 规则库.md。index 口径与 parse_rules() 一致(按 R 号顺序)。
    整块文本框编辑:text 含标题行(## R{n} — 标题)+ 字段。- 换行风格原样保留。
    update 只换内容行、不动尾随分隔空行 → get/update 往返字节精确。
    add 在最后一条规则的内容后插入(自动编号),delete 精确反演 add → add/delete 往返字节精确。"""
    raw_bytes = RULES.read_bytes()
    nl = "\r\n" if b"\r\n" in raw_bytes else "\n"
    lines = raw_bytes.decode("utf-8").split(nl)
    blocks = _rule_blocks(lines)

    def block_text(i):
        s, e, _, _ = blocks[i]
        return nl.join(lines[s:_content_end(lines, s, e)])

    if action == "list":
        return {"ok": True, "items": [
            {"index": i, "n": blocks[i][2], "title": blocks[i][3],
             "text": block_text(i)}
            for i in range(len(blocks))
        ]}
    if action == "get":
        if index is None or not (0 <= index < len(blocks)):
            return {"error": "索引越界"}
        return {"ok": True, "text": block_text(index)}

    if action == "add":
        body = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not body:
            return {"error": "规则内容不能为空"}
        # 自动编号:取现有最大 R 号 +1;若用户已写了 ## R\d 头,保留其原文
        if not _re.match(r"^##\s+R\d+\b", body.split("\n", 1)[0]):
            next_n = max([int(b[2]) for b in blocks], default=0) + 1
            first = body.split("\n", 1)[0].strip().lstrip("#").strip()
            rest = body.split("\n", 1)[1] if "\n" in body else ""
            body = f"## R{next_n} — {first}" + (f"\n{rest}" if rest else "")
        # 插在最后一条规则的内容之后(尾随分隔区之前):[""]+内容,前面留一个空行分隔
        if blocks:
            ps, pe, _, _ = blocks[-1]
            insert_at = _content_end(lines, ps, pe)
        else:
            insert_at = len(lines)
        new_lines = [""] + body.split("\n")
        lines[insert_at:insert_at] = new_lines
    elif action == "update":
        if index is None or not (0 <= index < len(blocks)):
            return {"error": "索引越界"}
        body = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not body:
            return {"error": "规则内容不能为空"}
        s, e, _, _ = blocks[index]
        ce = _content_end(lines, s, e)
        # 只换内容行 [s:ce),尾随分隔空行 [ce:e) 原样保留
        lines[s:ce] = body.split("\n")
    elif action == "delete":
        if index is None or not (0 <= index < len(blocks)):
            return {"error": "索引越界"}
        s, e, _, _ = blocks[index]
        ce = _content_end(lines, s, e)
        # 连同 add 当初插入的前导空行一起删,精确反演 add
        start = s - 1 if s > 0 and lines[s - 1].strip() == "" else s
        del lines[start:ce]
    else:
        return {"error": f"未知操作 {action}"}

    RULES.write_bytes(nl.join(lines).encode("utf-8"))
    return {"ok": True}


# ── 子系统进行中事项：逐条改写（解除 2 件上限，看板内增/删/改）──
def _find_todo_section(lines):
    """返回 (start, end):进行中事项标题行号 与 下一 ## 行号/末尾。"""
    start = None
    for i, l in enumerate(lines):
        if l.startswith("## ") and "进行中事项" in l:
            start = i
            break
    if start is None:
        return None
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("## "):
            return start, j
    return start, len(lines)

def rewrite_todo(subsystem, action, index=None, text=None):
    """改写 01_子系统/{name}.md 的『进行中事项』小节。- 拒空;保换行风格。"""
    name = (subsystem or "").strip()
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return {"error": "非法子系统名"}
    f = SUBSYS / f"{name}.md"
    if not f.is_file():
        return {"error": f"找不到子系统文件:{name}"}
    raw_bytes = f.read_bytes()
    nl = "\r\n" if b"\r\n" in raw_bytes else "\n"
    lines = raw_bytes.decode("utf-8").split(nl)
    sec = _find_todo_section(lines)
    if sec is None:
        return {"error": f"{name}.md 里找不到『进行中事项』小节"}
    start, end = sec
    # 事项行 = 小节内、以 - 开头、去掉前导空格后非空、不是 > 提示
    item_idx = [ln for ln in range(start + 1, end)
                if lines[ln].lstrip().startswith("-") and lines[ln].lstrip()[1:].strip()]
    body = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()

    if action == "list":
        return {"ok": True, "items": [
            {"index": k, "text": lines[ln].lstrip()[1:].strip()}
            for k, ln in enumerate(item_idx)
        ]}

    if action == "add":
        if not body:
            return {"error": "事项内容不能为空"}
        newline = "- " + body
        # 插到小节里最后一条事项之后;没有事项则插到标题后(跳过 > 提示行)
        ins = (item_idx[-1] + 1) if item_idx else (start + 1)
        lines.insert(ins, newline)
    elif action == "update":
        if index is None or not (0 <= index < len(item_idx)):
            return {"error": "索引越界"}
        if not body:
            return {"error": "事项内容不能为空"}
        lines[item_idx[index]] = "- " + body
    elif action == "delete":
        if index is None or not (0 <= index < len(item_idx)):
            return {"error": "索引越界"}
        del lines[item_idx[index]]
    else:
        return {"error": f"未知操作 {action}"}

    f.write_bytes(nl.join(lines).encode("utf-8"))
    return {"ok": True}


def parse_rules():
    """规则库的 ## R{n} — {title} 标题 + 该条正文(触发/类型/规则/日期)。
    n 必须是数字(排除格式说明里的模板行)。正文供看板『点单条就地展开』用。"""
    out = []
    lines = A.read_file(RULES).splitlines()
    for s, e, n, title in _rule_blocks(lines):
        body = "\n".join(lines[s + 1:e]).strip()
        out.append({"n": n, "title": title, "body": body})
    return out

def parse_subsystem(name):
    """读单个子系统：标题摘要 + 进行中事项条数。"""
    f = SUBSYS / f"{name}.md"
    text = A.read_file(f)
    # 一句话职责：首个 "> 职责：..." 行
    duty = ""
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("> 职责"):
            duty = s.lstrip("> ").replace("职责：", "").replace("职责:", "").strip()
            break
    # 进行中事项：## 本子系统的进行中事项 下的非空 - 项
    todos, in_sec = [], False
    for line in text.splitlines():
        if line.startswith("## ") and "进行中事项" in line:
            in_sec = True
            continue
        if in_sec and line.startswith("## "):
            break
        s = line.strip()
        if in_sec and s.startswith("-"):
            item = s[1:].strip()
            if item and not item.startswith(">"):
                todos.append(item)
    return {
        "name": name,
        "duty": duty,
        "todos": todos,
        "exists": f.exists(),
        "path": str(f),
    }

def parse_reviews():
    """02_每周复盘/ 下的 20*.md，按日期倒序。"""
    files = sorted(REVIEWS.glob("20*.md"), reverse=True)
    return [{"name": p.stem, "path": str(p)} for p in files]

def parse_diary():
    """03_每日日记/ 下的 20*.md,按日期倒序。每天含多条 HH:MM 条目。
    _周整理_ 前缀文件不被 glob('20*') 匹配,自动排除。"""
    if not DIARY_DIR.exists():
        return []
    out = []
    for p in sorted(DIARY_DIR.glob("20*.md"), reverse=True):
        raw = p.read_text(encoding="utf-8")
        lines = [l for l in raw.splitlines() if l.strip() and not l.startswith("#")]
        preview = ""
        for l in lines:
            if l.startswith("- "):
                rest = l[2:]
                # 去 "HH:MM " 前缀
                if len(rest) > 5 and rest[2] == ":":
                    rest = rest[5:].lstrip()
                preview = rest[:40]
                break
        out.append({
            "name": p.stem,
            "short": p.stem[5:],   # MM-DD
            "count": len(lines),
            "preview": preview,
            "body": "\n".join(lines),
        })
    return out

def parse_kb():
    """学习成长_知识库/ 下的条目（可能为空）。"""
    if not KB_DIR.exists():
        return []
    files = sorted(KB_DIR.glob("*.md"), reverse=True)
    return [{"name": p.stem, "path": str(p)} for p in files]

def parse_jianwen():
    """今日见闻池:扫所有日期文件(不只今天),返回带 date 字段的全部条目。
    pending 排前、组内新→旧,避免历史 pending 被埋在当天文件里烂掉。"""
    items = []
    for p in sorted(J.JIANWEN_DIR.glob("*.json")):
        date = p.stem
        try:
            pool = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        for it in pool.get("items", []):
            it = dict(it)            # 拷贝,不污染磁盘上的原 item
            it["date"] = date
            items.append(it)
    # 稳定排序:先按 date 新→旧,再稳定地把 pending 浮到最前
    items.sort(key=lambda x: (x.get("date", ""), x.get("id", 0)), reverse=True)
    items.sort(key=lambda x: x.get("status") != "pending")
    return items

def gather_data():
    return {
        "constraints": parse_constraints(),
        "subsystems": [parse_subsystem(n) for n in ["研究工作", "学习成长", "复盘进化"]],
        "rules": parse_rules(),
        "reviews": parse_reviews(),
        "diary": parse_diary(),
        "kb": parse_kb(),
        "jianwen": parse_jianwen(),
        "candidates": parse_candidates(),
        "today": A.today_str(),
    }

# ── AI 调用（非交互版，复用 assistant 的 call_llm/context）─────
def _cfg():
    return A.load_config()

def ai_checkup():
    cfg = _cfg()
    ctx = A.system_context()
    recent = sorted(REVIEWS.glob("20*.md"))[-4:]
    revs = "\n\n".join(f"### {p.name}\n{A.read_file(p)}" for p in recent)
    out = A.call_llm(cfg, [
        {"role": "system", "content": A.SYSTEM_ROLE},
        {"role": "user", "content": P.checkup(ctx, revs)},
    ])
    return {"text": out}

def ai_pit(user_input):
    if not user_input.strip():
        return {"error": "请先描述你踩的坑。"}
    cfg = _cfg()
    out = A.call_llm(cfg, [
        {"role": "system", "content": A.SYSTEM_ROLE},
        {"role": "user", "content": P.pit(A.read_file(RULES), user_input)},
    ])
    return {"text": out}

def ai_learn(user_input):
    if not user_input.strip():
        return {"error": "请先粘贴要沉淀的内容。"}
    src = A.read_file(user_input) if Path(user_input).exists() else user_input
    cfg = _cfg()
    out = A.call_llm(cfg, [
        {"role": "system", "content": A.SYSTEM_ROLE},
        {"role": "user", "content": P.learn(src)},
    ])
    KB_DIR.mkdir(exist_ok=True)
    path = KB_DIR / f"知识_{A.today_str()}.md"
    path.write_text(out, encoding="utf-8")
    return {"text": out, "saved": str(path)}

def ai_review(verify_text):
    """网页版周复盘：用户填 Verify → AI 做 Reflect/Patch + Plan → 写回文件。"""
    if not verify_text.strip():
        return {"error": "请先填写本周 Verify（上周定的事做到没、没做到一句话原因）。"}
    cfg = _cfg()
    ctx = A.system_context()
    r = A.call_llm(cfg, [
        {"role": "system", "content": A.SYSTEM_ROLE},
        {"role": "user", "content": P.reflect_patch(ctx, verify_text)},
    ])
    plan = A.call_llm(cfg, [
        {"role": "system", "content": A.SYSTEM_ROLE},
        {"role": "user", "content": P.plan(ctx, verify_text, r)},
    ])
    date = A.today_str()
    content = f"""# {date}(AI 辅助复盘)

## 一、Verify
{verify_text}

## 二、Reflect + 三、Patch
{r}

## 四、下周 Plan
{plan}

---
> 本份由可视化看板生成。如产出新规则,请确认后手动加入 规则库.md
"""
    out_path = REVIEWS / f"{date}.md"
    if out_path.exists():
        out_path = REVIEWS / f"{date}_AI.md"
    out_path.write_text(content, encoding="utf-8")
    return {"text": f"【Reflect + Patch】\n{r}\n\n【下周 Plan】\n{plan}", "saved": str(out_path)}

AI_MODES = {"checkup": ai_checkup, "pit": ai_pit, "learn": ai_learn, "review": ai_review}

# ── 模型注册表:网页展示(密钥掩码) + CRUD 派发 ─────────────────
def _mask_key(k):
    k = str(k or "")
    return ("••••" + k[-4:]) if len(k) > 4 else ("••••" if k else "")

def models_payload():
    """给浏览器的模型列表 —— 密钥只回掩码,原始 key 绝不出网。"""
    data = A.load_models()
    out = []
    for m in data["models"]:
        mm = {
            "id": m.get("id"),
            "label": m.get("label", ""),
            "model": m.get("model", ""),
            "base_url": m.get("base_url", ""),
            "temperature": m.get("temperature", "0.4"),
            "key_mask": _mask_key(m.get("api_key", "")),
            "has_key": bool(m.get("api_key")),
        }
        out.append(mm)
    return {"active": data.get("active"), "models": out}

# ── 阅读弹窗 + [[双链]] 本地解析(纯 stdlib)──────────────
# 目的:在 flat 文件阶段就能"点开看 + 双链跳转",把 GBrain 推迟到真正搜不动时。
import re as _re
_WIKI_RE = _re.compile(r"\[\[([^\]]+)\]\]")


def _reader_safe(path_str):
    """阅读弹窗只允许打开项目根目录内的文件,返回 (Path|None, error)。
    相对路径锚定到 ROOT(避免被 server 的 cwd 误解析),再做越界校验。"""
    try:
        p = Path(path_str)
        target = p if p.is_absolute() else (ROOT / p)
        target = target.resolve()
        target.relative_to(ROOT.resolve())  # 越界抛 ValueError
        return target, None
    except (ValueError, RuntimeError, OSError):
        return None, "路径越界,已拒绝。"


def save_file(path_str, content):
    """阅读弹窗的就地保存:复用 _reader_safe 路径防护,拒空(防误清空),写回 UTF-8。
    用字节级写入 + 保留原文件换行风格,避免 Windows 文本模式把 LF 偷偷改成 CRLF。"""
    target, err = _reader_safe(path_str)
    if err:
        return {"error": err}
    if not target.is_file():
        return {"error": "不是文件,已拒绝。"}
    if content is None or not content.strip():
        return {"error": "内容为空,已拒绝(防误清空)。git 基线是后悔药。"}
    try:
        raw = target.read_bytes()
        nl = "\r\n" if b"\r\n" in raw else "\n"
        norm = content.replace("\r\n", "\n").replace("\r", "\n")
        target.write_bytes(norm.replace("\n", nl).encode("utf-8"))
    except Exception as e:
        return {"error": f"写入失败:{e}"}
    return {"ok": True, "path": str(target)}


def resolve_link(label):
    """[[label]] → {path, exists}。R 编号→规则库;日期→周复盘;其余→知识库/复盘按名包含匹配。"""
    label = (label or "").strip()
    if not label:
        return {"path": "", "exists": False}
    if _re.fullmatch(r"R\d+", label):
        return {"path": str(RULES), "exists": RULES.exists()}
    if _re.fullmatch(r"\d{4}-\d{2}-\d{2}", label):
        f = REVIEWS / f"{label}.md"
        return {"path": str(f), "exists": f.exists()}
    cands = []
    if KB_DIR.exists():
        cands += list(KB_DIR.glob("*.md"))
    cands += list(REVIEWS.glob("*.md"))
    for f in cands:
        if label in f.stem:
            return {"path": str(f), "exists": True}
    return {"path": "", "exists": False}


def _md_inline(s):
    s = html.escape(s)
    s = _re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = _re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
    s = _WIKI_RE.sub(
        lambda m: f'<a class="wl" data-link="{html.escape(m.group(1))}">{html.escape(m.group(1))}</a>', s)
    return s


def md_to_html(text):
    """极简 Markdown→HTML(标题/列表/引用/加粗/代码/[[双链]])。纯 stdlib,够看就行。"""
    out, in_ul = [], False
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            if in_ul:
                out.append("</ul>"); in_ul = False
            out.append("")
            continue
        m = _re.match(r"^(#{1,4})\s+(.*)$", line)
        if m:
            if in_ul:
                out.append("</ul>"); in_ul = False
            lvl = len(m.group(1))
            slug = _re.sub(r"[^\w一-鿿]+", "-", m.group(2)).strip("-")
            out.append(f'<h{lvl} id="{html.escape(slug)}">{_md_inline(m.group(2))}</h{lvl}>')
            continue
        if _re.match(r"^\s*[-*]\s+", line):
            if not in_ul:
                out.append("<ul>"); in_ul = True
            out.append("<li>" + _md_inline(line.lstrip().lstrip("-*").strip()) + "</li>")
            continue
        if line.startswith(">"):
            if in_ul:
                out.append("</ul>"); in_ul = False
            out.append(f"<blockquote>{_md_inline(line.lstrip('> ').rstrip())}</blockquote>")
            continue
        if in_ul:
            out.append("</ul>"); in_ul = False
        out.append(f"<p>{_md_inline(line)}</p>")
    if in_ul:
        out.append("</ul>")
    return "\n".join(out)


def read_for_reader(path_str=None, link=None):
    """读一个文件渲染进阅读弹窗。link 优先(解析 [[双链]]);R 编号带锚点。"""
    anchor = ""
    if link:
        r = resolve_link(link)
        if not r["exists"]:
            return {"notfound": True, "label": link}
        path_str = r["path"]
        if _re.fullmatch(r"R\d+", link):
            anchor = link
    target, err = _reader_safe(path_str or "")
    if err:
        return {"error": err}
    if not target.exists() or not target.is_file():
        return {"error": "文件不存在。"}
    try:
        text = target.read_text(encoding="utf-8")
    except Exception as e:
        return {"error": f"读取失败:{e}"}
    title = target.stem
    first = next((l for l in text.splitlines() if l.startswith("# ")), None)
    if first:
        title = first[2:].strip()
    return {
        "title": title,
        "html": md_to_html(text),
        "raw": text,
        "path": str(target),
        "anchor": anchor,
    }

# ── 候选规则暂存区(人保留最终决定权)──────────────────────
CANDIDATES = ROOT / "规则库_候选.md"
_CAND_HEADER_RE = _re.compile(r"^##\s+候选\s+id=(\S+)\s+src=(\S+)\s+time=(\S+)\s+标题=(.*)$")


def _cand_file_init():
    """候选文件不存在时建个带说明的空壳。"""
    if not CANDIDATES.exists():
        CANDIDATES.write_text(
            "# 候选规则暂存区\n\n"
            "> AI 提炼、人未批准的规则草稿。看板里「批准」→ 进规则库;「丢弃」→ 删。\n"
            "> 不靠意志力,但靠人点头 —— 人保留最终决定权。\n\n---\n\n",
            encoding="utf-8")


def parse_candidates():
    """解析候选文件,返回 [{id, source, time, title, body}]。"""
    _cand_file_init()
    out, cur = [], None
    for line in CANDIDATES.read_text(encoding="utf-8").splitlines():
        m = _CAND_HEADER_RE.match(line)
        if m:
            if cur:
                out.append(cur)
            cur = {"id": m.group(1), "source": m.group(2),
                   "time": m.group(3), "title": m.group(4).strip(), "body_lines": []}
        elif cur is not None:
            if line.strip() == "---":
                continue
            cur["body_lines"].append(line)
    if cur:
        out.append(cur)
    for c in out:
        c["body"] = "\n".join(c["body_lines"]).strip()
        del c["body_lines"]
    return out


def _rewrite_candidates(cands):
    """用列表整页重写候选文件。"""
    parts = [
        "# 候选规则暂存区\n\n",
        "> AI 提炼、人未批准的规则草稿。看板里「批准」→ 进规则库;「丢弃」→ 删。\n",
        "> 不靠意志力,但靠人点头 —— 人保留最终决定权。\n\n---\n",
    ]
    for c in cands:
        parts.append(
            f"\n## 候选 id={c['id']} src={c['source']} time={c['time']} 标题={c['title']}\n\n{c['body']}\n")
    CANDIDATES.write_text("".join(parts), encoding="utf-8")


def stage_candidate(title, body, source):
    """把一条候选追加进候选文件。返回 {id}。"""
    _cand_file_init()
    now = datetime.datetime.now()
    cid = now.strftime("%Y%m%d%H%M%S")
    existing = {c["id"] for c in parse_candidates()}
    if cid in existing:
        i = 2
        while f"{cid}-{i}" in existing:
            i += 1
        cid = f"{cid}-{i}"
    time_iso = now.strftime("%Y-%m-%dT%H:%M")
    block = (f"\n## 候选 id={cid} src={source} time={time_iso} 标题={title.strip()}\n\n"
             f"{body.strip()}\n")
    with CANDIDATES.open("a", encoding="utf-8") as f:
        f.write(block)
    return {"id": cid}


def approve_candidate(cid):
    """批准:编 R 号追加进规则库,从候选文件删除。返回 {n}。"""
    cands = parse_candidates()
    target = next((c for c in cands if c["id"] == cid), None)
    if not target:
        return {"error": "候选不存在(可能已被处理)"}
    n = max([int(r["n"]) for r in parse_rules()], default=0) + 1
    new_block = f"## R{n} — {target['title']}\n\n{target['body']}\n"
    text = RULES.read_text(encoding="utf-8")
    marker = "> 下面留空"
    if marker in text:
        sep = text.rfind("\n---\n", 0, text.index(marker))
        if sep >= 0:
            text = text[:sep] + "\n" + new_block + text[sep:]
        else:
            text = text.rstrip() + "\n\n" + new_block
    else:
        text = text.rstrip() + "\n\n" + new_block
    RULES.write_text(text, encoding="utf-8")
    _rewrite_candidates([c for c in cands if c["id"] != cid])
    return {"n": n, "title": target["title"]}


def discard_candidate(cid):
    cands = parse_candidates()
    new = [c for c in cands if c["id"] != cid]
    if len(new) == len(cands):
        return {"error": "候选不存在"}
    _rewrite_candidates(new)
    return {"ok": True}


# ── HTML 渲染（样式 C 温暖个人风）──────────────────────
def esc(s):
    return html.escape(str(s))

def render_page(data):
    cons_rows = ""
    for i, c in enumerate(data["constraints"]):
        cons_rows += (
            f'<div class="cons-row">'
            f'<span class="cons-mark">{"✅" if c["done"] else "⬜"}</span>'
            f'<span class="cons-text">{esc(c["text"])}</span>'
            f'<span class="cons-btns">'
            f'<button class="cons-act" title="编辑" onclick="consEdit({i})">✏️</button>'
            f'<button class="cons-act del" title="删除" onclick="consDel({i},this)">🗑</button>'
            f'</span></div>'
        )
    cons = (cons_rows or '<div class="empty">总纲领里还没写硬约束</div>') \
        + '<div class="cons-add"><button class="cons-add-btn" onclick="consAdd()">+ 添加硬约束</button></div>'

    sub_colors = ["s1", "s2", "s3"]
    sub_labels = ["主业 · 执行层", "滋养 · 执行层", "元层 · 系统的系统"]
    subs = ""
    for i, sub in enumerate(data["subsystems"]):
        todos = sub["todos"]
        sname = esc(sub["name"])
        if todos:
            todo_html = "".join(
                f'<div class="ln"><span class="todo-text">· {esc(t)}</span>'
                f'<span class="cons-btns">'
                f'<button class="cons-act" title="编辑" onclick="event.stopPropagation();todoEdit(\'{sname}\',{i},this)">✏️</button>'
                f'<button class="cons-act del" title="删除" onclick="event.stopPropagation();todoDel(\'{sname}\',{i},this)">🗑</button>'
                f'</span></div>'
                for i, t in enumerate(todos)
            )
        else:
            todo_html = '<div class="ln empty">暂无进行中事项</div>'
        todo_html += (f'<div class="cons-add"><button class="cons-add-btn sm" '
                      f'onclick="event.stopPropagation();todoAdd(\'{sname}\')">+ 添加事项</button></div>')
        subs += f'''
        <div class="sub {sub_colors[i]}" data-path="{esc(sub["path"])}" title="点击打开该子系统文件">
          <h3>{esc(sub["name"])}</h3><div class="lab">{sub_labels[i]}</div>
          <div class="duty">{esc(sub["duty"][:48])}</div>
          <div class="todos-title">进行中事项 · {len(todos)}</div>
          {todo_html}
        </div>'''

    rules = "".join(
        f'<div class="rule-row" onclick="ruleToggle({i})">'
        f'<span class="rid">R{esc(r["n"])}</span>'
        f'<span class="rule-text">{esc(r["title"])}</span>'
        f'<span class="rarrow">▸</span>'
        f'<span class="cons-btns">'
        f'<button class="cons-act" title="编辑" onclick="event.stopPropagation();ruleEdit({i})">✏️</button>'
        f'<button class="cons-act del" title="删除" onclick="event.stopPropagation();ruleDel({i},this)">🗑</button>'
        f'</span></div>'
        f'<div class="rule-detail" id="rd-{i}">{md_to_html(r["body"])}</div>'
        for i, r in enumerate(data["rules"])
    ) or '<div class="empty">规则库还没有规则</div>'
    rules += '<div class="cons-add"><button class="cons-add-btn" onclick="ruleAdd()">+ 添加规则</button></div>'

    reviews = "".join(
        f'<div class="note" data-path="{esc(rv["path"])}">{esc(rv["name"])}'
        f'<span class="when">{"最新" if i == 0 else ""}</span></div>'
        for i, rv in enumerate(data["reviews"])
    ) or '<div class="empty">还没有复盘记录</div>'

    kb_rows = "".join(
        f'<div class="note" data-read="{esc(k["path"])}">{esc(k["name"])}</div>'
        for k in data["kb"]
    )
    kb = (
        '<input id="kb-search" class="kb-search" placeholder="🔍 搜索知识库标题…（支持 [[双链]] 笔记跳转）"'
        ' oninput="filterKB()">'
        f'<div id="kb-list">{kb_rows}</div>'
    ) if kb_rows else '<div class="empty">知识库还是空的 — 用下方「沉淀知识」开始填</div>'

    # GBrain 绊线：知识库达阈值才显示，否则整块隐藏
    n_kb = len(data["kb"])
    if n_kb >= GBRAIN_THRESHOLD:
        tripwire = (
            f'<div class="tripwire">📈 知识库已达 <b>{n_kb}</b> 篇（阈值 {GBRAIN_THRESHOLD}）。'
            f'flat 文件可能开始搜不动了 — 可考虑启动 GBrain，见项目根目录 '
            f'<b>GBrain启动预案.md</b>。数据早已用 [[双链]] 预埋，一键 import 即可。</div>'
        )
    else:
        tripwire = ""

    # 今日见闻池：待筛选的条目带「归档/丢弃」按钮，已处理的灰显
    jw_items = data["jianwen"]
    pending = [it for it in jw_items if it.get("status") == "pending"]
    done = [it for it in jw_items if it.get("status") != "pending"]
    type_label = {"video": "🎬 视频", "inspiration": "💡 灵感"}
    today = data["today"]
    jw_rows = ""
    for it in pending:
        tl = type_label.get(it["type"], it["type"])
        summary = esc(it.get("summary", "")[:280])
        url = esc(it.get("source_url", ""))
        url_html = f'<a class="jw-url" href="{url}" target="_blank">原链接 ↗</a>' if url else ""
        dt = esc(it.get("date", ""))
        # 非今天的条目标日期徽章,提醒这是历史欠账
        badge = (f'<span class="jw-date">{dt[5:]}</span>' if dt and dt != today else "")
        dom_id = esc(f'{it.get("date","")}-{it["id"]}')
        jw_rows += f'''
        <div class="jw-item" id="jw-{dom_id}">
        <div class="jw-head">
            <span class="jw-type">{tl}</span>
            <span class="jw-title">{esc(it["title"])}</span>
            {badge}
            <span class="jw-time">{esc(it.get("time",""))}</span>
          </div>
        <div class="jw-summary">{summary}{"…" if len(it.get("summary",""))>280 else ""}</div>
        <div class="jw-foot">
        {url_html}
        <div class="jw-btns">
        <button class="jw-arch" onclick="jwAct('archive',{it["id"]},'{dt}')">📥 归档进知识库</button>
        <button class="jw-disc" onclick="jwAct('discard',{it["id"]},'{dt}')">🗑 丢弃</button>
            </div>
          </div>
        </div>'''
    if not pending:
        jw_rows = '<div class="empty">见闻池没有待筛选的条目 — 通过聊天机器人发视频链接或灵感给我，会自动汇集到这里</div>'
    done_html = ""
    if done:
        done_rows = "".join(
            f'<div class="jw-done">{("✅" if d["status"]=="archived" else "🗑")} '
            f'<span class="jw-done-date">{esc(d.get("date","")[5:])}</span> '
            f'{type_label.get(d["type"], d["type"])} · {esc(d["title"])}</div>'
            for d in done
        )
        done_html = f'<div class="jw-done-wrap"><div class="jw-done-title">已处理 {len(done)} 条</div>{done_rows}</div>'

    # 候选规则:有人时才渲染整块卡片
    cands = data["candidates"]
    if cands:
        cand_rows = "".join(
            f'''<div class="cand-item" id="cand-{esc(c["id"])}">
          <div class="cand-head">
            <span class="cand-src">{esc({"踩坑":"⚠️ 踩坑","周复盘":"📅 周复盘","手动":"✍️ 手动"}.get(c["source"], c["source"]))}</span>
            <span class="cand-title">{esc(c["title"])}</span>
            <span class="cand-time">{esc(c["time"])}</span>
          </div>
          <pre class="cand-body">{esc(c["body"][:600])}{"…" if len(c["body"])>600 else ""}</pre>
          <div class="cand-btns">
            <button class="jw-arch" onclick="candAct('approve','{esc(c["id"])}',this)">✅ 批准进规则库</button>
            <button class="jw-disc" onclick="candAct('discard','{esc(c["id"])}')">🗑 丢弃</button>
          </div>
        </div>''' for c in cands)
        cand_card = (
            '<div class="card cand-card">'
            f'<h2>📝 候选规则 · 待你点头 {len(cands)} 条</h2>'
            '<div class="demo-note">AI 提炼的草稿。批准 → 自动编号进规则库(R4 起);丢弃 → 删。人不点头不进库。</div>'
            f'{cand_rows}</div>'
        )
    else:
        cand_card = ""

    # 每日日记(纯文本追加 + 折叠看当天全文 + 本周 AI 整理)
    diary = data.get("diary", [])
    if diary:
        drows = "".join(
            f'''<div class="rule-row diary-row" onclick="diaryToggle({i})">
          <span class="rid">📅 {esc(d["short"])}</span>
          <span class="rule-text">{esc(d["count"])} 条 · {esc(d["preview"])}{"…" if len(d["preview"]) >= 40 else ""}</span>
          <span class="rarrow">▸</span>
        </div>
        <div class="rule-detail" id="dd-{i}">{md_to_html(d["body"])}</div>'''
            for i, d in enumerate(diary))
        diary_empty = ""
    else:
        drows = ""
        diary_empty = '<div class="empty">还没记过 — 写一句,点「记一笔」开始。不打卡、不评分,流水而已。</div>'
    diary_html = (
        '<div class="diary-box">'
        '<div class="diary-input-row">'
        '<textarea id="diary-input" placeholder="今天想记点什么…(做了什么/收获/想法,一句话也行)"></textarea>'
        '<button class="diary-add-btn" onclick="diarySubmit()">✏ 记一笔</button>'
        '</div>'
        '<button class="model-btn diary-digest-btn" onclick="diaryDigest()">🔍 用 AI 整理本周日记</button>'
        f'{diary_empty}{drows}'
        '</div>'
    )

    # 当前 active 模型(供顶栏按钮 + AI 卡片标题动态显示)
    try:
        am = A.active_model() or {}
        active_label = am.get("label") or am.get("model") or "未配置"
    except Exception:
        active_label = "未配置"

    return PAGE.replace("{{TODAY}}", esc(data["today"])) \
               .replace("{{TRIPWIRE}}", tripwire) \
               .replace("{{CONSTRAINTS}}", cons) \
               .replace("{{SUBS}}", subs) \
               .replace("{{RULES}}", rules) \
               .replace("{{REVIEWS}}", reviews) \
               .replace("{{DIARY_CARD}}", diary_html) \
               .replace("{{KB}}", kb) \
               .replace("{{JIANWEN}}", jw_rows) \
               .replace("{{JIANWEN_DONE}}", done_html) \
               .replace("{{NJW}}", str(len(pending))) \
               .replace("{{NREVIEW}}", str(len(data["reviews"]))) \
               .replace("{{NRULE}}", str(len(data["rules"]))) \
               .replace("{{NKB}}", str(len(data["kb"]))) \
               .replace("{{CAND_CARD}}", cand_card) \
               .replace("{{ACTIVE_MODEL}}", esc(active_label))

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>个人总体设计部 · 看板</title>
<style>
  :root{
    --bg:#f4ece1; --card:#fffaf3; --line:#e8dccb; --txt:#4a3f35; --txt-dim:#857565;
    --terra:#c2693f; --terra-soft:#f6e3d6; --olive:#7a8450; --olive-soft:#eaeede;
    --plum:#9c6b8a; --plum-soft:#f1e3ee; --gold:#c99a3f;
    --num:"Helvetica Neue","Segoe UI","PingFang SC","Microsoft YaHei",system-ui,sans-serif;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--txt);
    font-family:"Georgia","Songti SC","Microsoft YaHei",serif;
    font-variant-numeric:lining-nums tabular-nums;
    padding:34px 44px;line-height:1.65;max-width:1180px;margin:0 auto}
  header{text-align:center;margin-bottom:14px}
  header h1{font-size:28px;font-weight:700;letter-spacing:2px}
  header .sub{color:var(--txt-dim);font-size:14px;margin-top:6px;font-style:italic}
  header .date{margin-top:10px;font-size:14px;color:var(--terra)}
  .topbar{display:flex;justify-content:center;gap:10px;margin-bottom:24px;flex-wrap:wrap}
  .chip{background:var(--card);border:1px solid var(--line);border-radius:20px;
    padding:6px 16px;font-size:14px}
  .chip b{color:var(--terra);font-family:var(--num)}
  .refresh{cursor:pointer;background:var(--terra-soft);border:1px solid #e6c4ab;
    border-radius:20px;padding:6px 16px;font-size:13px;color:var(--terra);font-family:inherit}
  .refresh:hover{background:var(--terra);color:#fff}
  .card{background:var(--card);border:1px solid var(--line);border-radius:20px;
    padding:24px 28px;margin-bottom:22px;box-shadow:0 2px 12px rgba(160,120,80,.06)}
  .card h2{font-size:17px;font-weight:700;margin-bottom:16px;color:var(--terra);
    display:flex;align-items:center;gap:9px}
  .card h2::before{content:"";width:10px;height:10px;border-radius:50%;background:var(--terra)}
  .loop{display:flex;align-items:center;justify-content:center;gap:4px;flex-wrap:wrap}
  .bead{padding:13px 20px;border-radius:24px;background:var(--olive-soft);
    color:var(--olive);font-size:15px;font-weight:700}
  .bead.meta{background:var(--plum-soft);color:var(--plum)}
  .link{color:var(--gold);font-size:18px}
  .loop-note{text-align:center;color:var(--txt-dim);font-size:14px;margin-top:14px;font-style:italic}
  .subs{display:grid;grid-template-columns:repeat(3,1fr);gap:20px;margin-bottom:22px}
  .sub{background:var(--card);border:1px solid var(--line);border-radius:20px;padding:22px;
    box-shadow:0 2px 12px rgba(160,120,80,.06);cursor:pointer;transition:.15s}
  .sub:hover{transform:translateY(-3px);box-shadow:0 8px 20px rgba(160,120,80,.14)}
  .sub h3{font-size:18px;font-weight:700;margin-bottom:3px}
  .sub .lab{font-size:13px;color:var(--txt-dim);margin-bottom:10px;font-style:italic}
  .sub .duty{font-size:13px;color:var(--txt-dim);margin-bottom:14px;min-height:34px;line-height:1.4}
  .sub.s1 h3{color:var(--terra)} .sub.s2 h3{color:var(--olive)} .sub.s3 h3{color:var(--plum)}
  .todos-title{font-size:14px;font-weight:700;border-top:1px dotted var(--line);padding-top:10px;margin-bottom:6px}
  .ln{font-size:14px;padding:3px 0;display:flex;align-items:center;gap:8px}
  .ln .todo-text{flex:1}
  .ln.empty,.empty{color:var(--txt-dim);font-style:italic;font-size:14px}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:22px;margin-bottom:22px}
  .note{padding:10px 0;border-bottom:1px dashed var(--line);font-size:14px;cursor:pointer;display:flex;align-items:center;gap:8px}
  .note:last-child{border:none}
  .note:hover{color:var(--terra)}
  .note .rid{color:var(--gold);font-weight:700;margin-right:8px;font-family:var(--num)}
  .note .rule-text{flex:1;min-width:0}
  .note .when{margin-left:auto;color:var(--terra);font-size:13px;font-style:italic}
  .note .cons-btns,.ln .cons-btns{opacity:0;transition:.15s;margin-left:auto;flex-shrink:0}
  .note:hover .cons-btns,.ln:hover .cons-btns{opacity:1}
  .rule-row{padding:10px 0;border-bottom:1px dashed var(--line);font-size:14px;cursor:pointer;display:flex;align-items:center;gap:8px}
  .rule-row:hover{color:var(--terra)}
  .rule-row .rid{color:var(--gold);font-weight:700;margin-right:8px;font-family:var(--num)}
  .rule-row .rule-text{flex:1;min-width:0}
  .rule-row .rarrow{color:var(--txt-dim);font-size:12px;transition:transform .15s}
  .rule-row.open .rarrow{transform:rotate(90deg)}
  .rule-row .cons-btns{opacity:0;transition:opacity .15s;margin-left:auto;flex-shrink:0}
  .rule-row:hover .cons-btns{opacity:1}
  .rule-detail{display:none;padding:6px 0 10px 36px;color:var(--txt-dim);font-size:13px;line-height:1.7}
  .rule-detail.show{display:block}
  .rule-detail ul{margin:4px 0;padding-left:18px}
  .rule-detail li{margin:2px 0}
  .diary-box{display:flex;flex-direction:column;gap:14px}
  .diary-input-row{display:flex;gap:10px;align-items:stretch}
  .diary-input-row textarea{flex:1;min-height:54px;border:1px solid var(--terra-soft);border-radius:12px;
    padding:10px 12px;font-family:inherit;font-size:14px;resize:vertical;background:#fffdf9;color:var(--txt);
    transition:border-color .16s,box-shadow .16s}
  .diary-input-row textarea:focus{outline:none;border-color:var(--terra);box-shadow:0 0 0 3px rgba(194,105,63,.15)}
  .diary-add-btn{align-self:stretch;white-space:nowrap;font-family:inherit;font-size:15px;font-weight:700;
    color:#fff;background:linear-gradient(135deg,#d6794a 0%,var(--terra) 55%,#b85c2c 100%);
    border:none;border-radius:14px;padding:0 26px;cursor:pointer;
    display:inline-flex;align-items:center;gap:6px;box-shadow:0 4px 12px rgba(194,105,63,.28);
    transition:transform .16s,box-shadow .16s,filter .16s}
  .diary-add-btn:hover{transform:translateY(-2px);box-shadow:0 8px 20px rgba(194,105,63,.38);filter:brightness(1.05)}
  .diary-add-btn:active{transform:translateY(0);box-shadow:0 3px 8px rgba(194,105,63,.26)}
  .diary-add-btn:disabled{opacity:.55;cursor:wait;transform:none}
  .diary-digest-btn{align-self:flex-start}
  .cons-add-btn.sm{font-size:13px;padding:4px 12px}
  .actions{display:grid;grid-template-columns:repeat(2,1fr);gap:16px}
  .btn{background:var(--terra-soft);border:1px solid #e6c4ab;border-radius:16px;
    padding:18px 20px;cursor:pointer;transition:.18s;text-align:left;font-family:inherit}
  .btn:hover{background:var(--terra);color:#fff;transform:translateY(-2px);
    box-shadow:0 6px 18px rgba(194,105,63,.25)}
  .btn .ti{font-size:15px;font-weight:700;color:var(--terra)}
  .btn:hover .ti{color:#fff}
  .btn .de{font-size:13px;color:var(--txt-dim);margin-top:3px}
  .btn:hover .de{color:#fbe8dd}
  footer{margin-top:24px;text-align:center;color:var(--txt-dim);font-size:13px;font-style:italic}
  /* 弹窗 */
  .mask{position:fixed;inset:0;background:rgba(74,63,53,.45);display:none;
    align-items:center;justify-content:center;z-index:50;padding:24px}
  .mask.on{display:flex}
  .modal{background:var(--card);border-radius:20px;max-width:760px;width:100%;
    max-height:85vh;overflow:auto;padding:28px 30px;box-shadow:0 20px 60px rgba(74,63,53,.3)}
  .modal h3{color:var(--terra);font-size:18px;margin-bottom:14px}
  .modal textarea{width:100%;min-height:120px;border:1px solid var(--line);border-radius:12px;
    padding:12px;font-family:inherit;font-size:14px;resize:vertical;background:#fffdf9;color:var(--txt)}
  .modal .out{white-space:pre-wrap;background:#fbf5ec;border:1px solid var(--line);
    border-radius:12px;padding:16px;margin-top:14px;font-size:14px;line-height:1.7;
    font-family:"Microsoft YaHei",sans-serif}
  .modal .saved{color:var(--olive);font-size:13px;margin-top:8px;font-style:italic}
  .modal .mbtns{display:flex;gap:12px;margin-top:16px;justify-content:flex-end}
  .modal button{font-family:inherit;border-radius:12px;padding:10px 22px;cursor:pointer;font-size:14px}
  .run{background:var(--terra);color:#fff;border:none}
  .run:hover{background:#a9542f}
  .run:disabled{opacity:.5;cursor:wait}
  .close{background:transparent;border:1px solid var(--line);color:var(--txt-dim)}
  .spinner{color:var(--terra);font-style:italic;margin-top:14px}
  .demo-note{background:var(--olive-soft);color:var(--olive);border-radius:12px;
    padding:8px 14px;font-size:13px;text-align:center;margin-bottom:18px}
  .tripwire{background:#fbeede;border:1px solid var(--gold);color:#8a6512;border-radius:12px;
    padding:12px 16px;font-size:14px;text-align:center;margin-bottom:18px;line-height:1.6}
  .tripwire b{color:var(--terra)}
  /* 模型管理 */
  .model-btn{cursor:pointer;background:var(--olive-soft);border:1px solid #c5cdb0;
    border-radius:20px;padding:6px 16px;font-size:13px;color:var(--olive);font-family:inherit}
  .model-btn:hover{background:var(--olive);color:#fff}
  .model-row{display:flex;align-items:center;justify-content:space-between;gap:12px;
    background:#fffdf9;border:1px solid var(--line);border-radius:14px;padding:12px 16px;margin-bottom:10px}
  .model-row-on{border-color:var(--olive);background:var(--olive-soft)}
  .model-row-main{flex:1;min-width:0}
  .model-row-label{font-weight:700;font-size:15px;color:var(--txt)}
  .model-row-meta{font-size:12px;color:var(--txt-dim);margin-top:2px;
    overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .model-row-key{font-size:12px;color:var(--txt-dim);margin-top:3px;font-family:var(--num)}
  .model-row-btns{display:flex;align-items:center;gap:8px;flex-shrink:0}
  .model-on{font-size:12px;color:var(--olive);font-weight:700}
  .model-act,.model-edit,.model-del{font-family:inherit;font-size:12px;border-radius:10px;
    padding:6px 12px;cursor:pointer;border:1px solid var(--line);background:var(--card);color:var(--txt-dim)}
  .model-act{background:var(--terra-soft);border-color:#e6c4ab;color:var(--terra)}
  .model-act:hover{background:var(--terra);color:#fff}
  .model-edit:hover{color:var(--txt)}
  .model-del{color:#b06a52}
  .model-del:hover{background:#fbeede;border-color:#e8bda8}
  .model-add-bar{margin-top:6px}
  .model-add-btn{font-family:inherit;font-size:13px;border:1px dashed var(--olive);
    background:transparent;color:var(--olive);border-radius:12px;padding:8px 16px;cursor:pointer;width:100%}
  .model-add-btn:hover{background:var(--olive-soft)}
  .model-field{display:flex;flex-direction:column;gap:4px;margin-bottom:10px}
  .model-field label{font-size:12px;color:var(--txt-dim)}
  .model-field input{width:100%;border:1px solid var(--line);border-radius:10px;
    padding:9px 12px;font-family:inherit;font-size:14px;background:#fffdf9;color:var(--txt)}
  .model-field input:focus{outline:none;border-color:var(--terra)}
  /* 今日见闻 */
  .jw-item{background:var(--card);border:1px solid var(--line);border-radius:16px;
    padding:16px 20px;margin-bottom:12px;transition:.12s}
  .jw-item:hover{border-color:var(--terra);box-shadow:0 4px 14px rgba(160,120,80,.10)}
  .jw-head{display:flex;align-items:center;gap:10px;margin-bottom:8px}
  .jw-type{font-size:12px;color:var(--terra);background:var(--terra-soft);
    padding:2px 10px;border-radius:10px;font-weight:700}
  .jw-title{font-size:15px;font-weight:700;color:var(--txt)}
  .jw-time{margin-left:auto;font-size:12px;color:var(--txt-dim)}
  .jw-date{font-size:12px;color:#b87a1e;background:#fbeede;border:1px solid #e6c4ab;
    padding:1px 8px;border-radius:10px;font-weight:600}
  .jw-done-date{font-size:12px;color:var(--gold)}
  .jw-summary{font-size:13px;color:var(--txt-dim);line-height:1.6;
    max-height:80px;overflow:hidden;margin-bottom:10px}
  .jw-foot{display:flex;align-items:center;gap:10px}
  .jw-url{font-size:12px;color:var(--gold);text-decoration:none}
  .jw-url:hover{text-decoration:underline}
  .jw-btns{display:flex;gap:8px}
  .jw-arch{background:var(--olive-soft);color:var(--olive);border:1px solid #c2caa8;
    border-radius:10px;padding:6px 14px;cursor:pointer;font-size:13px;font-family:inherit}
  .jw-arch:hover{background:var(--olive);color:#fff}
  .jw-disc{background:#f5f0eb;color:var(--txt-dim);border:1px solid var(--line);
    border-radius:10px;padding:6px 14px;cursor:pointer;font-size:13px;font-family:inherit}
  .jw-disc:hover{background:#e5ddd2;color:var(--terra)}
  .jw-done-wrap{background:var(--card);border:1px solid var(--line);
    border-radius:14px;padding:14px 18px;margin-top:10px}
  .jw-done-title{font-size:13px;color:var(--txt-dim);margin-bottom:8px}
  .jw-done{font-size:13px;color:var(--txt-dim);padding:3px 0}
  /* 知识库搜索框 */
  .kb-search{width:100%;border:1px solid var(--line);border-radius:12px;
    padding:10px 14px;font-family:inherit;font-size:14px;margin-bottom:12px;background:#fffdf9;color:var(--txt)}
  .kb-search:focus{outline:none;border-color:var(--terra)}
  /* 阅读弹窗 + 双链 */
  .reader .reader-bar{display:flex;align-items:center;gap:8px;margin-bottom:12px}
  .reader .reader-body{font-size:14px;line-height:1.8;color:var(--txt);
    font-family:"Microsoft YaHei",sans-serif}
  .reader .reader-body h1,.reader .reader-body h2,.reader .reader-body h3{color:var(--terra);margin:18px 0 8px}
  .reader .reader-body p{margin:8px 0}
  .reader .reader-body ul{margin:8px 0 8px 22px}
  .reader .reader-body blockquote{border-left:3px solid var(--gold);padding-left:12px;
    color:var(--txt-dim);font-style:italic;margin:8px 0}
  .reader .reader-body code{background:var(--terra-soft);padding:1px 5px;border-radius:4px;font-size:13px}
  .wl{color:var(--olive);text-decoration:none;border-bottom:1px dotted var(--olive);cursor:pointer}
  .wl:hover{color:var(--terra);border-bottom-color:var(--terra)}
  .reader .r-back{background:transparent;border:1px solid var(--line);color:var(--txt-dim)}
  .reader .r-edit{background:var(--olive-soft);color:var(--olive);border:1px solid #c2caa8}
  .reader .r-edit:hover{background:var(--olive);color:#fff}
  .reader .stub{background:var(--terra-soft);border:1px solid #e6c4ab;color:var(--terra);
    border-radius:12px;padding:14px;font-size:14px}
  /* 候选规则暂存区 */
  .cand-card{border-color:var(--gold)}
  .cand-item{background:#fffdf9;border:1px solid var(--line);border-radius:14px;
    padding:12px 14px;margin-top:10px}
  .cand-item:hover{border-color:var(--gold);box-shadow:0 4px 12px rgba(190,150,70,.10)}
  .cand-head{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:6px}
  .cand-src{font-size:12px;background:var(--terra-soft);color:var(--terra);
    padding:2px 8px;border-radius:10px;white-space:nowrap}
  .cand-title{font-weight:600;color:var(--txt);flex:1;min-width:160px}
  .cand-time{font-size:12px;color:var(--txt-dim);white-space:nowrap}
  .cand-body{margin:6px 0 10px;font-size:13px;line-height:1.7;color:var(--txt-dim);
    white-space:pre-wrap;font-family:"Microsoft YaHei",sans-serif;max-height:220px;
    overflow:auto;background:#faf7f0;border-radius:10px;padding:10px 12px;border:1px solid var(--line)}
  .cand-btns{display:flex;gap:8px;flex-wrap:wrap}
  /* 弹窗里的候选编辑行 */
  .cand-edit{margin-top:12px;border-top:1px dashed var(--line);padding-top:12px}
  .cand-title-in{width:100%;border:1px solid var(--line);border-radius:12px;
    padding:10px 14px;font-family:inherit;font-size:14px;background:#fffdf9;color:var(--txt)}
  .cand-title-in:focus{outline:none;border-color:var(--gold)}
  .cand-hint{font-size:13px;color:var(--txt-dim);margin:8px 0 10px;line-height:1.6}
  /* 硬约束:每条带编辑/删除小按钮 */
  .cons-row{display:flex;align-items:center;gap:10px;padding:9px 0;border-bottom:1px dashed var(--line);font-size:14px}
  .cons-row:last-of-type{border:none}
  .cons-mark{font-size:15px;width:18px;text-align:center}
  .cons-text{flex:1;line-height:1.5}
  .cons-btns{display:flex;gap:4px;opacity:.55;transition:.15s}
  .cons-row:hover .cons-btns{opacity:1}
  .cons-act{background:transparent;border:none;cursor:pointer;font-size:14px;color:var(--txt-dim);
    padding:2px 7px;border-radius:8px;font-family:inherit}
  .cons-act:hover{background:var(--terra-soft);color:var(--terra)}
  .cons-act.del:hover{background:#fbeede;color:#b8552e}
  .cons-add{margin-top:14px;text-align:center}
  .cons-add-btn{background:var(--olive-soft);color:var(--olive);border:1px dashed #c2caa8;
    border-radius:12px;padding:9px 22px;cursor:pointer;font-family:inherit;font-size:14px;transition:.15s}
  .cons-add-btn:hover{background:var(--olive);color:#fff}
  /* 阅读弹窗的就地编辑器 */
  .reader-edit-area{width:100%;min-height:56vh;border:1px solid var(--line);border-radius:12px;
    padding:14px;font-family:"SFMono-Regular",Consolas,"Microsoft YaHei",monospace;font-size:13px;
    line-height:1.6;resize:vertical;background:#fffdf9;color:var(--txt);white-space:pre;overflow:auto}
  .reader .r-saved{color:var(--olive);font-size:13px;font-style:italic;margin-top:8px;text-align:right}
  /* 硬约束小弹窗 */
  .cons-done-lab{display:flex;align-items:center;gap:8px;font-size:14px;color:var(--txt-dim);margin:10px 0}
  .cons-done-lab input{width:auto}
  /* 轻量提示(替代原生 alert) + 行内二次确认(替代原生 confirm) */
  #toast{position:fixed;left:50%;bottom:30px;transform:translateX(-50%) translateY(16px);
    background:var(--txt);color:var(--bg);padding:11px 20px;border-radius:14px;font-size:13px;
    line-height:1.4;opacity:0;pointer-events:none;transition:opacity .2s,transform .2s;
    z-index:200;box-shadow:0 10px 28px rgba(74,63,53,.28);max-width:82vw;text-align:center}
  #toast.on{opacity:1;transform:translateX(-50%) translateY(0)}
  .arming{box-shadow:0 0 0 2px var(--gold) inset;cursor:pointer}
  .cons-act.arming{background:#fbeede;color:#b8552e;opacity:1}
  @media(max-width:768px){
    body{padding:18px 14px 40px}
    header h1{font-size:23px;letter-spacing:1px}
    .topbar{gap:8px}
    .card,.sub{padding:18px}
    .btn{padding:14px 16px}
    .subs{grid-template-columns:1fr}
    .grid2{grid-template-columns:1fr}
    .actions{grid-template-columns:1fr}
    .run,.close,.jw-arch,.jw-disc,.cons-add-btn,.chip,.refresh,.model-btn{min-height:36px}
    .modal{padding:20px 18px;margin:0 4px}
    .model-row{flex-direction:column;align-items:stretch}
    .model-row-btns{flex-wrap:wrap}
  }
</style></head>
<body>
  <header>
    <h1>个人总体设计部</h1>
    <div class="sub">把人生当作一个系统来设计 · 钱学森系统工程</div>
    <div class="date">{{TODAY}}</div>
  </header>
  <div class="demo-note">读取的是你的真实文件 · 点子系统/规则/复盘条目可打开原文 · 当前模型:{{ACTIVE_MODEL}}</div>
  {{TRIPWIRE}}
  <div class="topbar">
    <span class="chip">累计复盘 <b>{{NREVIEW}}</b></span>
    <span class="chip">规则库 <b>{{NRULE}}</b> 条</span>
    <span class="chip">知识库 <b>{{NKB}}</b> 篇</span>
    <span class="chip">今日见闻 <b>{{NJW}}</b> 条</span>
    <button class="model-btn" onclick="openModel()">⚙ 模型 · {{ACTIVE_MODEL}}</button>
    <button class="refresh" onclick="location.reload()">↻ 刷新</button>
  </div>

  <div class="card">
    <h2>核心闭环</h2>
    <div class="loop">
      <span class="bead">计划</span><span class="link">→</span>
      <span class="bead">执行</span><span class="link">→</span>
      <span class="bead">验证</span><span class="link">→</span>
      <span class="bead meta">反思</span><span class="link">→</span>
      <span class="bead meta">修补</span>
    </div>
    <div class="loop-note">每周一次,温柔地问自己:做到了吗?为什么?下次怎么调?</div>
  </div>

  <div class="subs">{{SUBS}}</div>

  <div class="card">
    <h2>硬约束 · 不靠意志力靠规则锁死</h2>
    {{CONSTRAINTS}}
  </div>

  <div class="grid2">
    <div class="card">
      <h2>规则库 · 长出来的智慧</h2>
      {{RULES}}
    </div>
    <div class="card">
      <h2>复盘足迹</h2>
      {{REVIEWS}}
    </div>
  </div>

  <div class="card">
    <h2>知识库</h2>
    {{KB}}
  </div>

  {{CAND_CARD}}

  <div class="card">
    <h2>📓 每日日记 · 原始流水(只追加,不提炼)</h2>
    {{DIARY_CARD}}
  </div>

  <div class="card">
    <h2>📥 今日见闻池 · 待筛选 {{NJW}} 条</h2>
    {{JIANWEN}}
    {{JIANWEN_DONE}}
  </div>

  <div class="card">
    <h2>请 AI 帮我一把 · 当前:{{ACTIVE_MODEL}}</h2>
    <div class="actions">
      <button class="btn" onclick="openAI('review')"><div class="ti">🌱 本周复盘</div><div class="de">填 Verify → AI 做反思+下周计划,自动写回</div></button>
      <button class="btn" onclick="openAI('pit')"><div class="ti">🪨 踩坑记一笔</div><div class="de">描述坑 → AI 判断要不要入规则库</div></button>
      <button class="btn" onclick="openAI('learn')"><div class="ti">📖 沉淀知识</div><div class="de">粘一段文字/文件路径 → 结构化存知识库</div></button>
      <button class="btn" onclick="openAI('checkup')"><div class="ti">🩺 月度体检</div><div class="de">无需输入 → AI 看系统该保持/解封/简化</div></button>
    </div>
  </div>

  <footer>本地看板 · 数据实时读自项目文件 · 样式 C 温暖个人风</footer>

  <div class="mask" id="mask">
    <div class="modal">
      <h3 id="m-title"></h3>
      <div id="m-inputwrap"><textarea id="m-input" placeholder=""></textarea></div>
      <div class="mbtns">
        <button class="close" onclick="closeAI()">关闭</button>
        <button class="run" id="m-run" onclick="runAI()">运行</button>
        <button class="run" id="m-tocand" onclick="toCand()" style="display:none;background:var(--c2)">📝 存为候选规则</button>
      </div>
      <div id="m-spin" class="spinner" style="display:none">AI 思考中… 首次响应可能要十几秒，请稍候。</div>
      <div id="m-out" class="out" style="display:none"></div>
      <div id="m-saved" class="saved"></div>
      <div id="m-cand" class="cand-edit" style="display:none">
        <input id="m-cand-title" class="cand-title-in" placeholder="一句话规则标题（将作为规则库 R 号的标题）">
        <div class="cand-hint">正文取上方 AI 输出。批准时自动编号（R4 起）写入规则库；丢弃可随时删。人不点头不进库。</div>
        <div class="mbtns">
          <button class="close" onclick="cancelCand()">取消</button>
          <button class="run" onclick="submitCand()">提交候选</button>
        </div>
      </div>
    </div>
  </div>

  <div class="mask" id="reader-mask">
    <div class="modal reader">
      <div class="reader-bar">
        <button class="close r-back" id="r-back" onclick="readerBack()" style="display:none">‹ 返回</button>
        <button class="close" onclick="closeReader()">关闭</button>
        <button class="run r-edit" id="r-edit-btn" onclick="readerEditOn()" style="display:none">✏️ 编辑</button>
      </div>
      <h3 id="r-title"></h3>
      <div id="r-body" class="reader-body"></div>
      <div id="r-edit-wrap" style="display:none">
        <textarea id="r-edit" class="reader-edit-area" spellcheck="false"></textarea>
        <div class="r-saved" id="r-saved" style="display:none">已保存 · 关闭后刷新首页</div>
        <div class="mbtns">
          <button class="close" onclick="readerEditOff()">取消</button>
          <button class="run" id="r-save-btn" onclick="readerSave()">💾 保存</button>
        </div>
      </div>
    </div>
  </div>

  <div class="mask" id="cons-mask">
    <div class="modal" style="max-width:560px">
      <h3 id="cons-title">编辑硬约束</h3>
      <textarea id="cons-input" style="min-height:66px" placeholder="硬约束内容（不商量、不例外、写死）"></textarea>
      <label class="cons-done-lab"><input type="checkbox" id="cons-done"> 已达成 / 勾掉（✅）</label>
      <div class="mbtns">
        <button class="close" onclick="closeCons()">取消</button>
        <button class="run" onclick="saveCons()">保存</button>
      </div>
    </div>
  </div>

  <div class="mask" id="rule-mask">
    <div class="modal" style="max-width:600px">
      <h3 id="rule-title">编辑规则</h3>
      <div style="font-size:12px;color:var(--txt-dim);margin-bottom:8px">整块编辑：第一行是 <b>## R编号 — 标题</b>，下面跟 触发/类型/规则/日期 四个字段。编号留空会自动顺延。</div>
      <textarea id="rule-input" style="min-height:200px;font-family:inherit" placeholder="## R4 — 一句话规则&#10;- 触发:什么坑让我加这条&#10;- 类型:边界乱 / 时间没留 / ...&#10;- 规则:具体怎么做（不靠意志力）&#10;- 日期:YYYY-MM-DD"></textarea>
      <div class="mbtns">
        <button class="close" onclick="closeRule()">取消</button>
        <button class="run" onclick="saveRule()">保存</button>
      </div>
    </div>
  </div>

  <div class="mask" id="todo-mask">
    <div class="modal" style="max-width:520px">
      <h3 id="todo-title">编辑进行中事项</h3>
      <textarea id="todo-input" style="min-height:80px;font-family:inherit" placeholder="一件正在推进的事…"></textarea>
      <div class="mbtns">
        <button class="close" onclick="closeTodo()">取消</button>
        <button class="run" onclick="saveTodo()">保存</button>
      </div>
    </div>
  </div>

  <!-- 模型管理(切换 / 增 / 改 / 删)-->
  <div class="mask" id="model-mask">
    <div class="modal" style="max-width:640px">
      <h3>⚙ 模型管理</h3>
      <div class="demo-note">切换后,网页 + 命令行(assistant.py)一起用这个模型。密钥只存在你本机 tools/models.json(已 gitignore),永远不会传到别处。</div>
      <div id="model-list"></div>
      <div class="model-add-bar"><button class="model-add-btn" onclick="modelForm()">+ 添加模型</button></div>
      <div id="model-form-wrap" style="display:none">
        <h4 id="model-form-title" style="margin:14px 0 8px;font-size:15px">添加模型</h4>
        <div class="model-field"><label>显示名</label><input id="mf-label" placeholder="例:DeepSeek · 主力"></div>
        <div class="model-field"><label>模型名 model</label><input id="mf-model" placeholder="deepseek-chat"></div>
        <div class="model-field"><label>接口地址 base_url</label><input id="mf-base" placeholder="https://api.deepseek.com/v1"></div>
        <div class="model-field"><label>API Key</label><input id="mf-key" type="password" placeholder="sk-…（编辑现有模型留空=不改密钥）"></div>
        <div class="model-field"><label>温度 temperature</label><input id="mf-temp" placeholder="0.4"></div>
        <div class="mbtns">
          <button class="close" onclick="modelFormCancel()">取消</button>
          <button class="run" onclick="modelSave()">保存</button>
        </div>
      </div>
      <div class="mbtns" style="margin-top:14px">
        <button class="close" onclick="closeModel()">关闭</button>
      </div>
    </div>
  </div>

<div id="toast"></div>
<script>
  var META = {
    review:  {title:"🌱 本周复盘", needInput:true,  ph:"逐条写：上周每个子系统定的事，做到没？没做到一句话原因。\n例：研究工作—主线推进了，但学习成长那条没做，因为周中加班。"},
    pit:     {title:"🪨 踩坑记一笔", needInput:true,  ph:"描述你踩的坑（具体场景）。AI 会判断是一次性问题还是该入库的系统问题。"},
    learn:   {title:"📖 沉淀知识", needInput:true,  ph:"粘贴要沉淀的一段文字，或一个文件路径。AI 会结构化后存进知识库。"},
    checkup: {title:"🩺 月度体检", needInput:false, ph:""}
  };
  var curMode = null;
  // —— 交互顺畅度三件套 ——
  // A. 跨刷新保留滚动位置:存/删/批 准后 reload 不再跳回顶部
  function softReload(){
    try{ sessionStorage.setItem('scrollY', String(window.scrollY||0)); }catch(e){}
    location.reload();
  }
  window.addEventListener('load', function(){
    var y=null; try{ y=sessionStorage.getItem('scrollY'); }catch(e){}
    if(y){ try{ sessionStorage.removeItem('scrollY'); }catch(e){} window.scrollTo(0, parseInt(y,10)||0); }
  });
  // C. 轻量提示(替代原生 alert):底部胶囊,3.2s 自动隐
  var __toastT=null;
  function showToast(msg, ms){
    var t=document.getElementById('toast'); if(!t){ return; }
    t.textContent=msg; t.classList.add('on');
    if(__toastT){ clearTimeout(__toastT); }
    __toastT=setTimeout(function(){ t.classList.remove('on'); }, ms||3200);
  }
  // C. 行内二次确认(替代原生 confirm):首次点→武装(变字+金圈,4s);4s 内再点才执行
  function armed(el, label){
    if(!el){ return true; }
    if(el.dataset.arm==='1'){
      el.dataset.arm=''; el.classList.remove('arming');
      if(typeof el._oldTxt==='string'){ el.textContent=el._oldTxt; }
      return true;
    }
    el.dataset.arm='1'; el._oldTxt=el.textContent; el.textContent=label||'确认?';
    el.classList.add('arming');
    var grp=el.closest('.cons-btns'); if(grp){ grp.style.opacity='1'; }
    setTimeout(function(){
      if(el.dataset.arm==='1'){
        el.dataset.arm=''; el.classList.remove('arming');
        if(typeof el._oldTxt==='string'){ el.textContent=el._oldTxt; }
      }
    }, 4000);
    return false;
  }
  // B. 弹窗快捷键(ESC 关闭 / Ctrl+Enter 保存)+ 点遮罩关闭
  function __topMaskId(){
    var ids=['reader-mask','cons-mask','rule-mask','todo-mask','model-mask','mask'];
    for(var i=0;i<ids.length;i++){ var m=document.getElementById(ids[i]); if(m && m.classList.contains('on')){ return ids[i]; } }
    return null;
  }
  function __closeMask(id){
    if(id==='reader-mask'){ closeReader(); }
    else if(id==='cons-mask'){ closeCons(); }
    else if(id==='rule-mask'){ closeRule(); }
    else if(id==='todo-mask'){ closeTodo(); }
    else if(id==='model-mask'){ closeModel(); }
    else if(id==='mask'){ closeAI(); }
  }
  function __saveTopMask(){
    var id=__topMaskId();
    if(id==='reader-mask'){
      if(document.getElementById('r-edit-wrap').style.display!=='none'){ readerSave(); }
    } else if(id==='cons-mask'){ saveCons(); }
    else if(id==='rule-mask'){ saveRule(); }
    else if(id==='todo-mask'){ saveTodo(); }
    else if(id==='model-mask'){ modelSave(); }
    else if(id==='mask'){ runAI(); }
  }
  document.addEventListener('keydown', function(e){
    if(e.key==='Escape'){ var id=__topMaskId(); if(id){ __closeMask(id); } }
    else if((e.ctrlKey||e.metaKey) && e.key==='Enter'){ __saveTopMask(); }
  });
  (function(){
    ['reader-mask','cons-mask','rule-mask','todo-mask','model-mask','mask'].forEach(function(id){
      var m=document.getElementById(id); if(!m){ return; }
      m.addEventListener('click', function(e){ if(e.target===m){ __closeMask(id); } });
    });
  })();
  function openAI(mode){
    curMode = mode; var m = META[mode];
    document.getElementById('m-title').textContent = m.title;
    document.getElementById('m-inputwrap').style.display = m.needInput ? 'block':'none';
    document.getElementById('m-input').value = '';
    document.getElementById('m-input').placeholder = m.ph;
    document.getElementById('m-out').style.display='none';
    document.getElementById('m-out').textContent='';
    document.getElementById('m-saved').textContent='';
    document.getElementById('m-run').disabled=false;
    document.getElementById('m-spin').style.display='none';
    document.getElementById('m-cand').style.display='none';
    document.getElementById('m-tocand').style.display='none';
    document.getElementById('mask').classList.add('on');
  }
  function closeAI(){ document.getElementById('mask').classList.remove('on'); }
  async function runAI(){
    var run=document.getElementById('m-run'), spin=document.getElementById('m-spin');
    var out=document.getElementById('m-out'), saved=document.getElementById('m-saved');
    run.disabled=true; spin.style.display='block'; out.style.display='none'; saved.textContent='';
    document.getElementById('m-cand').style.display='none';
    try{
      var resp=await fetch('/api/ai',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({mode:curMode, input:document.getElementById('m-input').value})});
      var data=await resp.json();
      spin.style.display='none'; out.style.display='block';
      if(data.error){ out.textContent='⚠ '+data.error; }
      else{
        out.textContent=data.text||'(无输出)';
        if(data.saved){ saved.textContent='✅ 已写入：'+data.saved+'（刷新页面可见）'; }
        // 踩坑/复盘/体检 可能产出规则草稿 → 显示「存为候选」(沉淀知识走 KB,不需要)
        if((curMode==='pit'||curMode==='review'||curMode==='checkup') && data.text){
          document.getElementById('m-tocand').style.display='';
        }
      }
    }catch(e){ spin.style.display='none'; out.style.display='block'; out.textContent='⚠ 请求失败：'+e; }
    run.disabled=false;
  }
  // 候选规则:AI 输出 → 存草稿 → 看板里批准/丢弃
  function toCand(){
    var out=document.getElementById('m-out').textContent.trim();
    if(!out){ showToast('先运行 AI 产出内容,再存为候选'); return; }
    // 标题预填:取首行非空、去掉前缀标记
    var firstLine = out.split('\n').map(s=>s.trim()).find(s=>s.length>0) || '';
    firstLine = firstLine.replace(/^#+\s*/,'').replace(/^\*\*|\*\*$/g,'').slice(0,40);
    var ti=document.getElementById('m-cand-title'); ti.value=firstLine;
    document.getElementById('m-cand').style.display='block';
    ti.focus(); ti.select();
  }
  function cancelCand(){ document.getElementById('m-cand').style.display='none'; }
  async function submitCand(){
    var title=document.getElementById('m-cand-title').value.trim();
    var body=document.getElementById('m-out').textContent.trim();
    if(!title){ showToast('请先填一句话规则标题'); return; }
    if(!body){ showToast('没有可存的正文(先运行 AI)'); return; }
    var src = curMode==='pit'?'踩坑':curMode==='review'?'周复盘':'手动';
    try{
      var r=await fetch('/api/candidate',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({action:'stage', title:title, body:body, source:src})});
      var d=await r.json();
      if(d.error){ showToast(d.error); return; }
      document.getElementById('m-saved').textContent='📝 已存为候选(刷新看板,在「候选规则」区批准/丢弃)';
      document.getElementById('m-cand').style.display='none';
      document.getElementById('m-tocand').style.display='none';
    }catch(e){ showToast('请求失败:'+e); }
  }
  // 候选规则批准/丢弃(看板卡片)— 批准改行内二次确认,丢弃直接执行
  async function candAct(action, id, el){
    var btn=el||event.target;
    if(action==='approve' && !armed(btn,'再点确认')) return;
    btn.disabled=true; var old=btn.textContent; btn.textContent='处理中…';
    try{
      var r=await fetch('/api/candidate',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({action:action, id:id})});
      var d=await r.json();
      if(d.error){ showToast(d.error); btn.disabled=false; btn.textContent=old; return; }
      softReload();
    }catch(e){ showToast('请求失败:'+e); btn.disabled=false; btn.textContent=old; }
  }
  // 点击分发：data-read(知识库)/data-path(子系统/规则/复盘) → 统一进阅读弹窗(可就地编辑)
  document.addEventListener('click', function(e){
    var rd=e.target.closest('[data-read]');
    if(rd){ openReader(rd.getAttribute('data-read')); return; }
    var el=e.target.closest('[data-path]');
    if(el){ openReader(el.getAttribute('data-path')); }
  });

  // 今日见闻：归档 / 丢弃(跨日期,带 date)
  async function jwAct(action, id, date){
    var btn=event.target; btn.disabled=true; btn.textContent='处理中…';
    try{
      var r=await fetch('/api/jianwen',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({action:action, id:id, date:date})});
      var d=await r.json();
      if(d.ok){
        softReload();
      }else{
        showToast(d.error||'操作失败');
        btn.disabled=false; btn.textContent=action==='archive'?'📥 归档进知识库':'🗑 丢弃';
      }
    }catch(e){ showToast('请求失败：'+e); btn.disabled=false; }
  }

  // 阅读弹窗 + [[双链]] 跳转(纯客户端栈,带返回) + 就地编辑
  var readerStack = [];
  function closeReader(){
    document.getElementById('reader-mask').classList.remove('on');
    readerStack = [];
    document.getElementById('r-back').style.display='none';
    // 编辑过源文件 → 关闭时刷新首页,让计数/卡片同步(保留滚动位置)
    if(window.__homeDirty){ window.__homeDirty=false; softReload(); }
  }
  async function loadReader(path, link, push){
    if(push) readerStack.push({path:path||'', link:link||''});
    document.getElementById('reader-mask').classList.add('on');
    readerEditOff();  // 每次加载回到查看态
    document.getElementById('r-title').textContent='加载中…';
    document.getElementById('r-body').innerHTML='';
    document.getElementById('r-edit-btn').style.display='none';
    document.getElementById('r-back').style.display = readerStack.length>1 ? '' : 'none';
    var url = link ? ('/api/read?link='+encodeURIComponent(link))
                   : ('/api/read?path='+encodeURIComponent(path));
    try{
      var d = await (await fetch(url)).json();
      if(d.error){
        document.getElementById('r-title').textContent='⚠';
        document.getElementById('r-body').innerHTML='<div class="stub">'+d.error+'</div>'; return;
      }
      if(d.notfound){
        document.getElementById('r-title').textContent=d.label;
        document.getElementById('r-body').innerHTML='<div class="stub">⚠ 暂无对应文件。这是预埋的双链 <b>[[ '+d.label+' ]]</b> — 将来写了同名笔记,点这里就能跳。</div>';
        return;
      }
      document.getElementById('r-title').textContent=d.title;
      document.getElementById('r-body').innerHTML=d.html;
      window.__rPath = d.path;
      window.__rRaw = d.raw || '';
      document.getElementById('r-edit-btn').style.display='';
      var box=document.querySelector('#reader-mask .modal');
      if(d.anchor){
        var el=document.getElementById(d.anchor);
        if(el){ el.scrollIntoView({block:'center'}); el.style.background='var(--terra-soft)'; }
      } else { box.scrollTop=0; }
    }catch(e){
      document.getElementById('r-title').textContent='⚠ 请求失败';
      document.getElementById('r-body').textContent=String(e);
    }
  }
  function openReader(path){ loadReader(path,null,true); }
  function openReaderByLink(link){ loadReader(null,link,true); }
  function readerBack(){
    if(readerStack.length<=1){ closeReader(); return; }
    readerStack.pop();
    var prev=readerStack[readerStack.length-1];
    loadReader(prev.path, prev.link, false);
  }
  // 查看态 ↔ 编辑态 切换 + 保存写回
  function readerEditOn(){
    if(!window.__rPath) return;
    document.getElementById('r-edit').value = window.__rRaw || '';
    document.getElementById('r-body').style.display='none';
    document.getElementById('r-edit-wrap').style.display='block';
    document.getElementById('r-edit-btn').style.display='none';
    document.getElementById('r-saved').style.display='none';
  }
  function readerEditOff(){
    document.getElementById('r-edit-wrap').style.display='none';
    document.getElementById('r-body').style.display='';
    if(window.__rPath) document.getElementById('r-edit-btn').style.display='';
  }
  async function readerSave(){
    var ta=document.getElementById('r-edit');
    if(!ta.value.trim()){ showToast('内容为空,已拒绝(防误清空)。git 基线是后悔药。'); return; }
    var btn=document.getElementById('r-save-btn'); btn.disabled=true; var old=btn.textContent; btn.textContent='保存中…';
    try{
      var r=await fetch('/api/save',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({path:window.__rPath, content:ta.value})});
      var d=await r.json();
      btn.disabled=false; btn.textContent=old;
      if(d.error){ showToast('保存失败:'+d.error); return; }
      window.__homeDirty=true;                 // 关闭弹窗时刷新首页
      readerEditOff();                          // 回查看态
      await loadReader(window.__rPath, null, false);  // 重拉,刷新渲染 + raw
      document.getElementById('r-saved').style.display='';
    }catch(e){ btn.disabled=false; btn.textContent=old; showToast('请求失败:'+e); }
  }
  // 硬约束:每条就地 编辑/删除 + 新增(写回 总纲领.md)
  function consOpen(title, action, index, text, done){
    window.__consAction=action; window.__consIndex=index;
    document.getElementById('cons-title').textContent=title;
    document.getElementById('cons-input').value=text;
    document.getElementById('cons-done').checked=done;
    document.getElementById('cons-mask').classList.add('on');
    setTimeout(function(){ document.getElementById('cons-input').focus(); },0);
  }
  function consEdit(i){
    var row=document.querySelectorAll('.cons-row')[i]; if(!row) return;
    var done=row.querySelector('.cons-mark').textContent.trim()==='✅';
    consOpen('编辑硬约束','update',i,row.querySelector('.cons-text').textContent,done);
  }
  function consAdd(){ consOpen('添加硬约束','add',null,'',false); }
  function closeCons(){ document.getElementById('cons-mask').classList.remove('on'); }
  async function saveCons(){
    var text=document.getElementById('cons-input').value.trim();
    if(!text){ showToast('约束内容不能为空'); return; }
    var body={action:window.__consAction, text:text, done:document.getElementById('cons-done').checked};
    if(window.__consAction==='update') body.index=window.__consIndex;
    try{
      var r=await fetch('/api/constraint',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
      var d=await r.json();
      if(d.error){ showToast(d.error); return; }
      softReload();
    }catch(e){ showToast('请求失败:'+e); }
  }
  function consDel(i, el){
    if(!armed(el,'删除?')) return;
    fetch('/api/constraint',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({action:'delete',index:i})})
      .then(function(r){return r.json();})
      .then(function(d){ if(d.error) showToast(d.error); else softReload(); });
  }
  // 规则:每条整块编辑/删除 + 新增(写回 规则库.md),与硬约束同款弹窗
  function ruleOpen(title, action, index, text){
    window.__ruleAction=action; window.__ruleIndex=index;
    document.getElementById('rule-title').textContent=title;
    document.getElementById('rule-input').value=text;
    document.getElementById('rule-mask').classList.add('on');
    setTimeout(function(){ document.getElementById('rule-input').focus(); },0);
  }
  function ruleAdd(){
    ruleOpen('添加规则','add',null,
      '## R — \n- 触发:\n- 类型:\n- 规则:\n- 日期:{{TODAY}}');
  }
  async function ruleEdit(i){
    try{
      var r=await fetch('/api/rule',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({action:'get',index:i})});
      var d=await r.json();
      if(d.error){ showToast(d.error); return; }
      ruleOpen('编辑规则','update',i,d.text);
    }catch(e){ showToast('请求失败:'+e); }
  }
  function closeRule(){ document.getElementById('rule-mask').classList.remove('on'); }
  async function saveRule(){
    var text=document.getElementById('rule-input').value;
    if(!text.trim()){ showToast('规则内容不能为空'); return; }
    var body={action:window.__ruleAction, text:text};
    if(window.__ruleAction==='update') body.index=window.__ruleIndex;
    try{
      var r=await fetch('/api/rule',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
      var d=await r.json();
      if(d.error){ showToast(d.error); return; }
      softReload();
    }catch(e){ showToast('请求失败:'+e); }
  }
  function ruleDel(i, el){
    if(!armed(el,'删除?')) return;
    fetch('/api/rule',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({action:'delete',index:i})})
      .then(function(r){return r.json();})
      .then(function(d){ if(d.error) showToast(d.error); else softReload(); });
  }
  function ruleToggle(i){
    var row=document.querySelectorAll('.rule-row')[i];
    var d=document.getElementById('rd-'+i);
    if(!row||!d) return;
    var open=d.classList.toggle('show');
    row.classList.toggle('open', open);
  }
  function diaryToggle(i){
    var row=document.querySelectorAll('.diary-row')[i];
    var d=document.getElementById('dd-'+i);
    if(!row||!d) return;
    var open=d.classList.toggle('show');
    row.classList.toggle('open', open);
  }
  async function diarySubmit(){
    var text=document.getElementById('diary-input').value.trim();
    if(!text){ showToast('写一句再记'); return; }
    try{
      var r=await fetch('/api/diary',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'add',text:text})});
      var d=await r.json();
      if(d.error){ showToast(d.error); return; }
      document.getElementById('diary-input').value='';
      showToast('✅ 已记进 '+d.date);
      softReload();
    }catch(e){ showToast('请求失败:'+e); }
  }
  async function diaryDigest(){
    showToast('🔍 正在整理本周日记…');
    try{
      var r=await fetch('/api/diary_digest',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
      var d=await r.json();
      if(d.error){ showToast(d.error); return; }
      showToast('✅ 已整理本周日记,结果存进 03_每日日记/');
      softReload();
    }catch(e){ showToast('请求失败:'+e); }
  }
  // 进行中事项:逐条增/删/改(写回 01_子系统/{name}.md),解除 2 件上限
  function todoOpen(title, action, subsystem, index, text){
    window.__todoAction=action; window.__todoSub=subsystem; window.__todoIndex=index;
    document.getElementById('todo-title').textContent=title;
    document.getElementById('todo-input').value=text;
    document.getElementById('todo-mask').classList.add('on');
    setTimeout(function(){ document.getElementById('todo-input').focus(); },0);
  }
  function todoAdd(sub){ todoOpen('添加进行中事项','add',sub,null,''); }
  function todoEdit(sub, i, el){
    var span=el.closest('.ln').querySelector('.todo-text');
    var text=span ? span.textContent.replace(/^\s*·\s*/,'') : '';
    todoOpen('编辑进行中事项','update',sub,i,text);
  }
  function closeTodo(){ document.getElementById('todo-mask').classList.remove('on'); }
  async function saveTodo(){
    var text=document.getElementById('todo-input').value.trim();
    if(!text){ showToast('事项内容不能为空'); return; }
    var body={action:window.__todoAction, subsystem:window.__todoSub, text:text};
    if(window.__todoAction==='update') body.index=window.__todoIndex;
    try{
      var r=await fetch('/api/todo',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
      var d=await r.json();
      if(d.error){ showToast(d.error); return; }
      softReload();
    }catch(e){ showToast('请求失败:'+e); }
  }
  function todoDel(sub, i, el){
    if(!armed(el,'删除?')) return;
    fetch('/api/todo',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({action:'delete',subsystem:sub,index:i})})
      .then(function(r){return r.json();})
      .then(function(d){ if(d.error) showToast(d.error); else softReload(); });
  }
  // —— 模型管理(切换/增/改/删)——
  function __esc(s){
    return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }
  function openModel(){
    document.getElementById('model-mask').classList.add('on');
    modelFormCancel();
    modelLoad();
  }
  function closeModel(){ document.getElementById('model-mask').classList.remove('on'); }
  function modelLoad(){
    fetch('/api/models').then(function(r){return r.json();}).then(function(d){
      if(d.error){ showToast(d.error); return; }
      window.__models=d.models||[];
      window.__activeModel=d.active;
      modelRender();
    }).catch(function(e){ showToast('加载失败:'+e); });
  }
  function modelRender(){
    var wrap=document.getElementById('model-list');
    var list=window.__models||[]; var active=window.__activeModel;
    if(!list.length){ wrap.innerHTML='<div class="empty">还没有模型,下面点「+ 添加模型」</div>'; return; }
    var rows=list.map(function(m){
      var isOn=(m.id===active);
      var actBtn=isOn
        ? '<span class="model-on">● 当前</span>'
        : '<button class="model-act" onclick="modelActivate(\''+m.id+'\')">切换为当前</button>';
      return '<div class="model-row'+(isOn?' model-row-on':'')+'">'
        + '<div class="model-row-main">'
          + '<div class="model-row-label">'+__esc(m.label)+'</div>'
          + '<div class="model-row-meta">'+__esc(m.model)+' · '+__esc(m.base_url)+'</div>'
          + '<div class="model-row-key">密钥 '+(m.has_key?__esc(m.key_mask):'未填')+'</div>'
        + '</div>'
        + '<div class="model-row-btns">'
          + actBtn
          + '<button class="model-edit" onclick="modelForm(\''+m.id+'\')">编辑</button>'
          + (list.length<=1?'':'<button class="model-del" onclick="modelDel(\''+m.id+'\',this)">删除</button>')
        + '</div>'
      + '</div>';
    }).join('');
    wrap.innerHTML=rows;
  }
  function modelForm(id){
    window.__modelEditId=id||'';
    document.getElementById('model-form-title').textContent= id?'编辑模型':'添加模型';
    var entry=null;
    if(id){ for(var i=0;i<(window.__models||[]).length;i++){ if(window.__models[i].id===id){ entry=window.__models[i]; break; } } }
    document.getElementById('mf-label').value = entry?entry.label:'';
    document.getElementById('mf-model').value = entry?entry.model:'';
    document.getElementById('mf-base').value   = entry?entry.base_url:'';
    document.getElementById('mf-key').value    = '';   // 编辑时留空=不改密钥
    document.getElementById('mf-temp').value   = entry?entry.temperature:'0.4';
    document.getElementById('mf-key').placeholder = id?'留空=不改密钥':'sk-…';
    document.getElementById('model-form-wrap').style.display='block';
  }
  function modelFormCancel(){
    window.__modelEditId='';
    document.getElementById('model-form-wrap').style.display='none';
  }
  async function modelSave(){
    var label=document.getElementById('mf-label').value.trim();
    var model=document.getElementById('mf-model').value.trim();
    var base =document.getElementById('mf-base').value.trim();
    var key  =document.getElementById('mf-key').value;
    var temp =document.getElementById('mf-temp').value.trim()||'0.4';
    var editId=window.__modelEditId;
    if(!model||!base){ showToast('模型名 / 接口地址不能为空'); return; }
    if(!editId && !key){ showToast('新增模型必须填 API Key'); return; }
    var body={label:label, model:model, base_url:base, temperature:temp};
    if(editId){ body.action='update'; body.id=editId; if(key){ body.api_key=key; } }
    else      { body.action='add'; body.api_key=key; if(!label){ body.label=model; } }
    try{
      var r=await fetch('/api/models',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
      var d=await r.json();
      if(d.error){ showToast(d.error); return; }
      showToast(editId?'已更新':'已添加');
      modelFormCancel();
      modelLoad();
    }catch(e){ showToast('请求失败:'+e); }
  }
  function modelActivate(id){
    fetch('/api/models',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({action:'activate',id:id})})
      .then(function(r){return r.json();})
      .then(function(d){
        if(d.error){ showToast(d.error); return; }
        showToast('已切换,刷新中…');
        setTimeout(softReload, 500);
      });
  }
  function modelDel(id, el){
    if(!armed(el,'删除?')) return;
    fetch('/api/models',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({action:'delete',id:id})})
      .then(function(r){return r.json();})
      .then(function(d){ if(d.error) showToast(d.error); else { modelFormCancel(); modelLoad(); } });
  }
  // 双链点击委托(阅读弹窗内)
  document.getElementById('reader-mask').addEventListener('click', function(e){
    var wl=e.target.closest('.wl'); if(!wl) return;
    e.preventDefault(); openReaderByLink(wl.getAttribute('data-link'));
  });
  // 知识库搜索(客户端过滤,库小够用 — 这是 flat 文件阶段)
  function filterKB(){
    var q=document.getElementById('kb-search').value.trim().toLowerCase();
    document.querySelectorAll('#kb-list .note').forEach(function(n){
      n.style.display = (!q || n.textContent.toLowerCase().indexOf(q)>=0) ? '' : 'none';
    });
  }
</script>
</body></html>"""

# ── HTTP 处理 ──────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # 静音

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            try:
                page = render_page(gather_data())
            except Exception as e:
                page = f"<h1>读取数据出错</h1><pre>{esc(e)}</pre>"
            self._send(200, page, "text/html; charset=utf-8")
        elif parsed.path == "/api/read":
            q = urllib.parse.parse_qs(parsed.query)
            if q.get("link"):
                result = read_for_reader(link=q["link"][0])
            else:
                result = read_for_reader(path_str=q.get("path", [""])[0])
            self._send(200, json.dumps(result, ensure_ascii=False))
        elif parsed.path == "/api/models":
            self._send(200, json.dumps(models_payload(), ensure_ascii=False))
        elif parsed.path == "/mobile":
            try:
                import mobile_ui as M
                page = M.render_page(M._get_data())
            except Exception as e:
                page = f"<h1>手机端出错</h1><pre>{esc(e)}</pre>"
            self._send(200, page, "text/html; charset=utf-8")
        elif parsed.path == "/mobile/api/data":
            try:
                import mobile_ui as M
                self._send(200, json.dumps(M._get_data(), ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False))
        elif parsed.path == "/mobile/api/write":
            self._handle_mobile_write()
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def _handle_mobile_write(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except Exception:
            self._send(400, json.dumps({"error": "bad json"}))
            return
        text = body.get("text", "").strip()
        if not text:
            self._send(400, json.dumps({"error": "内容不能为空"}))
            return
        msg_type = body.get("type", "diary")
        try:
            from diary_add import append_diary_with_ai
            # 灵感已合并进日记,统一走 diary
            r = append_diary_with_ai(text)
            self._send(200, json.dumps(r, ensure_ascii=False) if r.get("ok") else json.dumps(r, ensure_ascii=False))
        except Exception as e:
            self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False))

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/jianwen":
            self._handle_jianwen()
            return
        if parsed.path == "/api/candidate":
            self._handle_candidate()
            return
        if parsed.path == "/api/save":
            self._handle_save()
            return
        if parsed.path == "/api/constraint":
            self._handle_constraint()
            return
        if parsed.path == "/api/rule":
            self._handle_rule()
            return
        if parsed.path == "/api/todo":
            self._handle_todo()
            return
        if parsed.path == "/api/diary":
            self._handle_diary()
            return
        if parsed.path == "/api/diary_digest":
            self._handle_diary_digest()
            return
        if parsed.path == "/api/models":
            self._handle_models()
            return
        if parsed.path == "/mobile/api/write":
            self._handle_mobile_write()
            return
        if parsed.path != "/api/ai":
            self._send(404, json.dumps({"error": "not found"}))
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except Exception:
            self._send(400, json.dumps({"error": "bad json"}))
            return
        mode = body.get("mode", "")
        fn = AI_MODES.get(mode)
        if not fn:
            self._send(400, json.dumps({"error": f"未知模式 {mode}"}, ensure_ascii=False))
            return
        try:
            result = fn(body.get("input", "")) if mode != "checkup" else fn()
        except SystemExit as e:
            result = {"error": str(e)}
        except Exception as e:
            result = {"error": f"AI 调用出错：{e}"}
        self._send(200, json.dumps(result, ensure_ascii=False))

    def _handle_jianwen(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except Exception:
            self._send(400, json.dumps({"error": "bad json"}))
            return
        action = body.get("action", "")
        item_id = body.get("id", "")
        # 跨日期:用条目所在日期(前端传),没传则兜底今天
        date = body.get("date") or A.today_str()
        try:
            if action == "archive":
                result = J.archive_item(date, item_id)
            elif action == "discard":
                result = J.discard_item(date, item_id)
            else:
                result = {"error": f"未知操作 {action}"}
        except Exception as e:
            result = {"error": str(e)}
        self._send(200, json.dumps(result, ensure_ascii=False))

    def _handle_candidate(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except Exception:
            self._send(400, json.dumps({"error": "bad json"}))
            return
        action = body.get("action", "")
        try:
            if action == "stage":
                title = (body.get("title", "") or "").strip()
                cbody = body.get("body", "") or ""
                source = body.get("source", "手动") or "手动"
                if not title:
                    result = {"error": "标题不能为空"}
                elif not cbody.strip():
                    result = {"error": "正文为空,先运行 AI 产出内容"}
                else:
                    result = stage_candidate(title, cbody, source)
            elif action == "approve":
                result = approve_candidate(body.get("id", ""))
            elif action == "discard":
                result = discard_candidate(body.get("id", ""))
            else:
                result = {"error": f"未知操作 {action}"}
        except Exception as e:
            result = {"error": str(e)}
        self._send(200, json.dumps(result, ensure_ascii=False))

    def _handle_save(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except Exception:
            self._send(400, json.dumps({"error": "bad json"}))
            return
        result = save_file(body.get("path", ""), body.get("content", ""))
        self._send(200, json.dumps(result, ensure_ascii=False))

    def _handle_constraint(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except Exception:
            self._send(400, json.dumps({"error": "bad json"}))
            return
        try:
            result = _rewrite_constraint(
                body.get("action", ""),
                body.get("index"),
                body.get("done"),
                body.get("text"),
            )
        except Exception as e:
            result = {"error": str(e)}
        self._send(200, json.dumps(result, ensure_ascii=False))

    def _handle_rule(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except Exception:
            self._send(400, json.dumps({"error": "bad json"}))
            return
        try:
            result = rewrite_rule(body.get("action", ""), body.get("index"), body.get("text"))
        except Exception as e:
            result = {"error": str(e)}
        self._send(200, json.dumps(result, ensure_ascii=False))

    def _handle_todo(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except Exception:
            self._send(400, json.dumps({"error": "bad json"}))
            return
        try:
            result = rewrite_todo(
                body.get("subsystem", ""),
                body.get("action", ""),
                body.get("index"),
                body.get("text"),
            )
        except Exception as e:
            result = {"error": str(e)}
        self._send(200, json.dumps(result, ensure_ascii=False))

    def _handle_diary(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except Exception:
            self._send(400, json.dumps({"error": "bad json"}))
            return
        try:
            if body.get("action") == "add":
                result = A.append_diary(body.get("text", ""))
            elif body.get("action") == "edit":
                result = A.edit_diary(body.get("date", ""), body.get("index"), body.get("text", ""))
            elif body.get("action") == "delete":
                result = A.delete_diary(body.get("date", ""), body.get("index"))
            else:
                result = {"error": "未知操作"}
        except Exception as e:
            result = {"error": str(e)}
        self._send(200, json.dumps(result, ensure_ascii=False))

    def _handle_diary_digest(self):
        try:
            result = A.run_diary_digest(_cfg())
        except SystemExit as e:
            result = {"error": str(e)}
        except Exception as e:
            result = {"error": str(e)}
        self._send(200, json.dumps(result, ensure_ascii=False))

    def _handle_models(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except Exception:
            self._send(400, json.dumps({"error": "bad json"}))
            return
        action = body.get("action", "")
        try:
            if action == "add":
                result = A.add_model(
                    body.get("label", ""),
                    body.get("model", ""),
                    body.get("base_url", ""),
                    body.get("api_key", ""),
                    body.get("temperature", "0.4"),
                )
            elif action == "update":
                result = A.update_model(
                    body.get("id", ""),
                    body.get("label"),
                    body.get("model"),
                    body.get("base_url"),
                    body.get("api_key"),
                    body.get("temperature"),
                )
            elif action == "delete":
                result = A.delete_model(body.get("id", ""))
            elif action == "activate":
                result = A.activate_model(body.get("id", ""))
            else:
                result = {"error": f"未知操作 {action}"}
        except Exception as e:
            result = {"error": str(e)}
        self._send(200, json.dumps(result, ensure_ascii=False))

# ── 启动 ───────────────────────────────────────────────
def find_port(start=8770):
    import socket
    for p in range(start, start + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    return start

def main():
    port = find_port()
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"✅ 看板已启动: {url}")
    print("   关闭：在此终端按 Ctrl+C")
    if "--no-open" not in sys.argv:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
        srv.shutdown()

if __name__ == "__main__":
    main()
