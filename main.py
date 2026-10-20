#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A 股事件雷达：抓公告 + 快讯，按事件类型分级推送到微信。

设计原则：
  1. 不做代码过滤，只做事件匹配——命中即报，不预设自选池
  2. 任何单源失败都不影响主流程，最终 exit 0
  3. 所有状态落 data/state.json，GitHub Actions 提交回仓库

用法：
  python main.py                 # 正常跑一次
  python main.py --dry-run       # 抓取+匹配，但不推送
  python main.py --offline       # 用内置样本验证逻辑，不联网
  python main.py --reset         # 清空状态
  python main.py --flush         # 强制把 P1 缓冲区推出
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

import config as C

BJ = timezone(timedelta(hours=8))
BASE_DIR = Path(__file__).resolve().parent
STATE_PATH = BASE_DIR / "data" / "state.json"

CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
NAME_RE = re.compile(r"^【?([\u4e00-\u9fa5A-Za-z]{2,8})】?[：:]")


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def now_bj() -> datetime:
    return datetime.now(BJ)


def log(msg: str) -> None:
    print(f"[{now_bj():%m-%d %H:%M:%S}] {msg}", flush=True)


def fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, BJ).strftime("%m-%d %H:%M")


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------

def default_state() -> dict:
    return {
        "last_run_ts": 0,
        "seen_ann": {},        # announcementId -> ts
        "seen_tel": {},        # telegraph id -> ts
        "stock_event": {},     # "600825|重组" -> ts
        "p1_buffer": [],       # 待合并的 P1 记录
        "p1_runs": 0,          # 运行计数器，用于每 N 次合并
        "carry": [],           # 超限后结转次日的 P1
        "daily_date": "",
        "daily_count": 0,
    }


def load_state() -> dict:
    if not STATE_PATH.exists():
        return default_state()
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        log(f"状态文件损坏，重建：{exc}")
        return default_state()
    state = default_state()
    state.update(data or {})
    return state


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, STATE_PATH)


def cleanup(state: dict, now: datetime) -> None:
    """滚动清理过期 ID，避免 state.json 无限膨胀。"""
    cutoff = int(now.timestamp()) - C.SEEN_TTL_DAYS * 86400
    for key in ("seen_ann", "seen_tel"):
        state[key] = {k: v for k, v in state[key].items() if v >= cutoff}
    dedup_cutoff = int(now.timestamp()) - C.DEDUP_HOURS * 3600
    state["stock_event"] = {k: v for k, v in state["stock_event"].items() if v >= dedup_cutoff}


# ---------------------------------------------------------------------------
# 匹配
# ---------------------------------------------------------------------------

def _match(rule: dict, text: str) -> bool:
    if any(k in text for k in rule.get("exclude") or []):
        return False
    kw_hit = any(k in text for k in rule.get("keywords") or [])
    pair_hit = any(a in text and b in text for a, b in rule.get("pairs") or [])
    if not (kw_hit or pair_hit):
        return False
    req = rule.get("require_any") or []
    if req and not any(k in text for k in req):
        return False
    return True


def classify(text: str) -> tuple[str, str] | None:
    """返回 (level, tag)。P0 优先，命中 P0 不再看噪音黑名单。"""
    if not text:
        return None
    for rule in C.P0_RULES:
        if _match(rule, text):
            return "P0", rule["tag"]
    if any(k in text for k in C.NOISE_KEYWORDS):
        return None
    for rule in C.P1_RULES:
        if _match(rule, text):
            return "P1", rule["tag"]
    return None


def extract_stock(text: str) -> tuple[str, str]:
    """从文本里抠股票代码和名称，抠不到就返回空串。"""
    code = ""
    m = CODE_RE.search(text)
    if m:
        code = m.group(1)
    name = ""
    m2 = NAME_RE.match(text.strip())
    if m2:
        name = m2.group(1)
    return code, name


# ---------------------------------------------------------------------------
# 数据源
# ---------------------------------------------------------------------------

def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": C.USER_AGENT,
        "Accept": "application/json, text/plain, */*",
    })
    return s


