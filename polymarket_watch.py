#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Polymarket Watch — v2.0 终极版 (带动态涨跌、高定卡片与云文档联动)
通过 Polymarket 官方 API 获取预测数据，生成面向 AI 投资人的深度简报。
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
BJT = timezone(timedelta(hours=8))

# 环境变量读取
FEISHU_WEBHOOK_URL = os.getenv("FEISHU_WEBHOOK_URL", "")
FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")

# ─── 关键词过滤 ──────────────────────────────────────────────────
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
BLOCKED_PATTERNS = [
    r"taiwan.*invasi", r"invad.*taiwan", r"xi jinping.*removed",
    r"china.*coup", r"regime.*change.*china", r"CCP.*collapse",
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("polymarket-watch")

# ════════════════════════════════════════════════════════════════
# 1. API 基础函数 (Gamma & CLOB)
# ════════════════════════════════════════════════════════════════

def gamma_get(endpoint: str, params: dict | None = None) -> list | dict:
    try:
        r = requests.get(f"{GAMMA_API}/{endpoint}", params=params, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning(f"Gamma API 访问失败: {e}")
        return []

def fetch_all_active_markets(limit: int = 100, max_pages: int = 10) -> list:
    all_markets = []
    for page in range(max_pages):
        batch = gamma_get("markets", params={
            "limit": limit, "offset": page * limit, "active": "true",
            "closed": "false", "order": "volume24hr", "ascending": "false",
        })
        if not isinstance(batch, list):
            batch = batch.get("data", batch.get("markets", []))
        if not batch: break
        all_markets.extend(batch)
        if len(batch) < limit: break
        time.sleep(0.3)
    return all_markets

def enrich_with_prices(markets: list) -> list:
    for m in markets:
        prices_str = m.get("outcomePrices", "")
        try:
            if isinstance(prices_str, str) and prices_str:
                m["_yes_price"] = float(json.loads(prices_str)[0])
            elif isinstance(prices_str, list) and prices_str:
                m["_yes_price"] = float(prices_str[0])
            else:
                # Fallback to CLOB Midpoint
                token_ids = m.get("clobTokenIds", "[]")
                t_list = json.loads(token_ids) if isinstance(token_ids, str) else token_ids
                if t_list:
                    r = requests.get(f"{CLOB_API}/midpoint", params={"token_id": t_list[0]}, timeout=10)
                    m["_yes_price"] = float(r.json().get("mid")) if r.status_code == 200 else None
                    time.sleep(0.1)
        except: m["_yes_price"] = None
    return markets

# ════════════════════════════════════════════════════════════════
# 2. 核心逻辑：历史对比与 Slug 生成
# ════════════════════════════════════════════════════════════════

def load_history() -> dict:
    if HISTORY_FILE.exists():
        try: return json.loads(HISTORY_FILE.read_text("utf-8"))
        except: pass
    return {"markets": {}}

def make_slug(m: dict) -> str:
    question = m.get("question", m.get("groupItemTitle", ""))
    slug = re.sub(r'[^a-z0-9]+', '-', question.lower()).strip('-')[:80]
    return slug or m.get("conditionId", "unknown")

def compare_and_save(markets: list, history: dict, now_str: str):
    for m in markets:
        slug = make_slug(m)
        price = m.get("_yes_price")
        if price is None: continue

        prev = history["markets"].get(slug)
        if prev:
            m["_change"] = round(price - prev["current_probability"], 4)
            m["_prev_prob"] = prev["current_probability"]
        else:
            m["_change"] = None
            m["_is_new"] = True

        history["markets"][slug] = {
            "current_probability": price,
            "last_updated": now_str,
            "history": (prev.get("history", []) + [{"d": now_str, "p": price}])[-20:]
        }
    HISTORY_FILE.write_text(json.dumps(history, ensure_ascii=False, indent=2), "utf-8")

# ════════════════════════════════════════════════════════════════
# 3. UI 渲染逻辑 (针对飞书进行高定排版)
# ════════════════════════════════════════════════════════════════

def pct(v: float | None) -> str:
    return f"{v*100:.1f}%" if v is not None else "N/A"

def format_trend(c: float | None) -> str:
    if c is None: return "<font color='grey'>New</font>"
    pp = c * 100
    if pp >= 0.5: return f"<font color='red'>🔥 +{pp:.1f}pp</font>"
    if pp <= -0.5: return f"<font color='green'>🍃 {pp:.1f}pp</font>"
    return f"<font color='grey'>{'+' if pp>0 else ''}{pp:.1f}pp</font>"

def generate_doc_markdown(ai_markets, china_markets, now):
    """生成用于飞书长文档的详细 Markdown 内容"""
    lines = [f"# Polymarket 深度交易面板 | {now.strftime('%Y-%m-%d')}", "---"]
    
    lines.append("## 🤖 AI 赛道详情")
    for m in ai_markets:
        lines.append(f"- **{m.get('question')}**\n  概率: {pct(m.get('_yes_price'))} | 变动: {format_trend(m.get('_change'))}")
    
    lines.append("\n## 🇨🇳 中国宏观详情")
    for m in china_markets:
        lines.append(f"- **{m.get('question')}**\n  概率: {pct(m.get('_yes_price'))} | 变动: {format_trend(m.get('_change'))}")
        
    lines.append("\n---\n*数据实时抓取自 Polymarket Gamma API*")
    return "\n".join(lines)

# ════════════════════════════════════════════════════════════════
# 4. 飞书 API 联动 (文档创建 + Webhook)
# ════════════════════════════════════════════════════════════════

def get_tenant_token():
    if not FEISHU_APP_ID or not FEISHU_APP_SECRET: return None
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    try:
        r = requests.post(url, json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET})
        return r.json().get("tenant_access_token")
    except: return None

def push_feishu_doc(title, md_content):
    token = get_tenant_token()
    if not token: return ""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}
    
    try:
        # 1. 创建文档
        r = requests.post("https://open.feishu.cn/open-apis/docx/v1/documents", 
                          headers=headers, json={"title": title})
        doc_id = r.json().get("data", {}).get("document", {}).get("document_id")
        if not doc_id: return ""

        # 2. 写入内容 (简化版写入)
        requests.post(f"https://open.feishu.cn/open-apis/docx/v1/documents/{doc_id}/blocks/{doc_id}/children",
                      headers=headers, json={
                          "children": [{"block_type": 2, "text": {"elements": [{"text_run": {"content": md_content}}]}}]
                      })
        
        # 3. 开启权限：链接所有人可阅读
        requests.patch(f"https://open.feishu.cn/open-apis/drive/v1/permissions/{doc_id}/public?type=docx",
                       headers=headers, json={"external_access_entity": "open", "security_entity": "anyone_can_view", "link_share_entity": "anyone_readable"})
        
        return f"https://hillhousecap.feishu.cn/docx/{doc_id}"
    except Exception as e:
        log.error(f"飞书文档创建失败: {e}")
        return ""

