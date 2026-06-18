#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
个人成长 · 手机端 Web UI
=========================
移动优先设计，大按钮、底部导航、触控友好。
依赖已有 assistant.py / diary_add.py，零额外 pip 包。
端口 8780
"""

import sys, os, json, datetime, urllib.parse, html as html_mod
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import assistant as A
from diary_add import append_diary_with_ai

ROOT = A.ROOT
SUBSYS = A.SUBSYS
REVIEWS = A.REVIEWS
RULES = A.RULES
DIARY_DIR = A.DIARY

PORT = 8780

# ── 数据接口 ──────────────────────────────────

def _get_data():
    constraints = _parse_constraints()
    subsystems = [_parse_subsystem(n) for n in ["研究工作", "学习成长", "复盘进化"]]
    rules = _parse_rules()
    diary = _parse_diary(limit=20)
    today = A.today_str()
    # 复用 server.py 的解析逻辑(延迟 import 避免循环依赖;server 已在路由内延迟 import 本模块)
    jianwen, candidates = [], []
    try:
        import server as S
        jianwen = S.parse_jianwen()
        candidates = S.parse_candidates()
    except Exception:
        pass
    return {
        "constraints": constraints, "subsystems": subsystems,
        "rules": rules, "diary": diary, "today": today,
        "jianwen": jianwen, "candidates": candidates,
    }

def _parse_constraints():
    text = A.read_file(ROOT / "00_总纲领.md")
    out, in_sec = [], False
    for line in text.splitlines():
        if line.startswith("## ") and "硬约束" in line:
            in_sec = True; continue
        if in_sec and line.startswith("## "):
            break
        s = line.strip()
        if in_sec and (s.startswith("- [ ]") or s.startswith("- [x]")):
            out.append({"done": s.startswith("- [x]"), "text": s[5:].strip()})
    return out

def _parse_subsystem(name):
    f = SUBSYS / f"{name}.md"
    if not f.exists():
        return {"name": name, "duty": "", "todos": [], "exists": False}
    text = A.read_file(f)
    duty = ""
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("> 职责"):
            duty = s.lstrip("> ").replace("职责：", "").replace("职责:", "").strip()
            break
    todos, in_sec = [], False
    for line in text.splitlines():
        if line.startswith("## ") and "进行中事项" in line:
            in_sec = True; continue
        if in_sec and line.startswith("## "):
            break
        s = line.strip()
        if in_sec and s.startswith("-") and s[1:].strip() and not s[1:].strip().startswith(">"):
            todos.append(s[1:].strip())
    return {"name": name, "duty": duty, "todos": todos, "exists": True}

def _parse_rules():
    import re
    text = A.read_file(RULES)
    out, buf, n, title = [], [], None, None
    for line in text.splitlines():
        m = re.match(r"^##\s+R(\d+)\s*[—-]\s*(.+)$", line)
        if m:
            if n is not None:
                out.append({"n": n, "title": title, "body": "\n".join(buf).strip()})
            n, title, buf = m.group(1), m.group(2).strip(), []
        elif n is not None:
            buf.append(line)
    if n is not None:
        out.append({"n": n, "title": title, "body": "\n".join(buf).strip()})
    return out

def _parse_diary(limit=20):
    if not DIARY_DIR.exists():
        return []
    out = []
    for p in sorted(DIARY_DIR.glob("20*.md"), reverse=True)[:limit]:
        raw = p.read_text(encoding="utf-8")
        lines = [l for l in raw.splitlines() if l.strip() and not l.startswith("#")]
        entries = []
        for l in lines:
            if l.startswith("- "):
                rest = l[2:]
                time_prefix = ""
                if len(rest) > 5 and rest[2] == ":":
                    time_prefix = rest[:5]
                    rest = rest[5:].lstrip()
                entries.append({"time": time_prefix, "text": rest})
        out.append({"date": p.stem, "short": p.stem[5:], "entries": entries})
    return out

# ── HTML 模板 ──────────────────────────────────

PAGE_TPL = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<meta name="theme-color" content="#f4ece1">
<title>个人成长</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#f4ece1; --card:#fffaf3; --card2:#fbf5ec; --line:#e8dccb;
  --text:#4a3f35; --text2:#857565; --text3:#a99a88;
  --terra:#c2693f; --terra-soft:#f6e3d6; --olive:#7a8450; --olive-soft:#eaeede;
  --plum:#9c6b8a; --plum-soft:#f1e3ee; --gold:#c99a3f;
  --accent:#c2693f; --accent2:#f6e3d6;
  --radius:18px; --safe-bottom:env(safe-area-inset-bottom,8px);
  --num:"Helvetica Neue","Segoe UI","PingFang SC","Microsoft YaHei",system-ui,sans-serif;
}
body{font-family:"Georgia","Songti SC","Microsoft YaHei",serif;
  font-variant-numeric:lining-nums tabular-nums;
  background:var(--bg);color:var(--text);
  padding-bottom:calc(70px + var(--safe-bottom));min-height:100vh;font-size:15px;line-height:1.6}
.tab-content{display:none;padding:18px 16px 14px;max-width:520px;margin:0 auto}
.tab-content.active{display:block}
h1{font-size:21px;font-weight:700;margin-bottom:4px;letter-spacing:1px}
h2{font-size:16px;font-weight:700;margin:18px 0 10px;color:var(--terra);
  display:flex;align-items:center;gap:8px}
h2::before{content:"";width:8px;height:8px;border-radius:50%;background:var(--terra);flex-shrink:0}
.date-badge{font-size:13px;color:var(--terra);margin-bottom:14px;font-style:italic}

.card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
  padding:16px 18px;margin-bottom:14px;box-shadow:0 2px 12px rgba(160,120,80,.06)}
.card-title{font-size:13px;color:var(--text2);margin-bottom:6px;font-weight:500}
.card-stat{font-size:30px;font-weight:700;color:var(--terra);font-family:var(--num)}
.card-stat.done{color:var(--olive)}
.card-stat.sub{color:var(--plum)}
.card-row{display:flex;gap:12px}
.card-row .card{flex:1;text-align:center;padding:16px 8px}

.todo-item{display:flex;align-items:flex-start;gap:8px;padding:8px 0;border-bottom:1px dashed var(--line);font-size:14px}
.todo-item:last-child{border:none}
.todo-dot{width:6px;height:6px;border-radius:50%;background:var(--terra);margin-top:8px;flex-shrink:0}
.todo-dot.done{background:var(--olive)}

.diary-item{padding:9px 0;border-bottom:1px dashed var(--line);font-size:14px}
.diary-item:last-child{border:none}
.diary-time{color:var(--gold);font-size:12px;margin-right:6px;font-family:var(--num)}
.diary-text{display:inline;color:var(--text)}
.diary-text .tag{color:var(--terra);font-size:12px;background:var(--terra-soft);padding:1px 5px;border-radius:5px}
.diary-text .idea-mark{color:#7a6a3e;background:#fef9eb;border-left:3px solid var(--gold);padding-left:6px;display:inline-block}
.diary-date-hdr{font-size:13px;color:var(--terra);font-weight:700;margin:14px 0 4px}

.rule-item{padding:11px 0;border-bottom:1px dashed var(--line)}
.rule-item:last-child{border:none}
.rule-title{font-weight:700;font-size:14px;margin-bottom:5px;color:var(--terra)}
.rule-title .rid{color:var(--gold);font-family:var(--num);margin-right:4px}
.rule-body{font-size:13px;color:var(--text2);line-height:1.7;white-space:pre-wrap}

.input-card{position:relative}
textarea{width:100%;background:#fffdf9;border:1px solid var(--terra-soft);border-radius:12px;
  color:var(--text);font-size:15px;padding:13px;resize:none;min-height:110px;font-family:inherit;
  line-height:1.7;outline:none;transition:border .2s,box-shadow .2s}
textarea:focus{border-color:var(--terra);box-shadow:0 0 0 3px rgba(194,105,63,.13)}
.send-btn{display:block;width:100%;padding:15px;color:#fff;border:none;border-radius:14px;
  font-size:16px;font-weight:700;font-family:inherit;margin-top:12px;cursor:pointer;
  background:linear-gradient(135deg,#d6794a 0%,var(--terra) 55%,#b85c2c 100%);
  box-shadow:0 4px 12px rgba(194,105,63,.28);transition:transform .16s,box-shadow .16s,filter .16s}
.send-btn:active{transform:translateY(1px);filter:brightness(.96)}
.send-btn:disabled{opacity:.45;pointer-events:none;box-shadow:none}
.quick-btns{display:flex;gap:8px;margin-top:12px;flex-wrap:wrap}
.quick-btn{flex:1;min-width:96px;padding:11px;border:1px dashed #c5cdb0;border-radius:10px;
  background:var(--olive-soft);color:var(--olive);font-size:13px;text-align:center;
  cursor:pointer;font-family:inherit;transition:all .2s}
.quick-btn:active{background:var(--olive);color:#fff}
.result-msg{margin-top:12px;padding:9px 13px;border-radius:10px;font-size:13px;display:none}
.result-msg.ok{display:block;background:var(--olive-soft);color:var(--olive)}
.result-msg.err{display:block;background:#fbeede;color:#b8552e}

.tab-bar{position:fixed;bottom:0;left:0;right:0;background:var(--card);
  border-top:1px solid var(--line);display:flex;padding:6px 0 calc(6px + var(--safe-bottom));
  z-index:100;max-width:520px;margin:0 auto;box-shadow:0 -2px 14px rgba(160,120,80,.08)}
.tab{flex:1;display:flex;flex-direction:column;align-items:center;gap:2px;padding:5px 0;
  cursor:pointer;font-size:10px;color:var(--text3);transition:color .2s;border:none;
  background:none;font-family:inherit;-webkit-tap-highlight-color:transparent}
.tab.active{color:var(--terra)}
.tab-icon{font-size:20px;line-height:1}
.tab-label{font-size:10px}

.empty-state{text-align:center;padding:40px 20px;color:var(--text3);font-size:14px;font-style:italic}

.sub-card{padding:11px 0;border-bottom:1px dashed var(--line)}
.sub-card:last-child{border:none}
.sub-name{font-weight:700;font-size:14px;margin-bottom:2px;color:var(--terra)}
.sub-duty{font-size:12px;color:var(--text3);margin-bottom:5px;font-style:italic}
.sub-todos{padding-left:12px}
.sub-todo-item{font-size:13px;color:var(--text2);padding:2px 0}

.const-item{display:flex;align-items:flex-start;gap:9px;padding:7px 0;font-size:14px}
.const-check{width:18px;height:18px;border-radius:5px;border:2px solid var(--text3);
  flex-shrink:0;margin-top:2px;display:flex;align-items:center;justify-content:center;font-size:12px}
.const-check.done{background:var(--olive);border-color:var(--olive);color:#fff}
.const-text{flex:1}

.scroll-wrap{max-height:calc(100vh - 92px);overflow-y:auto;-webkit-overflow-scrolling:touch}
.scroll-wrap::-webkit-scrollbar{width:0}

/* 见闻池 */
.jw-item{background:var(--card2);border:1px solid var(--line);border-radius:14px;padding:13px 15px;margin-bottom:11px}
.jw-head{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:6px}
.jw-type{font-size:11px;color:var(--terra);background:var(--terra-soft);padding:2px 9px;border-radius:9px;font-weight:700}
.jw-title{font-size:14px;font-weight:700;color:var(--text);flex:1;min-width:120px}
.jw-date{font-size:11px;color:#b87a1e;background:#fbeede;border:1px solid #e6c4ab;padding:1px 7px;border-radius:9px}
.jw-summary{font-size:13px;color:var(--text2);line-height:1.6;margin-bottom:10px}
.jw-url{font-size:12px;color:var(--gold);text-decoration:none;display:inline-block;margin-bottom:8px}
.jw-btns{display:flex;gap:8px}
.jw-arch,.jw-disc,.cand-approve,.cand-disc{flex:1;border-radius:10px;padding:9px;cursor:pointer;font-size:13px;font-family:inherit;font-weight:600;border:1px solid var(--line)}
.jw-arch,.cand-approve{background:var(--olive-soft);color:var(--olive);border-color:#c2caa8}
.jw-arch:active,.cand-approve:active{background:var(--olive);color:#fff}
.jw-disc,.cand-disc{background:#f5f0eb;color:var(--text2)}
.jw-disc:active,.cand-disc:active{background:#e5ddd2;color:var(--terra)}
.jw-done{font-size:12px;color:var(--text3);padding:4px 0;border-bottom:1px dashed var(--line)}
/* 候选规则 */
.cand-card{border:1px solid var(--gold)}
.cand-item{background:var(--card2);border:1px solid var(--line);border-radius:14px;padding:13px 15px;margin-bottom:11px}
.cand-head{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:6px}
.cand-src{font-size:11px;background:var(--terra-soft);color:var(--terra);padding:2px 8px;border-radius:9px}
.cand-title{font-weight:700;color:var(--text);flex:1;min-width:120px;font-size:14px}
.cand-body{font-size:13px;color:var(--text2);line-height:1.7;white-space:pre-wrap;background:var(--card);
  border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin-bottom:10px;max-height:200px;overflow:auto}
.cand-btns{display:flex;gap:8px}
/* AI 卡片 */
.ai-btn{display:block;width:100%;text-align:left;background:var(--terra-soft);border:1px solid #e6c4ab;
  border-radius:14px;padding:15px 16px;margin-bottom:12px;cursor:pointer;font-family:inherit;transition:all .16s}
.ai-btn:active{background:var(--terra);transform:translateY(1px)}
.ai-btn .ti{font-size:15px;font-weight:700;color:var(--terra)}
.ai-btn:active .ti{color:#fff}
.ai-btn .de{font-size:12px;color:var(--text2);margin-top:3px}
.ai-btn:active .de{color:#fbe8dd}
/* 行内编辑小按钮 */
.edit-btns{display:inline-flex;gap:2px;margin-left:auto;flex-shrink:0}
.mini-btn{background:transparent;border:none;cursor:pointer;font-size:14px;color:var(--text3);padding:3px 7px;border-radius:8px;font-family:inherit}
.mini-btn:active{background:var(--terra-soft);color:var(--terra)}
.add-btn{display:block;width:100%;background:var(--olive-soft);color:var(--olive);border:1px dashed #c2caa8;
  border-radius:11px;padding:10px;margin-top:10px;cursor:pointer;font-family:inherit;font-size:13px}
.add-btn:active{background:var(--olive);color:#fff}
/* 弹窗 */
.mask{position:fixed;inset:0;background:rgba(74,63,53,.45);display:none;align-items:flex-end;justify-content:center;z-index:200}
.mask.on{display:flex}
.sheet{background:var(--card);border-radius:22px 22px 0 0;width:100%;max-width:520px;max-height:88vh;
  overflow:auto;padding:22px 18px calc(22px + var(--safe-bottom));box-shadow:0 -8px 40px rgba(74,63,53,.3)}
.sheet h3{color:var(--terra);font-size:17px;margin-bottom:14px}
.sheet textarea{min-height:120px;margin-bottom:6px}
.sheet .out{white-space:pre-wrap;background:var(--card2);border:1px solid var(--line);border-radius:12px;
  padding:14px;margin-top:12px;font-size:14px;line-height:1.7;font-family:"Microsoft YaHei",sans-serif}
.sheet .saved{color:var(--olive);font-size:12px;margin-top:8px;font-style:italic}
.sheet-btns{display:flex;gap:10px;margin-top:14px}
.sheet-btns button{flex:1;border-radius:12px;padding:13px;cursor:pointer;font-size:15px;font-family:inherit;font-weight:600}
.sheet-cancel{background:var(--card2);border:1px solid var(--line);color:var(--text2)}
.sheet-ok{background:var(--terra);color:#fff;border:none}
.sheet-ok:disabled{opacity:.5}
.cons-done-lab{display:flex;align-items:center;gap:8px;font-size:14px;color:var(--text2);margin:10px 0}
.cons-done-lab input{width:auto}
/* toast */
#toast{position:fixed;left:50%;bottom:84px;transform:translateX(-50%) translateY(16px);background:var(--text);
  color:var(--bg);padding:11px 20px;border-radius:14px;font-size:13px;opacity:0;pointer-events:none;
  transition:opacity .2s,transform .2s;z-index:300;max-width:82vw;text-align:center}
#toast.on{opacity:1;transform:translateX(-50%) translateY(0)}
</style>
</head>
<body>

<div class="tab-bar" id="tabBar">
  <button class="tab active" data-tab="home"><span class="tab-icon">🏠</span><span class="tab-label">首页</span></button>
  <button class="tab" data-tab="write"><span class="tab-icon">✏️</span><span class="tab-label">写</span></button>
  <button class="tab" data-tab="diary"><span class="tab-icon">📖</span><span class="tab-label">日记</span></button>
  <button class="tab" data-tab="kanban"><span class="tab-icon">📋</span><span class="tab-label">看板</span></button>
  <button class="tab" data-tab="rules"><span class="tab-icon">📜</span><span class="tab-label">规则</span></button>
  <button class="tab" data-tab="ai"><span class="tab-icon">🤖</span><span class="tab-label">AI</span></button>
</div>

<div class="tab-content active" id="tabHome"></div>
<div class="tab-content" id="tabWrite"></div>
<div class="tab-content" id="tabDiary"></div>
<div class="tab-content" id="tabKanban"></div>
<div class="tab-content" id="tabRules"></div>
<div class="tab-content" id="tabAi"></div>

<!-- 通用底部弹窗(编辑硬约束/规则/事项) -->
<div class="mask" id="editMask">
  <div class="sheet">
    <h3 id="editTitle">编辑</h3>
    <textarea id="editInput" placeholder=""></textarea>
    <label class="cons-done-lab" id="editDoneLab" style="display:none"><input type="checkbox" id="editDone"> 已达成 / 勾掉(✅)</label>
    <div class="sheet-btns">
      <button class="sheet-cancel" onclick="closeEdit()">取消</button>
      <button class="sheet-ok" onclick="saveEdit()">保存</button>
    </div>
  </div>
</div>

<!-- AI 功能弹窗 -->
<div class="mask" id="aiMask">
  <div class="sheet">
    <h3 id="aiTitle">AI</h3>
    <div id="aiInputWrap"><textarea id="aiInput" placeholder=""></textarea></div>
    <div id="aiSpin" class="saved" style="display:none">🤖 AI 思考中… 首次响应可能十几秒,请稍候。</div>
    <div id="aiOut" class="out" style="display:none"></div>
    <div id="aiSaved" class="saved"></div>
    <div class="sheet-btns">
      <button class="sheet-cancel" onclick="closeAi()">关闭</button>
      <button class="sheet-ok" id="aiRun" onclick="runAi()">运行</button>
    </div>
  </div>
</div>

<div id="toast"></div>

<!-- 删除确认弹窗 -->
<div class="mask" id="confirmMask">
  <div class="sheet" style="max-width:400px">
    <h3 id="confirmTitle">确认删除</h3>
    <div id="confirmMsg" style="font-size:14px;color:var(--text2);line-height:1.7;margin-bottom:4px"></div>
    <div class="sheet-btns">
      <button class="sheet-cancel" onclick="closeConfirm()">取消</button>
      <button class="sheet-ok" id="confirmOk" style="background:#b8552e" onclick="okConfirm()">删除</button>
    </div>
  </div>
</div>

<script>
var BASE = window.location.pathname.replace(/\/+$/, '') || '';
var API_WRITE = BASE + '/api/write';
var API_DATA = BASE + '/api/data';
// 交互写端点在 server.py 根路径(/api/*),不带 /mobile 前缀。
// 经 8770/mobile 访问时绝对路径直达;独立 8780 无这些端点(见 README:交互功能走 8770/mobile)。
var API_ROOT = (BASE === '/mobile') ? '' : BASE;
function apiPost(path, body){
  return fetch(API_ROOT+path, {method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body)}).then(function(r){ return r.json(); });
}
function showToast(msg){
  var t=document.getElementById('toast'); if(!t) return;
  t.textContent=msg; t.classList.add('on');
  if(window.__tT) clearTimeout(window.__tT);
  window.__tT=setTimeout(function(){ t.classList.remove('on'); }, 3000);
}
function armed(el, label){
  if(!el) return true;
  if(el.dataset.arm==='1'){ el.dataset.arm=''; el.textContent=el._old; return true; }
  el.dataset.arm='1'; el._old=el.textContent; el.textContent=label||'确认?';
  setTimeout(function(){ if(el.dataset.arm==='1'){ el.dataset.arm=''; el.textContent=el._old; } }, 4000);
  return false;
}
// 删除确认弹窗:点删除 → 弹框 → 点"删除"才执行
function confirmDialog(msg, onOK, okLabel){
  window.__confirmOK = onOK;
  document.getElementById('confirmMsg').textContent = msg;
  document.getElementById('confirmOk').textContent = okLabel || '删除';
  document.getElementById('confirmMask').classList.add('on');
}
function closeConfirm(){ document.getElementById('confirmMask').classList.remove('on'); }
function okConfirm(){
  closeConfirm();
  if(typeof window.__confirmOK==='function'){ var f=window.__confirmOK; window.__confirmOK=null; f(); }
}

var __DATA__ = <<<JSON_DATA>>>;

function esc(s) {
  var d = document.createElement('div');
  d.appendChild(document.createTextNode(s||''));
  return d.innerHTML;
}

function renderDiaryText(text) {
  var escaped = esc(text).replace(/`([^`]+)`/g, '<span class="tag">$1</span>');
  // 如果开头是 💡,加特殊样式
  if(text.trim().startsWith('💡')) {
    return '<span class="idea-mark">'+escaped+'</span>';
  }
  return escaped;
}

function renderHome() {
  var d = __DATA__;
  var doneC = 0, totalC = 0;
  d.constraints.forEach(function(c){ if(c.done) doneC++; totalC++; });
  var todoTotal = 0;
  d.subsystems.forEach(function(s){ todoTotal += s.todos.length; });
  var diaryToday = null;
  d.diary.forEach(function(dd){ if(dd.date===d.today) diaryToday=dd; });
  var todayEntries = diaryToday ? diaryToday.entries : [];

  var diaryHtml = '';
  if(todayEntries.length===0) {
    diaryHtml = '<div class="empty-state">今天还没有写日记 📝</div>';
  } else {
    for(var i=0;i<todayEntries.length;i++) {
      var e = todayEntries[i];
      diaryHtml += '<div class="diary-item"><span class="diary-time">'+esc(e.time)+'</span><span class="diary-text">'+renderDiaryText(e.text)+'</span></div>';
    }
  }

  var shows = '';
  ['研究工作','学习成长','复盘进化'].forEach(function(name){
    var s = null;
    d.subsystems.forEach(function(x){ if(x.name===name) s=x; });
    if(!s||!s.exists) return;
    var todos = '';
    for(var i=0;i<Math.min(s.todos.length,3);i++) {
      todos += '<div class="sub-todo-item">· '+esc(s.todos[i])+'</div>';
    }
    shows += '<div class="sub-card"><div class="sub-name">'+esc(name)+'</div><div class="sub-duty">'+esc(s.duty)+'</div><div class="sub-todos">'+todos+'</div></div>';
  });

  return '<h1>👋 你好</h1><div class="date-badge">把人生当作系统来设计 · '+d.today+'</div>'+
    '<div class="card-row">'+
      '<div class="card"><div class="card-title">今日日记</div><div class="card-stat">'+todayEntries.length+'</div></div>'+
      '<div class="card"><div class="card-title">硬约束</div><div class="card-stat done">'+doneC+'<span style="font-size:14px;color:var(--text3)">/'+totalC+'</span></div></div>'+
      '<div class="card"><div class="card-title">进行中</div><div class="card-stat sub">'+todoTotal+'</div></div>'+
    '</div>'+
    '<h2>📝 今日日记</h2><div class="card">'+diaryHtml+'</div>'+
    '<h2>📋 各子系统</h2><div class="card">'+shows+'</div>';
}

function renderDiary() {
  var d = __DATA__;
  var items = '';
  for(var i=0;i<d.diary.length;i++) {
    var day = d.diary[i];
    items += '<div class="diary-date-hdr">📅 '+day.short+'</div>';
    for(var j=0;j<day.entries.length;j++) {
      var e = day.entries[j];
      items += '<div class="diary-item" style="display:flex;align-items:flex-start">'+
        '<span style="flex:1;min-width:0"><span class="diary-time">'+esc(e.time)+'</span>'+
        '<span class="diary-text">'+renderDiaryText(e.text)+'</span></span>'+
        '<span class="edit-btns"><button class="mini-btn" onclick="diaryEdit(\''+day.date+'\','+j+',this)">✏️</button>'+
        '<button class="mini-btn" onclick="diaryDel(\''+day.date+'\','+j+',this)">🗑</button></span></div>';
    }
  }
  if(!items) items = '<div class="empty-state">还没有日记记录 📝</div>';
  return '<h1>📖 日记</h1><div class="card">'+items+'</div>';
}

function renderKanban() {
  var d = __DATA__;
  var constHtml = '';
  d.constraints.forEach(function(c, i){
    constHtml += '<div class="const-item"><div class="const-check'+(c.done?' done':'')+'">'+(c.done?'✓':'')+'</div>'+
      '<div class="const-text">'+esc(c.text)+'</div>'+
      '<span class="edit-btns"><button class="mini-btn" onclick="consEdit('+i+')">✏️</button>'+
      '<button class="mini-btn" onclick="consDel('+i+',this)">🗑</button></span></div>';
  });
  if(!constHtml) constHtml = '<div class="empty-state">还没有硬约束</div>';
  constHtml += '<button class="add-btn" onclick="consAdd()">+ 添加硬约束</button>';

  var subHtml = '';
  d.subsystems.forEach(function(s){
    if(!s.exists) return;
    var todos = '';
    if(s.todos.length) {
      s.todos.forEach(function(t, ti){
        todos += '<div class="sub-todo-item" style="display:flex;align-items:center">· '+esc(t)+
          '<span class="edit-btns"><button class="mini-btn" onclick="todoEdit(\''+esc(s.name)+'\','+ti+',this)">✏️</button>'+
          '<button class="mini-btn" onclick="todoDel(\''+esc(s.name)+'\','+ti+',this)">🗑</button></span></div>';
      });
    } else {
      todos = '<div class="sub-todo-item" style="color:var(--text3)">暂无进行中事项</div>';
    }
    todos += '<button class="add-btn" onclick="todoAdd(\''+esc(s.name)+'\')">+ 添加事项</button>';
    subHtml += '<div class="sub-card"><div class="sub-name">'+esc(s.name)+'</div><div class="sub-duty">'+esc(s.duty)+'</div><div class="sub-todos">'+todos+'</div></div>';
  });

  return '<h1>📋 看板</h1><h2>🎯 硬约束</h2><div class="card">'+constHtml+'</div>'+
    '<h2>📂 子系统</h2><div class="card">'+subHtml+'</div>'+
    renderJianwen()+renderCandidates();
}

function renderJianwen() {
  var d = __DATA__;
  var jw = d.jianwen||[];
  var pending = jw.filter(function(x){ return x.status==='pending'; });
  var done = jw.filter(function(x){ return x.status!=='pending'; });
  var typeLabel = {video:'🎬 视频', inspiration:'💡 灵感'};
  var rows = '';
  pending.forEach(function(it){
    var tl = typeLabel[it.type]||it.type;
    var badge = (it.date && it.date!==d.today) ? '<span class="jw-date">'+esc(it.date.slice(5))+'</span>' : '';
    var url = it.source_url ? '<a class="jw-url" href="'+esc(it.source_url)+'" target="_blank">原链接 ↗</a>' : '';
    rows += '<div class="jw-item"><div class="jw-head"><span class="jw-type">'+esc(tl)+'</span>'+
      '<span class="jw-title">'+esc(it.title)+'</span>'+badge+'</div>'+
      '<div class="jw-summary">'+esc((it.summary||'').slice(0,200))+'</div>'+url+
      '<div class="jw-btns"><button class="jw-arch" onclick="jwAct(\'archive\','+it.id+',\''+esc(it.date||'')+'\',this)">📥 归档进知识库</button>'+
      '<button class="jw-disc" onclick="jwAct(\'discard\','+it.id+',\''+esc(it.date||'')+'\',this)">🗑 丢弃</button></div></div>';
  });
  if(!pending.length) rows = '<div class="empty-state">见闻池没有待筛选的条目 — 通过聊天机器人发链接/灵感会自动汇集到这</div>';
  var doneHtml = '';
  if(done.length){
    doneHtml = done.map(function(x){
      return '<div class="jw-done">'+(x.status==='archived'?'✅':'🗑')+' '+esc((x.date||'').slice(5))+' '+esc(typeLabel[x.type]||x.type)+' · '+esc(x.title)+'</div>';
    }).join('');
    doneHtml = '<div style="margin-top:10px;font-size:12px;color:var(--text3)">已处理 '+done.length+' 条</div>'+doneHtml;
  }
  return '<h2>📥 今日见闻池 · 待筛选 '+pending.length+' 条</h2><div class="card">'+rows+doneHtml+'</div>';
}

function renderCandidates() {
  var d = __DATA__;
  var cands = d.candidates||[];
  if(!cands.length) return '';
  var srcLabel = {'踩坑':'⚠️ 踩坑','周复盘':'📅 周复盘','手动':'✍️ 手动'};
  var rows = cands.map(function(c){
    return '<div class="cand-item" id="cand-'+esc(c.id)+'"><div class="cand-head">'+
      '<span class="cand-src">'+esc(srcLabel[c.source]||c.source)+'</span>'+
      '<span class="cand-title">'+esc(c.title)+'</span></div>'+
      '<div class="cand-body">'+esc((c.body||'').slice(0,500))+'</div>'+
      '<div class="cand-btns"><button class="cand-approve" onclick="candAct(\'approve\',\''+esc(c.id)+'\',this)">✅ 批准进规则库</button>'+
      '<button class="cand-disc" onclick="candAct(\'discard\',\''+esc(c.id)+'\',this)">🗑 丢弃</button></div></div>';
  }).join('');
  return '<h2>📝 候选规则 · 待你点头 '+cands.length+' 条</h2><div class="card cand-card">'+rows+'</div>';
}

function renderRules() {
  var d = __DATA__;
  var items = '';
  d.rules.forEach(function(r, i){
    items += '<div class="rule-item"><div class="rule-title"><span class="rid">R'+esc(r.n)+'</span>— '+esc(r.title)+
      '<span class="edit-btns"><button class="mini-btn" onclick="ruleEdit('+i+')">✏️</button>'+
      '<button class="mini-btn" onclick="ruleDel('+i+',this)">🗑</button></span></div>'+
      '<div class="rule-body">'+esc(r.body)+'</div></div>';
  });
  if(!items) items = '<div class="empty-state">还没有规则</div>';
  items += '<button class="add-btn" onclick="ruleAdd()">+ 添加规则</button>';
  return '<h1>📜 规则库</h1><div class="card">'+items+'</div>';
}

function renderAi() {
  return '<h1>🤖 请 AI 帮我一把</h1>'+
    '<div class="date-badge">结果自动写回项目文件,刷新可见</div>'+
    '<button class="ai-btn" onclick="openAi(\'review\')"><div class="ti">🌱 本周复盘</div><div class="de">填 Verify → AI 做反思+下周计划,自动写回</div></button>'+
    '<button class="ai-btn" onclick="openAi(\'pit\')"><div class="ti">🪨 踩坑记一笔</div><div class="de">描述坑 → AI 判断要不要入规则库</div></button>'+
    '<button class="ai-btn" onclick="openAi(\'learn\')"><div class="ti">📖 沉淀知识</div><div class="de">粘一段文字/文件路径 → 结构化存知识库</div></button>'+
    '<button class="ai-btn" onclick="openAi(\'checkup\')"><div class="ti">🩺 月度体检</div><div class="de">无需输入 → AI 看系统该保持/解封/简化</div></button>';
}

function renderWrite() {
  return '<h1>✏️ 快速记录</h1>'+
    '<div class="date-badge">灵感?文本开头加 💡 就行</div>'+
    '<div class="card input-card">'+
      '<textarea id="entryText" placeholder="写点什么…灵感开头加💡" rows="5"></textarea>'+
      '<div class="quick-btns">'+
        '<button class="quick-btn" data-quick="💡 ">💡 灵感</button>'+
        '<button class="quick-btn" data-quick="今天做了什么">今天做了什么</button>'+
        '<button class="quick-btn" data-quick="学到了什么">学到了什么</button>'+
        '<button class="quick-btn" data-quick="遇到了什么问题">遇到了什么问题</button>'+
      '</div>'+
      '<button class="send-btn" id="sendBtn">发送</button>'+
      '<div class="result-msg" id="resultMsg"></div>'+
    '</div>';
}

function switchTab(tabId) {
  document.querySelectorAll('.tab-content').forEach(function(el){ el.classList.remove('active'); });
  document.querySelectorAll('.tab').forEach(function(el){ el.classList.remove('active'); });
  var content = document.getElementById('tab' + tabId.charAt(0).toUpperCase() + tabId.slice(1));
  if(content) content.classList.add('active');
  document.querySelector('.tab[data-tab="'+tabId+'"]').classList.add('active');
  renderContent(tabId);
}

function renderContent(tabId) {
  var el = document.getElementById('tab' + tabId.charAt(0).toUpperCase() + tabId.slice(1));
  if(!el) return;
  var fn = {home:renderHome, write:renderWrite, diary:renderDiary, kanban:renderKanban, rules:renderRules, ai:renderAi}[tabId];
  if(!fn) return;
  el.innerHTML = '<div class="scroll-wrap">'+fn()+'</div>';
  bindEvents(tabId);
}

function bindEvents(tabId) {
  if(tabId==='write') {
    var sendBtn = document.getElementById('sendBtn');
    if(sendBtn) {
      sendBtn.onclick = function() {
        var ta = document.getElementById('entryText');
        var text = ta.value.trim();
        if(!text) return;
        sendBtn.disabled = true;
        sendBtn.textContent = '发送中…';
        var msg = document.getElementById('resultMsg');
        fetch(API_WRITE, {
          method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({text:text, type:'diary'})
        })
        .then(function(r){ return r.json(); })
        .then(function(data){
          if(data.ok) {
            msg.className = 'result-msg ok';
            msg.textContent = '✅ 已记录';
            ta.value = '';
            refreshData();
          } else {
            msg.className = 'result-msg err';
            msg.textContent = (data.error||'保存失败');
          }
        })
        .catch(function(e){
          msg.className = 'result-msg err';
          msg.textContent = '网络错误';
        })
        .then(function(){
          sendBtn.disabled = false;
          sendBtn.textContent = '发送';
          setTimeout(function(){ msg.className='result-msg'; }, 3000);
        });
      };
    }
    document.querySelectorAll('.quick-btn').forEach(function(btn){
      btn.onclick = function() {
        var ta = document.getElementById('entryText');
        if(ta) {
          var quick = this.dataset.quick;
          // 如果快捷文本是 "💡 " 就插入光标位置,其它的覆盖
          if(quick === '💡 ') {
            var start = ta.selectionStart;
            ta.value = ta.value.slice(0, start) + quick + ta.value.slice(start);
            ta.selectionStart = ta.selectionEnd = start + quick.length;
          } else {
            ta.value = quick;
          }
          ta.focus();
        }
      };
    });
  }
}

function refreshData() {
  fetch(API_DATA).then(function(r){ return r.json(); }).then(function(data){
    __DATA__ = data;
    var activeTab = document.querySelector('.tab.active');
    if(activeTab) renderContent(activeTab.dataset.tab);
  }).catch(function(e){});
}

// ── 硬约束 编辑/删除/新增 ──
function consEdit(i){
  var c = __DATA__.constraints[i];
  openEdit('编辑硬约束', c.text, true, c.done, function(text, done){
    return apiPost('/api/constraint', {action:'update', index:i, text:text, done:done});
  });
}
function consAdd(){
  openEdit('添加硬约束', '', true, false, function(text, done){
    return apiPost('/api/constraint', {action:'add', text:text, done:done});
  });
}
function consDel(i, el){
  var t = __DATA__.constraints[i] ? __DATA__.constraints[i].text : '';
  confirmDialog('删除这条硬约束?\n\n「'+t+'」', function(){
    apiPost('/api/constraint', {action:'delete', index:i}).then(afterWrite);
  });
}
// ── 规则 编辑/删除/新增 ──
function ruleEdit(i){
  apiPost('/api/rule', {action:'get', index:i}).then(function(d){
    if(d.error){ showToast(d.error); return; }
    openEdit('编辑规则', d.text, false, false, function(text){
      return apiPost('/api/rule', {action:'update', index:i, text:text});
    });
  });
}
function ruleAdd(){
  openEdit('添加规则', '## R — \n- 触发:\n- 类型:\n- 规则:\n- 日期:'+__DATA__.today, false, false, function(text){
    return apiPost('/api/rule', {action:'add', text:text});
  });
}
function ruleDel(i, el){
  var r = __DATA__.rules[i];
  var t = r ? ('R'+r.n+' — '+r.title) : '';
  confirmDialog('删除这条规则?\n\n「'+t+'」', function(){
    apiPost('/api/rule', {action:'delete', index:i}).then(afterWrite);
  });
}
// ── 进行中事项 编辑/删除/新增 ──
function todoEdit(sub, i, el){
  var text = el.closest('.sub-todo-item').firstChild.textContent.replace(/^·\s*/,'').trim();
  openEdit('编辑进行中事项', text, false, false, function(t){
    return apiPost('/api/todo', {action:'update', subsystem:sub, index:i, text:t});
  });
}
function todoAdd(sub){
  openEdit('添加进行中事项', '', false, false, function(t){
    return apiPost('/api/todo', {action:'add', subsystem:sub, text:t});
  });
}
function todoDel(sub, i, el){
  var t = el.closest('.sub-todo-item').firstChild.textContent.replace(/^·\s*/,'').trim();
  confirmDialog('删除这条进行中事项?\n\n「'+t+'」', function(){
    apiPost('/api/todo', {action:'delete', subsystem:sub, index:i}).then(afterWrite);
  });
}
// ── 见闻池 归档/丢弃 ──
function jwAct(action, id, date, el){
  if(action==='discard'){
    confirmDialog('丢弃这条见闻?丢了不进知识库。', function(){ jwRun(action, id, date, el); });
  } else {
    jwRun(action, id, date, el);
  }
}
function jwRun(action, id, date, el){
  el.disabled=true; el.textContent='处理中…';
  apiPost('/api/jianwen', {action:action, id:id, date:date}).then(function(d){
    if(d.ok){ refreshData(); } else { showToast(d.error||'操作失败'); el.disabled=false; }
  }).catch(function(e){ showToast('请求失败:'+e); el.disabled=false; });
}
// ── 候选规则 批准/丢弃 ──
function candAct(action, id, el){
  if(action==='discard'){
    confirmDialog('丢弃这条候选规则?丢了不进规则库。', function(){ candRun(action, id, el); });
  } else {
    candRun(action, id, el);
  }
}
function candRun(action, id, el){
  el.disabled=true; el.textContent='处理中…';
  apiPost('/api/candidate', {action:action, id:id}).then(function(d){
    if(d.error){ showToast(d.error); el.disabled=false; return; }
    refreshData();
  }).catch(function(e){ showToast('请求失败:'+e); el.disabled=false; });
}
// ── 日记 逐条编辑/删除 ──
function diaryEdit(date, idx, el){
  var text = el.closest('.diary-item').querySelector('.diary-text').textContent.trim();
  openEdit('编辑日记 · '+date.slice(5), text, false, false, function(t){
    return apiPost('/api/diary', {action:'edit', date:date, index:idx, text:t});
  });
}
function diaryDel(date, idx, el){
  var t = el.closest('.diary-item').querySelector('.diary-text').textContent.trim();
  confirmDialog('删除这条日记?\n\n「'+t.slice(0,60)+(t.length>60?'...':'')+'」', function(){
    apiPost('/api/diary', {action:'delete', date:date, index:idx}).then(afterWrite);
  });
}
// ── 通用编辑弹窗 ──
function openEdit(title, text, showDone, done, onSave){
  window.__editSave = onSave;
  document.getElementById('editTitle').textContent = title;
  document.getElementById('editInput').value = text;
  document.getElementById('editDoneLab').style.display = showDone ? 'flex' : 'none';
  document.getElementById('editDone').checked = !!done;
  document.getElementById('editMask').classList.add('on');
  setTimeout(function(){ document.getElementById('editInput').focus(); }, 50);
}
function closeEdit(){ document.getElementById('editMask').classList.remove('on'); }
function saveEdit(){
  var text = document.getElementById('editInput').value.trim();
  if(!text){ showToast('内容不能为空'); return; }
  var done = document.getElementById('editDone').checked;
  window.__editSave(text, done).then(function(d){
    if(d.error){ showToast(d.error); return; }
    closeEdit(); afterWrite(d);
  }).catch(function(e){ showToast('请求失败:'+e); });
}
function afterWrite(d){ if(d && d.error){ showToast(d.error); return; } refreshData(); }
// ── AI 功能弹窗 ──
var AI_META = {
  review:{title:'🌱 本周复盘', input:true, ph:'逐条写:上周每个子系统定的事做到没?没做到一句话原因。'},
  pit:{title:'🪨 踩坑记一笔', input:true, ph:'描述你踩的坑(具体场景)。AI 判断是否该入规则库。'},
  learn:{title:'📖 沉淀知识', input:true, ph:'粘贴要沉淀的一段文字,或一个文件路径。'},
  checkup:{title:'🩺 月度体检', input:false, ph:''}
};
function openAi(mode){
  window.__aiMode = mode; var m = AI_META[mode];
  document.getElementById('aiTitle').textContent = m.title;
  document.getElementById('aiInputWrap').style.display = m.input ? 'block':'none';
  document.getElementById('aiInput').value = '';
  document.getElementById('aiInput').placeholder = m.ph;
  document.getElementById('aiOut').style.display = 'none';
  document.getElementById('aiSpin').style.display = 'none';
  document.getElementById('aiSaved').textContent = '';
  document.getElementById('aiRun').disabled = false;
  document.getElementById('aiMask').classList.add('on');
}
function closeAi(){ document.getElementById('aiMask').classList.remove('on'); if(window.__aiDirty){ window.__aiDirty=false; refreshData(); } }
function runAi(){
  var run=document.getElementById('aiRun'), spin=document.getElementById('aiSpin');
  var out=document.getElementById('aiOut'), saved=document.getElementById('aiSaved');
  run.disabled=true; spin.style.display='block'; out.style.display='none'; saved.textContent='';
  apiPost('/api/ai', {mode:window.__aiMode, input:document.getElementById('aiInput').value}).then(function(d){
    spin.style.display='none'; out.style.display='block';
    if(d.error){ out.textContent='⚠ '+d.error; }
    else { out.textContent=d.text||'(无输出)'; if(d.saved){ saved.textContent='✅ 已写入:'+d.saved; window.__aiDirty=true; } }
    run.disabled=false;
  }).catch(function(e){ spin.style.display='none'; out.style.display='block'; out.textContent='⚠ 请求失败:'+e; run.disabled=false; });
}
// 点遮罩关闭弹窗
['editMask','aiMask','confirmMask'].forEach(function(id){
  document.getElementById(id).addEventListener('click', function(e){
    if(e.target===this){
      if(id==='editMask') closeEdit();
      else if(id==='aiMask') closeAi();
      else closeConfirm();
    }
  });
});

// 底部导航
document.getElementById('tabBar').addEventListener('click', function(e) {
  var tab = e.target.closest('.tab');
  if(!tab) return;
  e.preventDefault();
  switchTab(tab.dataset.tab);
});

// 初始渲染
switchTab('home');
</script>
</body>
</html>"""

