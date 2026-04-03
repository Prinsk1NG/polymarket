#!/usr/bin/env python3
"""
Polymarket Watch — 云端部署版 (高定排版版)
通过 Polymarket 官方 Gamma API + CLOB API 获取预测市场数据，
生成面向中国一级市场 AI 投资人的中文简报，推送至飞书 Webhook。
"""

import os
import sys
import json
import re
import time
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

# ─── 配置 ──────────────────────────────────────────────────────
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
HISTORY_FILE = Path(__file__).parent / "market_history.json"
OUTPUT_DIR = Path(__file__).parent / "briefings"
BJT = timezone(timedelta(hours=8))

FEISHU_WEBHOOK_URL = os.getenv("FEISHU_WEBHOOK_URL", "")

# ─── 关键词 ────────────────────────────────────────────────────
AI_KEYWORDS = [
    "AGI", "GPT", "Claude", "Gemini", "OpenAI", "Anthropic", "Google DeepMind",
    "AI regulation", "AI safety", "AI benchmark", "Turing test", "superintelligence",
    "AI bubble", "NVIDIA", "AI chip", "AI model", "AI legislation", "AI data center",
    "Mythos", "GPT-5", "GPT-6", "Claude 5", "Claude 4", "AI IMO",
]
CHINA_KEYWORDS = [
    "China GDP", "trade war", "tariff", "trade deal", "semiconductor", "chip ban",
    "Huawei", "BYD", "yuan", "RMB", "PBOC", "property market", "Chinese stocks",
    "China economy", "China trade", "rare earth", "TikTok", "China EV",
]
# 政治敏感过滤
BLOCKED_PATTERNS = [
    r"taiwan.*invasi", r"invad.*taiwan", r"xi jinping.*removed",
    r"china.*coup", r"regime.*change.*china", r"CCP.*collapse",
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("polymarket-watch")


# ════════════════════════════════════════════════════════════════
# 1. API 获取与过滤逻辑 (保持你原有的扎实逻辑不变)
# ════════════════════════════════════════════════════════════════

def gamma_get(endpoint: str, params: dict | None = None, retries: int = 3) -> list | dict:
    url = f"{GAMMA_API}/{endpoint}"
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.warning(f"Gamma API {endpoint} attempt {attempt+1} failed: {e}")
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    return []

def fetch_all_active_markets(limit: int = 100, max_pages: int = 10) -> list:
    all_markets = []
    for page in range(max_pages):
        offset = page * limit
        batch = gamma_get("markets", params={
            "limit": limit, "offset": offset, "active": "true",
            "closed": "false", "order": "volume24hr", "ascending": "false",
        })
        if not isinstance(batch, list):
            batch = batch.get("data", batch.get("markets", []))
        if not batch: break
        all_markets.extend(batch)
        if len(batch) < limit: break
        time.sleep(0.3)
    return all_markets

def filter_relevant_markets(markets: list) -> dict:
    ai_markets, china_markets = [], []
    for m in markets:
        text = f"{m.get('question', '')} {m.get('description', '')} {m.get('groupItemTitle', '')}".lower()
        if any(re.search(pat, text, re.I) for pat in BLOCKED_PATTERNS): continue
        if any(kw.lower() in text for kw in AI_KEYWORDS): ai_markets.append(m)
        if any(kw.lower() in text for kw in CHINA_KEYWORDS): china_markets.append(m)
    return {"ai": ai_markets, "china": china_markets}

def clob_get(endpoint: str, params: dict | None = None) -> dict | list:
    try:
        r = requests.get(f"{CLOB_API}/{endpoint}", params=params, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning(f"CLOB API {endpoint} failed: {e}")
        return {}

def get_midpoint(token_id: str) -> float | None:
    data = clob_get("midpoint", params={"token_id": token_id})
    return float(data["mid"]) if data.get("mid") is not None else None

def enrich_with_prices(markets: list) -> list:
    for m in markets:
        prices_str = m.get("outcomePrices", "")
        if isinstance(prices_str, str) and prices_str:
            try:
                prices = json.loads(prices_str)
                m["_parsed_prices"] = [float(p) for p in prices]
                m["_yes_price"] = m["_parsed_prices"][0] if m["_parsed_prices"] else None
            except (json.JSONDecodeError, ValueError):
                m["_yes_price"] = None
        elif isinstance(prices_str, list):
            m["_parsed_prices"] = [float(p) for p in prices_str]
            m["_yes_price"] = m["_parsed_prices"][0] if prices_str else None
        else:
            m["_yes_price"] = None

        if m.get("_yes_price") is None:
            token_ids_str = m.get("clobTokenIds", "")
            if isinstance(token_ids_str, str) and token_ids_str:
                try: token_ids = json.loads(token_ids_str)
                except (json.JSONDecodeError, ValueError): token_ids = []
            elif isinstance(token_ids_str, list): token_ids = token_ids_str
            else: token_ids = []
            
            if token_ids:
                mid = get_midpoint(token_ids[0])
                if mid is not None: m["_yes_price"] = mid
                time.sleep(0.15)
    return markets


# ════════════════════════════════════════════════════════════════
# 2. 历史数据状态机
# ════════════════════════════════════════════════════════════════

def load_history() -> dict:
    if HISTORY_FILE.exists():
        try: return json.loads(HISTORY_FILE.read_text("utf-8"))
        except: pass
    return {"last_updated": "", "markets": {}}

def save_history(history: dict):
    HISTORY_FILE.write_text(json.dumps(history, ensure_ascii=False, indent=2), "utf-8")

def make_slug(m: dict) -> str:
    question = m.get("question", m.get("groupItemTitle", ""))
    slug = re.sub(r'[^a-z0-9]+', '-', question.lower()).strip('-')[:80]
    return slug or m.get("conditionId", "unknown")

def compare_with_history(markets: list, history: dict, now_str: str) -> list:
    for m in markets:
        slug = make_slug(m)
        m["_slug"] = slug
        price = m.get("_yes_price")
        if price is None:
            m["_change"] = None
            continue

        prev = history.get("markets", {}).get(slug)
        if prev and prev.get("current_probability") is not None:
            m["_change"] = round(price - prev["current_probability"], 4)
            m["_prev_prob"] = prev["current_probability"]
        else:
            m["_change"] = None
            m["_is_new"] = True

        hist_entry = history.get("markets", {}).get(slug, {})
        hist_history = hist_entry.get("history", [])
        hist_history.append({"date": now_str, "probability": price})
        
        history.setdefault("markets", {})[slug] = {
            "name": m.get("question", m.get("groupItemTitle", "")),
            "url": f"https://polymarket.com/event/{m.get('slug', slug)}",
            "category": "ai" if m.get("_cat") == "ai" else "china",
            "current_probability": price,
            "previous_probability": m.get("_prev_prob", price),
            "first_seen": hist_entry.get("first_seen", now_str[:10]),
            "history": hist_history[-30:],
        }
    return markets

# ════════════════════════════════════════════════════════════════
# 3. UI 渲染引擎：动态颜色与富文本卡片
# ════════════════════════════════════════════════════════════════

def pct(v: float | None) -> str:
    return f"{v*100:.1f}%" if v is not None else "N/A"

def format_trend(c: float | None) -> str:
    """带颜色的涨跌幅格式化（适用飞书卡片）"""
    if c is None: return "<font color='grey'>新开盘</font>"
    pp = c * 100
    if pp >= 1.0: return f"<font color='red'>🔥 +{pp:.1f}pp</font>"
    if pp <= -1.0: return f"<font color='green'>🍃 {pp:.1f}pp</font>"
    if pp > 0: return f"<font color='grey'>+{pp:.1f}pp</font>"
    return f"<font color='grey'>{pp:.1f}pp</font>"

def push_feishu_interactive(ai_markets: list, china_markets: list, now: datetime, doc_url: str = ""):
    if not FEISHU_WEBHOOK_URL: return
    date_str = now.strftime("%Y-%m-%d %H:%M")

    def sort_key(m): return abs(m.get("_change") or 0)
    ai_sorted = sorted(ai_markets, key=sort_key, reverse=True)
    china_sorted = sorted(china_markets, key=sort_key, reverse=True)
    
    all_sorted = sorted(ai_markets + china_markets, key=sort_key, reverse=True)
    significant = [m for m in all_sorted if m.get("_change") is not None and abs(m["_change"]) >= 0.03]
    danger_consensus = [m for m in all_sorted if m.get("_yes_price") is not None and (m["_yes_price"] > 0.90 or m["_yes_price"] < 0.10)]

    elements = []

    # 🎯 今日核心异动
    elements.append({"tag": "markdown", "content": "**▌ ⚡️ 核心异动 (Top Movers)**"})
    top_movers_md = ""
    if significant:
        for i, m in enumerate(significant[:3], 1):
            name = m.get("question", m.get("groupItemTitle", "Unknown"))
            top_movers_md += f"👉 **{name}**\n当前概率: **{pct(m.get('_yes_price'))}** | 变动: {format_trend(m.get('_change'))}\n\n"
    else:
        top_movers_md = "<font color='grey'>过去 24 小时大盘情绪稳定，无显著资金博弈。</font>\n"
    elements.append({"tag": "markdown", "content": top_movers_md.strip()})
    elements.append({"tag": "hr"})

    # ⚠️ 共识极值区预警
    if danger_consensus:
        elements.append({"tag": "markdown", "content": "**▌ ⚠️ 共识极值预警 (Danger Zones)**\n<font color='grey'>当前处于 >90% 或 <10% 的高风险共识区</font>"})
        danger_md = ""
        for m in danger_consensus[:3]:
            name = m.get("question", m.get("groupItemTitle", ""))
            direction = "极端看多" if m.get("_yes_price", 0) > 0.9 else "极端看空"
            danger_md += f"🎯 **{name}**\n<font color='red'>当前极值: {pct(m.get('_yes_price'))} ({direction})</font>\n\n"
        elements.append({"tag": "markdown", "content": danger_md.strip()})
        elements.append({"tag": "hr"})

    # 🤖 AI 赛道追踪
    elements.append({"tag": "markdown", "content": "**▌ 🤖 AI 赛道大盘**"})
    if ai_sorted:
        ai_md = ""
        for m in ai_sorted[:5]:
            name = m.get("question", m.get("groupItemTitle", ""))
            ai_md += f"- **{name}**\n  {pct(m.get('_yes_price'))} ({format_trend(m.get('_change'))})\n"
        elements.append({"tag": "markdown", "content": ai_md.strip()})
    else:
        elements.append({"tag": "markdown", "content": "<font color='grey'>暂无活跃盘口</font>"})
    elements.append({"tag": "hr"})

    # 🇨🇳 中国宏观追踪
    elements.append({"tag": "markdown", "content": "**▌ 🇨🇳 中国宏观与出海**"})
    if china_sorted:
        china_md = ""
        for m in china_sorted[:5]:
            name = m.get("question", m.get("groupItemTitle", ""))
            china_md += f"- **{name}**\n  {pct(m.get('_yes_price'))} ({format_trend(m.get('_change'))})\n"
        elements.append({"tag": "markdown", "content": china_md.strip()})
    else:
        elements.append({"tag": "markdown", "content": "<font color='grey'>暂无活跃盘口</font>"})

    # 底部 Footer 与 按钮
    elements.append({"tag": "hr"})
    
    if doc_url:
        elements.append({
            "tag": "action",
            "actions": [{
                "tag": "button", "text": {"tag": "plain_text", "content": "📄 查阅完整交易面板"},
                "url": doc_url, "type": "primary"
            }]
        })

    card_payload = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True, "enable_forward": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"Polymarket 资金风向标 | {now.strftime('%m-%d')}"},
                "template": "blue"
            },
            "elements": elements + [{"tag": "note", "elements": [{"tag": "plain_text", "content": "Powered by Polymarket API + Cloud Watcher"}]}]
        }
    }

    try: requests.post(FEISHU_WEBHOOK_URL, json=card_payload, timeout=15)
    except Exception as e: log.error(f"飞书 Webhook 推送失败: {e}")


