#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Polymarket Watch — v3.2 深度渲染版
1. 修复文本截断问题，显示完整盘口名称
2. 采用标签匹配技术 (Tag-Matching) 精准提取分析内容
3. 增强过滤逻辑，确保投资情报纯净度
"""

import os
import re
import json
import time
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

# 关键词
AI_KEYWORDS = ["AGI", "GPT", "Claude", "Gemini", "OpenAI", "Anthropic", "DeepSeek", "NVIDIA", "AI chip", "AI model", "LLM", "Sora", "xAI", "Reasoning"]
CHINA_KEYWORDS = ["China GDP", "trade war", "tariff", "semiconductor", "chip ban", "Huawei", "BYD", "PBOC", "Chinese stocks", "China economy", "TikTok"]
NOISE_KEYWORDS = ["NBA", "Finals", "Win the game", "Counter-Strike", "NHL", "FIFA", "Esports", "Betting", "Weather", "Taipei", "Basketball", "Soccer"]
BLOCKED_PATTERNS = [r"taiwan.*invasi", r"invad.*taiwan", r"xi jinping.*removed", r"china.*coup", r"regime.*change.*china", r"CCP.*collapse"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("polymarket-watch")

# ════════════════════════════════════════════════════════════════
# 1. 精准数据抓取
# ════════════════════════════════════════════════════════════════

def fetch_top_markets():
    markets = []
    for page in range(5): 
        try:
            resp = requests.get(f"{GAMMA_API}/markets", params={
                "limit": 100, "offset": page * 100, "active": "true", "closed": "false",
                "order": "volume24hr", "ascending": "false"
            }, timeout=30)
            batch = resp.json()
            if not batch: break
            markets.extend(batch)
        except: break
    
    ai_pool, china_pool = [], []
    for m in markets:
        q = (m.get('question','') + m.get('description','')).lower()
        if any(re.search(p, q, re.I) for p in BLOCKED_PATTERNS): continue
        if any(k.lower() in q for k in NOISE_KEYWORDS): continue
        
        # 严格单词边界匹配，防止 Taipei 误杀
        if any(re.search(rf"\b{re.escape(kw.lower())}\b", q) for kw in AI_KEYWORDS):
            ai_pool.append(m)
        elif any(re.search(rf"\b{re.escape(kw.lower())}\b", q) for kw in CHINA_KEYWORDS):
            china_pool.append(m)
            
    ai_top = sorted(ai_pool, key=lambda x: float(x.get('volume',0)), reverse=True)[:5]
    china_top = sorted(china_pool, key=lambda x: float(x.get('volume',0)), reverse=True)[:5]
    return ai_top, china_top

def get_real_prices(markets):
    for m in markets:
        try:
            prices_str = m.get("outcomePrices", "")
            if isinstance(prices_str, str) and prices_str:
                m["_yes_price"] = float(json.loads(prices_str)[0])
            elif isinstance(prices_str, list) and prices_str:
                m["_yes_price"] = float(prices_str[0])
            else: m["_yes_price"] = None
        except: m["_yes_price"] = None
    return markets

# ════════════════════════════════════════════════════════════════
# 2. 状态机：记录历史用于对比变动
# ════════════════════════════════════════════════════════════════

def compare_history(markets, now_str):
    if not HISTORY_FILE.exists():
        HISTORY_FILE.write_text(json.dumps({"markets": {}}))
    try:
        history = json.loads(HISTORY_FILE.read_text("utf-8"))
    except:
        history = {"markets": {}}
    
    for m in markets:
        slug = re.sub(r'[^a-z0-9]+', '-', m.get('question','').lower()).strip('-')[:80]
        price = m.get("_yes_price")
        if price is None: continue
        
        prev = history["markets"].get(slug)
        if prev:
            prev_p = prev.get("p") or prev.get("current_probability")
            m["_change"] = round(price - float(prev_p), 4)
        else:
            m["_change"] = 0.0
        
        history["markets"][slug] = {"p": price, "t": now_str}
        
    HISTORY_FILE.write_text(json.dumps(history, ensure_ascii=False, indent=2), "utf-8")
    return markets

# ════════════════════════════════════════════════════════════════
# 3. Grok 分析：使用结构化标签
# ════════════════════════════════════════════════════════════════

def analyze_investment_strategy(ai_list, china_list):
    if not XAI_API_KEY: return "分析不可用"
    
    data_ctx = "【AI 监测样本】\n"
    for m in ai_list:
        data_ctx += f"- {m.get('question')}: {round(m.get('_yes_price',0)*100,1)}% (变动: {round(m.get('_change',0)*100,2)}pp)\n"
    data_ctx += "\n【中国宏观监测样本】\n"
    for m in china_list:
        data_ctx += f"- {m.get('question')}: {round(m.get('_yes_price',0)*100,1)}% (变动: {round(m.get('_change',0)*100,2)}pp)\n"

    prompt = f"""