def render_page(data):
    data_json = json.dumps(data, ensure_ascii=False)
    page = PAGE_TPL.replace("<<<JSON_DATA>>>", data_json)
    return page

# ── HTTP 服务 ──────────────────────────────────

class MobileHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            try:
                page = render_page(_get_data())
            except Exception as e:
                page = "<h1>出错</h1><pre>" + html_mod.escape(str(e)) + "</pre>"
            self._send(200, page, "text/html; charset=utf-8")
        elif parsed.path == "/api/data":
            try:
                self._send(200, json.dumps(_get_data(), ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False))
        elif parsed.path == "/health":
            self._send(200, json.dumps({"status": "ok"}))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/write":
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
                # 灵感已合并进日记,统一走 diary 端点
                r = append_diary_with_ai(text)
                if r.get("ok"):
                    self._send(200, json.dumps(r, ensure_ascii=False))
                else:
                    self._send(500, json.dumps(r, ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

def serve():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), MobileHandler)
    print("")
    print("=" * 50)
    print("  个人成长 · 手机端 Web UI")
    print("=" * 50)
    print("  本地:   http://0.0.0.0:{}".format(PORT))
    print("  公网:   http://<你的服务器IP>:{}".format(PORT))
    print("  端口:   {}".format(PORT))
    print("=" * 50)
    print()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已关闭。")
        server.server_close()

if __name__ == "__main__":
    serve()
