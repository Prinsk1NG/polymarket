#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Polymarket Watch — v4.2 视觉纯净版
1. 格式：Emoji + 盘口名，去除所有数字编号。
2. 标题：Polymarket 预测盘口。
3. 逻辑：智能标签清洗，确保理由与盘口 100% 匹配且无冗余文字。
"""

import os
import re
import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from xai_sdk import Client
from xai_sdk.chat import user, system

# ─── 基础配置 ──────────────────────────────────────────────────
GAMMA_API = "https://gamma-api.polymarket.com"
HISTORY_FILE = Path(__file__).parent / "market_history.json"
BJT = timezone(timedelta(hours=8))

# 环境变量
FEISHU_WEBHOOK_URL = os.getenv("FEISHU_WEBHOOK_URL", "")
FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")
XAI_API_KEY = os.getenv("XAI_API_KEY", "")

# ─── 过滤器 ────────────────────────────────────────────────────
AI_KEYWORDS = ["AGI", "GPT", "Claude", "Gemini", "OpenAI", "Anthropic", "DeepSeek", "NVIDIA", "AI chip", "AI model", "LLM", "GPT-5", "Sora", "xAI"]
CHINA_KEYWORDS = ["China GDP", "trade war", "tariff", "semiconductor", "chip ban", "Huawei", "BYD", "PBOC", "Chinese stocks", "China economy", "TikTok"]
NOISE_KEYWORDS = ["NBA", "Finals", "Win the game", "CS:", "FIFA", "Betting", "Weather", "Taipei", "Soccer", "League", "Points", "Basketball"]
BLOCKED_PATTERNS = [r"taiwan.*invasi", r"invad.*taiwan", r"xi jinping.*removed", r"china.*coup", r"regime.*change.*china", r"CCP.*collapse"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("polymarket-watch")

# ════════════════════════════════════════════════════════════════
# 1. 策展引擎 (精英 Top 10)
# ════════════════════════════════════════════════════════════════

def fetch_and_curate():
    all_raw = []
    for page in range(5):
        try:
            resp = requests.get(f"{GAMMA_API}/markets", params={
                "limit": 100, "offset": page * 100, "active": "true",
                "closed": "false", "order": "volume24hr", "ascending": "false"
            }, timeout=30)
            if resp.status_code == 200: all_raw.extend(resp.json())
        except: break

    if not HISTORY_FILE.exists(): HISTORY_FILE.write_text(json.dumps({"markets": {}}))
    try: history = json.loads(HISTORY_FILE.read_text("utf-8"))
    except: history = {"markets": {}}
    now_str = datetime.now(BJT).strftime("%Y-%m-%d %H:%M")

    for m in all_raw:
        try:
            prices = json.loads(m.get("outcomePrices", "[]"))
            m["_yes_price"] = float(prices[0]) if prices else None
            slug = re.sub(r'[^a-z0-9]+', '-', m.get('question','').lower()).strip('-')[:80]
            prev = history["markets"].get(slug)
            prev_p = prev.get("p") or prev.get("current_probability") if prev else None
            m["_change"] = round(m["_yes_price"] - float(prev_p), 4) if (m["_yes_price"] is not None and prev_p is not None) else 0.0
            history["markets"][slug] = {"p": m["_yes_price"], "t": now_str}
        except: m["_yes_price"], m["_change"] = None, 0.0

    HISTORY_FILE.write_text(json.dumps(history, ensure_ascii=False, indent=2), "utf-8")

    selected, seen_ids = [], set()
    def is_valid(m):
        q = (m.get('question','') + m.get('description','')).lower()
        if m.get('conditionId') in seen_ids: return False
        if any(re.search(p, q, re.I) for p in BLOCKED_PATTERNS): return False
        if any(k.lower() in q for k in NOISE_KEYWORDS): return False
        if m.get('_yes_price') is None: return False
        return True

    # 分维度策展并映射图标
    # 热钱风向
    for m in [m for m in all_raw if is_valid(m)][:3]:
        m["_reason_type"] = "热钱风向"; m["_emoji"] = "💰"; selected.append(m); seen_ids.add(m.get('conditionId'))
    # AI 信号
    for m in [m for m in all_raw if is_valid(m) and any(re.search(rf"\b{re.escape(kw.lower())}\b", (m.get('question','')+m.get('description','')).lower()) for kw in AI_KEYWORDS)][:3]:
        m["_reason_type"] = "AI 信号"; m["_emoji"] = "🤖"; selected.append(m); seen_ids.add(m.get('conditionId'))
    # 中国视角
    for m in [m for m in all_raw if is_valid(m) and any(re.search(rf"\b{re.escape(kw.lower())}\b", (m.get('question','')+m.get('description','')).lower()) for kw in CHINA_KEYWORDS)][:2]:
        m["_reason_type"] = "中国视角"; m["_emoji"] = "🇨🇳"; selected.append(m); seen_ids.add(m.get('conditionId'))
    # 异动预警
    for m in sorted([m for m in all_raw if is_valid(m)], key=lambda x: abs(x.get("_change", 0)), reverse=True)[:2]:
        m["_reason_type"] = "异动预警"; m["_emoji"] = "⚡"; selected.append(m); seen_ids.add(m.get('conditionId'))

    return selected[:10]

# ════════════════════════════════════════════════════════════════
# 2. Grok 分析 (强制 JSON + 强制去编号)
# ════════════════════════════════════════════════════════════════

def analyze_reasons_cleanly(selected_markets):
    if not XAI_API_KEY: return {}
    ctx = ""
    for m in selected_markets: ctx += f"ID: {m.get('conditionId')} | Market: {m.get('question')}\n"

    prompt = f"""