def push_feishu_card(ai_markets, china_markets, now, doc_url=""):
    if not FEISHU_WEBHOOK_URL: return
    
    # 筛选异动较大的市场
    all_m = ai_markets + china_markets
    significant = sorted([m for m in all_m if m.get("_change")], key=lambda x: abs(x["_change"]), reverse=True)[:3]
    
    elements = []
    
    # 核心异动模块
    elements.append({"tag": "markdown", "content": "**▌ ⚡️ 核心异动 (Top Movers)**"})
    movers_md = ""
    for m in significant:
        movers_md += f"👉 **{m.get('question')[:50]}...**\n概率: **{pct(m.get('_yes_price'))}** | 变动: {format_trend(m.get('_change'))}\n\n"
    elements.append({"tag": "markdown", "content": movers_md or "今日情绪稳定，无显著异动。"})
    elements.append({"tag": "hr"})

    # AI 赛道
    elements.append({"tag": "markdown", "content": "**▌ 🤖 AI 赛道概览**"})
    ai_md = ""
    for m in sorted(ai_markets, key=lambda x: x.get("_yes_price") or 0, reverse=True)[:5]:
        ai_md += f"• {m.get('question')[:40]}... **{pct(m.get('_yes_price'))}**\n"
    elements.append({"tag": "markdown", "content": ai_md or "暂无数据"})
    
    if doc_url:
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "action",
            "actions": [{
                "tag": "button", "text": {"tag": "plain_text", "content": "📄 查看详细交易面板"},
                "url": doc_url, "type": "primary"
            }]
        })

    payload = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": f"Polymarket 资金风向标 | {now.strftime('%m-%d')}"}, "template": "blue"},
            "elements": elements + [{"tag": "note", "elements": [{"tag": "plain_text", "content": "Powered by Cloud Watcher"}]}]
        }
    }
    requests.post(FEISHU_WEBHOOK_URL, json=payload)

# ════════════════════════════════════════════════════════════════
# 5. 主程序入口
# ════════════════════════════════════════════════════════════════

def main():
    now = datetime.now(BJT)
    now_str = now.strftime("%Y-%m-%dT%H:%M")
    log.info(f"=== Polymarket Watch 启动 === {now_str}")

    # 1. 抓取与过滤
    raw_markets = fetch_all_active_markets()
    ai_list = []
    china_list = []
    
    for m in raw_markets:
        text = f"{m.get('question')} {m.get('description')}".lower()
        if any(re.search(p, text, re.I) for p in BLOCKED_PATTERNS): continue
        if any(kw.lower() in text for kw in AI_KEYWORDS): ai_list.append(m)
        if any(kw.lower() in text for kw in CHINA_KEYWORDS): china_list.append(m)

    # 2. 价格补全与历史比对
    ai_list = enrich_with_prices(ai_list)
    china_list = enrich_with_prices(china_list)
    
    history = load_history()
    compare_and_save(ai_list + china_list, history, now_str)

    # 3. 飞书云文档生成 (自检逻辑)
    doc_url = ""
    if FEISHU_APP_ID and FEISHU_APP_SECRET:
        log.info("检测到飞书应用密钥，正在生成详细简报文档...")
        detailed_md = generate_doc_markdown(ai_list, china_list, now)
        doc_url = push_feishu_doc(f"Polymarket 交易明细 | {now.strftime('%m-%d %H:%M')}", detailed_md)
        if doc_url:
            log.info(f"成功生成飞书文档: {doc_url}")
        else:
            log.warning("飞书文档生成失败，请检查应用权限是否包含 docx:document")
    else:
        log.info("未配置 FEISHU_APP_ID，跳过云文档生成环节。")

    # 4. Webhook 卡片推送
    push_feishu_card(ai_list, china_list, now, doc_url=doc_url)
    log.info("=== 任务完成，消息已投递 ===")

if __name__ == "__main__":
    main()