你是一位资深的 VC 投资策略师。请基于以下数据生成中文博弈分析。

## 输出规范（严格遵守标签，不要有任何开场白）：
[ACTION]
此处写 1-2 条最核心的信号推演结论（信号 -> 含义 -> 建议）。
[/ACTION]

[WARN]
此处写“危险共识”预警（赔率 >90% 或 <10% 的市场），指出反驳共识的一个深度理由。
[/WARN]

[LINK]
此处写赔率变化对具体赛道（如芯片、应用层、出海）的投资含义推演。
[/LINK]

最新数据：
{data_ctx}
"""
    try:
        client = Client(api_key=XAI_API_KEY)
        chat = client.chat.create(model="grok-4.20-0309-reasoning")
        chat.append(user(prompt))
        res = chat.sample().content.strip()
        return re.sub(r'<think>.*?</think>', '', res, flags=re.DOTALL).strip()
    except Exception as e:
        return f"分析生成失败: {e}"

# ════════════════════════════════════════════════════════════════
# 4. 飞书卡片渲染：修复截断问题
# ════════════════════════════════════════════════════════════════

def push_feishu_card(ai_list, china_list, analysis, doc_url, now):
    if not FEISHU_WEBHOOK_URL: return
    
    # 抽取标签内容
    def extract_tag(tag, text):
        match = re.search(rf"\[{tag}\](.*?)\[/{tag}\]", text, re.DOTALL)
        return match.group(1).strip() if match else "暂无该项分析"

    action_text = extract_tag("ACTION", analysis)
    warn_text = extract_tag("WARN", analysis)
    link_text = extract_tag("LINK", analysis)

    elements = [
        {"tag": "div", "text": {"tag": "lark_md", "content": f"**🎯 今日行动提示**\n{action_text}"}},
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "lark_md", "content": f"**⚠️ 共识偏差预警**\n{warn_text}"}},
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "lark_md", "content": f"**🔗 赔率→赛道联动**\n{link_text}"}},
        {"tag": "hr"},
    ]

    # AI 赛道异动（去掉截断，显示完整问题）
    ai_md = "**▌ 🤖 AI 赛道异动**\n"
    for m in sorted(ai_list, key=lambda x: abs(x.get("_change", 0)), reverse=True):
        pp = m["_change"] * 100
        color = "red" if pp > 0.4 else ("green" if pp < -0.4 else "grey")
        # 🚨 移除 [:40]，显示完整 Question
        ai_md += f"• **{m.get('question')}**\n  概率: {round(m.get('_yes_price',0)*100, 1)}% (<font color='{color}'>{'+' if pp>0 else ''}{pp:.1f}pp</font>)\n"
    elements.append({"tag": "markdown", "content": ai_md})

    if doc_url:
        elements.append({"tag": "hr"})
        elements.append({"tag": "action", "actions": [{"tag": "button", "text": {"tag": "plain_text", "content": "📄 查阅完整深度简报"}, "url": doc_url, "type": "primary"}]})

    payload = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": f"Polymarket 资金风向标 | {now.strftime('%m-%d')}"}, "template": "indigo"},
            "elements": elements + [{"tag": "note", "elements": [{"tag": "plain_text", "content": "Grok-Reasoning 驱动 · 每周新陈代谢"}]}]
        }
    }
    requests.post(FEISHU_WEBHOOK_URL, json=payload)

def main():
    now = datetime.now(BJT)
    now_str = now.strftime("%Y-%m-%d %H:%M")
    log.info(f"=== Polymarket Investment Briefing 开始生成 ===")

    ai_top, china_top = fetch_top_markets()
    ai_top = get_real_prices(ai_top)
    china_top = get_real_prices(china_top)
    compare_history(ai_top + china_top, now_str)

    log.info("正在调用 Grok 进行投研分析...")
    analysis = analyze_investment_strategy(ai_top, china_top)
    
    # 此处假设 doc_url 逻辑已由独立函数处理
    push_feishu_card(ai_top, china_top, analysis, "", now)
    log.info("=== 任务完成 ===")

if __name__ == "__main__":
    main()