你是一位资深投资分析师。请为以下预测盘口撰写“上榜理由”。
要求：
1. **绝对禁令**：严禁使用任何编号（如 1. 2. 3.）、严禁重复“上榜理由”字样、严禁提供投资建议。
2. 逻辑：直接描述该盘口赔率反映的重大信号或隐蔽风险。
3. 必须输出为标准 JSON：{{"ID": "理由内容"}}。每条理由 50 字内。

数据：
{ctx}
"""
    try:
        client = Client(api_key=XAI_API_KEY)
        chat = client.chat.create(model="grok-4.20-0309-reasoning")
        chat.append(user(prompt))
        res = re.sub(r'<think>.*?</think>', '', chat.sample().content.strip(), flags=re.DOTALL)
        json_match = re.search(r'\{.*\}', res, re.DOTALL)
        if json_match:
            raw_map = json.loads(json_match.group())
            # 二次清洗：剔除理由中可能存在的数字编号前缀
            return {k: re.sub(r'^(\d+[\.\s、]+|上榜理由[:：\s]*)', '', v).strip() for k, v in raw_map.items()}
        return {}
    except Exception as e:
        log.error(f"分析失败: {e}")
        return {}

# ════════════════════════════════════════════════════════════════
# 3. 飞书渲染 (视觉重构版)
# ════════════════════════════════════════════════════════════════

def push_feishu_minimal(selected_markets, reasons_map, now):
    if not FEISHU_WEBHOOK_URL: return
    
    # 头部极简
    elements = [
        {"tag": "markdown", "content": f"**▌ 🎯 今日上榜预测盘口 (Top 10)**\n<font color='grey'>博弈信号实时监控</font>"},
        {"tag": "hr"}
    ]

    for m in selected_markets:
        reason = reasons_map.get(m.get('conditionId'), "信号具有显著博弈活跃度。")
        pp = m.get("_change", 0) * 100
        color = "red" if pp > 0.4 else ("green" if pp < -0.4 else "grey")
        trend = f"<font color='{color}'>{'+' if pp>0 else ''}{pp:.1f}pp</font>"
        
        # 格式：{Emoji} **问题名**
        card_content = f"{m.get('_emoji','📍')} **{m.get('question')}**\n"
        card_content += f"概率: **{round(m.get('_yes_price',0)*100, 1)}%** ({trend}) | 维度: {m['_reason_type']}\n\n"
        card_content += f"<font color='grey'>💡 {reason}</font>\n"
        
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": card_content}})
        elements.append({"tag": "hr"})

    payload = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True, "enable_forward": True},
            "header": {"title": {"tag": "plain_text", "content": f"Polymarket 预测盘口 | {now.strftime('%m-%d')}"}, "template": "indigo"},
            "elements": elements + [{"tag": "note", "elements": [{"tag": "plain_text", "content": "Grok-Reasoning 决策支持 · 仅限事实推演"}]}]
        }
    }
    requests.post(FEISHU_WEBHOOK_URL, json=payload, timeout=15)

def main():
    now = datetime.now(BJT)
    log.info("=== Polymarket Watch v4.2 视觉纯净版启动 ===")

    selected = fetch_and_curate()
    reasons = analyze_reasons_cleanly(selected)
    
    # 云文档创建逻辑 (保持 v4.1 逻辑不变)
    # doc_url = create_doc(...) 
    
    push_feishu_minimal(selected, reasons, now)
    log.info("=== 推送成功 ===")

if __name__ == "__main__":
    main()