def fetch_cninfo(session: requests.Session, since_ts: int, now: datetime) -> list[dict]:
    """巨潮全市场公告。接口实际收 form 表单，不是 JSON。"""
    since_dt = datetime.fromtimestamp(since_ts, BJ)
    se_date = f"{since_dt:%Y-%m-%d}~{now:%Y-%m-%d}"
    out: list[dict] = []

    for column in C.CNINFO_COLUMNS:
        page = 1
        while page <= C.MAX_PAGES:
            payload = {
                "pageNum": page,
                "pageSize": C.PAGE_SIZE,
                "column": column,
                "tabName": "fulltext",
                "plate": "",
                "stock": "",
                "searchkey": "",
                "secid": "",
                "category": "",
                "trade": "",
                "seDate": se_date,
                "sortName": "",
                "sortType": "",
                "isHLtitle": "true",
            }
            try:
                resp = session.post(
                    C.CNINFO_URL,
                    data=payload,
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                        "Referer": "http://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice",
                        "X-Requested-With": "XMLHttpRequest",
                    },
                    timeout=C.REQUEST_TIMEOUT,
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                if page == 1:
                    log(f"巨潮[{column}] 失败：{exc}")
                break

            rows = data.get("announcements") or []
            if not rows:
                break

            # 结果按时间倒序，本页最新一条都早于窗口，后面只会更早，直接停
            page_ts = [int(r.get("announcementTime") or 0) for r in rows]
            page_ts = [t // 1000 if t > 10 ** 11 else t for t in page_ts]
            if page_ts and max(page_ts) < since_ts:
                break

            for row in rows:
                ts = int(row.get("announcementTime") or 0)
                if ts > 10 ** 11:
                    ts //= 1000
                if ts and ts < since_ts:
                    continue
                ann_id = str(row.get("announcementId") or "")
                if not ann_id:
                    continue
                adjunct = row.get("adjunctUrl") or ""
                out.append({
                    "source": "cninfo",
                    "id": ann_id,
                    "code": str(row.get("secCode") or ""),
                    "name": str(row.get("secName") or ""),
                    "title": str(row.get("announcementTitle") or "").strip(),
                    "url": ("http://static.cninfo.com.cn/" + adjunct) if adjunct else "",
                    "ts": ts or int(now.timestamp()),
                })

            if not data.get("hasMore"):
                break
            page += 1

    log(f"巨潮公告 {len(out)} 条（窗口 {since_dt:%m-%d %H:%M} 起）")
    return out


def fetch_cls(session: requests.Session, since_ts: int) -> list[dict]:
    """财联社电报缓存接口。"""
    try:
        resp = session.get(
            C.CLS_URL,
            params=C.CLS_PARAMS,
            headers={"Referer": "https://www.cls.cn/telegraph"},
            timeout=C.REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log(f"财联社失败：{exc}")
        return []

    block = data.get("data") or {}
    rows = block.get("roll_data") or []
    out: list[dict] = []
    for row in rows:
        ts = int(row.get("ctime") or 0)
        if ts and ts < since_ts:
            continue
        title = str(row.get("title") or "").strip()
        brief = str(row.get("brief") or "").strip()
        text = title or brief
        if not text:
            continue
        code, name = extract_stock(f"{title} {brief}")
        out.append({
            "source": "cls",
            "id": str(row.get("id") or ""),
            "code": code,
            "name": name,
            "title": title or brief[:80],
            "text": f"{title} {brief}".strip(),
            "url": f"https://www.cls.cn/detail/{row.get('id')}" if row.get("id") else "",
            "ts": ts or int(time.time()),
        })

    log(f"财联社电报 {len(out)} 条")
    return out


# ---------------------------------------------------------------------------
# 推送
# ---------------------------------------------------------------------------

def push(title: str, content: str, dry_run: bool = False) -> bool:
    """按 PUSH_CHANNEL 分发。content 是纯文本，两个通道都能吃。"""
    if dry_run:
        log(f"[DRY-RUN] {title}")
        return True
    if C.PUSH_CHANNEL == "pushplus":
        return _push_pushplus(title, content)
    return _push_feishu(title, content)


def _push_feishu(title: str, content: str) -> bool:
    if not C.FEISHU_WEBHOOK:
        log("未配置 FEISHU_WEBHOOK，跳过推送")
        return False
    try:
        resp = requests.post(
            C.FEISHU_WEBHOOK,
            json={"msg_type": "text", "content": {"text": content}},
            timeout=C.REQUEST_TIMEOUT,
        )
        data = resp.json()
        # v2 接口返回 {"code":0,"msg":"success"}，老接口返回 {"StatusCode":0}
        code = data.get("code", data.get("StatusCode"))
        if code == 0:
            return True
        log(f"飞书返回异常：{data}")
    except Exception as exc:
        log(f"飞书推送失败：{exc}")
    return False


def _push_pushplus(title: str, content: str) -> bool:
    if not C.PUSHPLUS_TOKEN:
        log("未配置 PUSHPLUS_TOKEN，跳过推送")
        return False
    try:
        resp = requests.post(
            C.PUSHPLUS_URL,
            json={
                "token": C.PUSHPLUS_TOKEN,
                "title": title[:100],
                "content": content,
                "template": "txt",
            },
            timeout=C.REQUEST_TIMEOUT,
        )
        data = resp.json()
        if data.get("code") == 200:
            return True
        log(f"PushPlus 返回异常：{data}")
    except Exception as exc:
        log(f"PushPlus 推送失败：{exc}")
    return False


def p0_text(rec: dict) -> str:
    stock = rec.get("name") or rec.get("code") or "全市场"
    code = f"({rec['code']})" if rec.get("code") else ""
    src = "巨潮公告" if rec["source"] == "cninfo" else "财联社"
    lines = [
        f"【事件雷达】[P0·{rec['tag']}] {stock}{code}",
        rec["title"],
        f"{src} · {fmt_ts(rec['ts'])}",
    ]
    if rec.get("url"):
        lines.append(rec["url"])
    return "\n".join(lines)


def p1_text(items: list[dict], title: str) -> str:
    grouped: dict[str, list[dict]] = {}
    for it in items:
        grouped.setdefault(it["tag"], []).append(it)

    lines = [f"【事件雷达】{title}（{len(items)} 条）", "─" * 18]
    for tag, rows in grouped.items():
        lines.append(f"【{tag}】{len(rows)} 条")
        for i, r in enumerate(rows[:15], 1):
            stock = r.get("name") or r.get("code") or ""
            head = f"{stock} " if stock else ""
            lines.append(f"{i}. {head}{r['title']}")
            if r.get("url"):
                lines.append(f"   {r['url']}")
        if len(rows) > 15:
            lines.append(f"…… 另有 {len(rows) - 15} 条，见仓库 state.json")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run(dry_run: bool = False, offline: bool = False, force_flush: bool = False) -> int:
    now = now_bj()
    today = now.strftime("%Y-%m-%d")
    state = load_state()

    # 跨日重置计数
    if state.get("daily_date") != today:
        state["daily_date"] = today
        state["daily_count"] = 0

    # 昨日超限结转的 P1，开盘前补发
    if state.get("carry"):
        ok = push("事件雷达·昨日累积汇总", p1_text(state["carry"], "昨日累积"), dry_run)
        log(f"补发昨日累积 {len(state['carry'])} 条 {'成功' if ok else '失败'}")
        if ok or dry_run:
            state["carry"] = []

    # 抓取
    if offline:
        anns, tels = SAMPLE_ANN, SAMPLE_TEL
    else:
        session = _session()
        since = state.get("last_run_ts") or 0
        floor = int(now.timestamp()) - C.FIRST_RUN_LOOKBACK_MIN * 60
        since = max(since - C.OVERLAP_MIN * 60, floor) if since else floor
        anns = fetch_cninfo(session, since, now)
        tels = fetch_cls(session, since)

    # 分类 + 去重
    candidates: list[dict] = []
    now_ts = int(now.timestamp())
    for rec in anns + tels:
        result = classify(rec.get("text") or rec["title"])
        if not result:
            continue
        level, tag = result

        seen_key = "seen_ann" if rec["source"] == "cninfo" else "seen_tel"
        if rec["id"] and rec["id"] in state[seen_key]:
            continue

        ident = rec.get("code") or rec.get("name")
        event_key = f"{ident}|{tag}" if ident else ""
        if event_key and now_ts - state["stock_event"].get(event_key, 0) < C.DEDUP_HOURS * 3600:
            log(f"去重跳过：{event_key} {rec['title'][:30]}")
            if rec["id"]:
                state[seen_key][rec["id"]] = now_ts
            continue

        rec.update(level=level, tag=tag)
        candidates.append(rec)
        if rec["id"]:
            state[seen_key][rec["id"]] = now_ts
        if event_key:
            state["stock_event"][event_key] = now_ts

    p0 = [r for r in candidates if r["level"] == "P0"]
    p1 = [r for r in candidates if r["level"] == "P1"]
    log(f"命中 P0 {len(p0)} 条，P1 {len(p1)} 条")

    # P0 逐条推
    for rec in p0:
        if dry_run:
            push(f"[P0·{rec['tag']}] {rec.get('name') or rec.get('code')}", p0_text(rec), True)
            continue
        ok = push(
            f"[P0·{rec['tag']}] {rec.get('name') or rec.get('code') or '全市场'}",
            p0_text(rec),
        )
        if ok:
            state["daily_count"] += 1
        log(f"P0 推送 {'成功' if ok else '失败'}：{rec['title'][:40]}")

    # P1 进缓冲
    state["p1_buffer"].extend(p1)
    state["p1_runs"] = int(state.get("p1_runs", 0)) + 1

    # 超限后 P1 不再轮询推送，结转到次日
    over_limit = state["daily_count"] >= C.DAILY_LIMIT
    due = force_flush or (state["p1_runs"] % C.P1_BATCH_RUNS == 0)

    if state["p1_buffer"] and due:
        if over_limit:
            state["carry"].extend(state["p1_buffer"])
            log(f"已达当日上限 {C.DAILY_LIMIT}，{len(state['p1_buffer'])} 条 P1 结转次日")
            state["p1_buffer"] = []
        else:
            items = state["p1_buffer"]
            ok = push(
                f"事件雷达·{len(items)} 条汇总",
                p1_text(items, f"{now:%H:%M} 事件汇总"),
                dry_run,
            )
            log(f"P1 合并推送 {len(items)} 条 {'成功' if ok else '失败'}")
            if ok:
                state["daily_count"] += 1
            state["p1_buffer"] = []

    state["last_run_ts"] = now_ts
    cleanup(state, now)
    save_state(state)

    log(f"完成：P0 {len(p0)} / P1 {len(p1)} / 今日已推 {state['daily_count']} 条")
    return 0


# ---------------------------------------------------------------------------
# 离线样本（--offline 用，验证分类逻辑）
# ---------------------------------------------------------------------------

SAMPLE_ANN = [
    {"source": "cninfo", "id": "a1", "code": "600825", "name": "新华传媒",
     "title": "关于筹划重大资产重组事项的停牌公告", "url": "", "ts": int(time.time()) - 300},
    {"source": "cninfo", "id": "a2", "code": "600825", "name": "新华传媒",
     "title": "股票交易异常波动暨停牌核查公告", "url": "", "ts": int(time.time()) - 200},
    {"source": "cninfo", "id": "a3", "code": "000001", "name": "平安银行",
     "title": "关于召开2026年第三次临时股东大会的通知", "url": "", "ts": int(time.time()) - 100},
    {"source": "cninfo", "id": "a4", "code": "300750", "name": "宁德时代",
     "title": "关于签订重大合同的公告", "url": "", "ts": int(time.time()) - 90},
    {"source": "cninfo", "id": "a5", "code": "600000", "name": "浦发银行",
     "title": "关于2026年半年度业绩预增的公告", "url": "", "ts": int(time.time()) - 80},
]

SAMPLE_TEL = [
    {"source": "cls", "id": "t1", "code": "", "name": "",
     "title": "某公司连续涨停 录得5连板", "url": "", "ts": int(time.time()) - 60},
    {"source": "cls", "id": "t2", "code": "600825", "name": "新华传媒",
     "title": "新华传媒：筹划重大资产重组，明日起停牌", "url": "", "ts": int(time.time()) - 50},
]


def main() -> int:
    parser = argparse.ArgumentParser(description="A 股事件雷达")
    parser.add_argument("--dry-run", action="store_true", help="不推送，仅打印")
    parser.add_argument("--offline", action="store_true", help="用内置样本，不联网")
    parser.add_argument("--reset", action="store_true", help="清空状态")
    parser.add_argument("--flush", action="store_true", help="强制推送 P1 缓冲")
    args = parser.parse_args()

    if args.reset:
        if STATE_PATH.exists():
            STATE_PATH.unlink()
        log("状态已清空")
        return 0

    try:
        return run(dry_run=args.dry_run, offline=args.offline, force_flush=args.flush)
    except Exception as exc:
        log(f"运行异常（不影响 workflow）：{exc}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