# ════════════════════════════════════════════════════════════════
# 4. 主流程
# ════════════════════════════════════════════════════════════════

def main():
    now = datetime.now(BJT)
    now_str = now.strftime("%Y-%m-%dT%H:%M")
    log.info(f"=== Polymarket Watch 运行开始 === {now_str}")

    history = load_history()
    all_markets = fetch_all_active_markets(limit=100, max_pages=10)
    
    relevant = filter_relevant_markets(all_markets)
    ai_markets = enrich_with_prices(relevant["ai"])
    china_markets = enrich_with_prices(relevant["china"])

    for m in ai_markets: m["_cat"] = "ai"
    for m in china_markets: m["_cat"] = "china"

    all_relevant = ai_markets + china_markets
    seen_slugs, unique = set(), []
    for m in all_relevant:
        slug = make_slug(m)
        if slug not in seen_slugs:
            seen_slugs.add(slug)
            unique.append(m)

    compare_with_history(unique, history, now_str)
    
    history["last_updated"] = now_str
    save_history(history)

    # 推送高定版结构化飞书卡片
    push_feishu_interactive(ai_markets, china_markets, now, doc_url="")
    
    log.info("=== 运行完毕，消息已投递 ===")
    return 0

if __name__ == "__main__":
    sys.exit(main())
