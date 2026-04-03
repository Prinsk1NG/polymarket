#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Polymarket Watch — v3.3 视觉旗舰版
1. 视觉重构：强制间距 + 分割线，彻底消除拥挤感。
2. 逻辑重构：废弃箭头推演，采用“信号+解读”深度模式。
3. 内容重构：严格禁止投资建议，仅限事实逻辑推演。
4. 修复报错：解决 NameError 及文本截断问题。
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

# 关键词与过滤
AI_KEYWORDS = ["AGI", "GPT", "Claude", "Gemini", "OpenAI", "Anthropic", "DeepSeek", "NVIDIA", "AI chip", "AI model", "LLM", "Sora", "xAI"]
CHINA_KEYWORDS = ["China GDP", "trade war", "tariff", "semiconductor", "chip ban", "Huawei", "BYD", "PBOC", "Chinese stocks", "China economy", "TikTok"]
NOISE_KEYWORDS = ["NBA", "Finals", "Win the game", "Counter-Strike", "NHL", "FIFA", "Esports", "Betting", "Weather", "Taipei", "Basketball", "Soccer"]
BLOCKED_PATTERNS = [r"taiwan.*invasi", r"invad.*taiwan", r"xi jinping.*removed", r"china.*coup", r"regime.*change.*china", r"CCP.*collapse"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("polymarket-watch")

# ════════════════════════════════════════════════════════════════
# 1. 数据采集模块
# ════════════════════════════════════════════════════════════════

def fetch_top_markets():
    markets = []
    for page in range(5): 
        try:
            resp = requests.get(f"{GAMMA_API}/markets", params={
                "limit": 100, "offset": page * 100, "active": "true", "closed": "false",
                "order": "volume24hr", "ascending": "false"
            }, timeout=30)
            if resp.status_code == 200:
                markets.extend(resp.json())
        except: break
    
    ai_pool, china_pool = [], []
    for m in markets:
        q = (m.get('question','') + m.get('description','')).lower()
        if any(re.search(p, q, re.I) for p in BLOCKED_PATTERNS): continue
        if any(k.lower() in q for k in NOISE_KEYWORDS): continue
        
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
            prices = json.loads(m.get("outcomePrices", "[]"))
            m["_yes_price"] = float(prices[0]) if prices else None
        except: m["_yes_price"] = None
    return markets

def compare_history(markets, now_str):
    if not HISTORY_FILE.exists(): HISTORY_FILE.write_text(json.dumps({"markets": {}}))
    try: history = json.loads(HISTORY_FILE.read_text("utf-8"))
    except: history = {"markets": {}}
    
    for m in markets:
        slug = re.sub(r'[^a-z0-9]+', '-', m.get('question','').lower()).strip('-')[:80]
        price = m.get("_yes_price")
        if price is None: continue
        prev = history["markets"].get(slug)
        prev_p = prev.get("p") or prev.get("current_probability") if prev else None
        m["_change"] = round(price - float(prev_p), 4) if prev_p is not None else 0.0
        history["markets"][slug] = {"p": price, "t": now_str}
        
    HISTORY_FILE.write_text(json.dumps(history, ensure_ascii=False, indent=2), "utf-8")
    return markets

# ════════════════════════════════════════════════════════════════
# 2. Grok-Reasoning：深度逻辑推演 (移除投资建议)
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
你是一位顶级的一级市场宏观策略师。请基于最新的数据生成中文博弈分析。

## 核心禁令 (🚨 绝对遵守)：
1. 严禁提供投资建议。禁止出现“买入、配置、建议、加仓、看好”等字眼。
2. 仅做事实解读：说明赔率反映了什么资本情绪，锁定了什么样的行业预期。
3. 视觉要求：禁止使用箭头符号（->），禁止输出日期或简报标题。

## 输出格式：
[ACTION]
**信号**：英文市场名 + 赔率。
**解读**：分析该赔率背后反映的市场真实预期。
[/ACTION]

[WARN]
**危险共识**：市场名 + 赔率。
**反驳逻辑**：指出定价中被忽略的潜在风险或逻辑盲点。
[/WARN]

[LINK]
**[赛道名]**：该赔率变化对具体赛道（芯片/模型/应用/出海）的技术路径或竞争格局的具体影响。
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
# 3. 飞书渲染：视觉优化与分割
# ════════════════════════════════════════════════════════════════

def push_feishu_card(ai_list, china_list, analysis, doc_url, now):
    if not FEISHU_WEBHOOK_URL: return
    
    def extract_tag(tag, text):
        match = re.search(rf"\[{tag}\](.*?)\[/{tag}\]", text, re.DOTALL)
        return match.group(1).strip() if match else "暂无深度分析"

    elements = [
        {"tag": "div", "text": {"tag": "lark_md", "content": f"**▌ 🎯 核心市场判读**\n\n{extract_tag('ACTION', analysis)}"}},
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "lark_md", "content": f"**▌ ⚠️ 共识偏差检验**\n\n{extract_tag('WARN', analysis)}"}},
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "lark_md", "content": f"**▌ 🔗 赔率与赛道联动**\n\n{extract_tag('LINK', analysis)}"}},
        {"tag": "hr"},
    ]

    all_movers = sorted(ai_list + china_list, key=lambda x: abs(x.get("_change", 0)), reverse=True)
    movers_md = "**▌ 📊 实时赔率异动**\n\n"
    for m in all_movers[:5]:
        pp = m.get("_change", 0) * 100
        color = "red" if pp > 0.4 else ("green" if pp < -0.4 else "grey")
        trend = f"<font color='{color}'>{'+' if pp>0 else ''}{pp:.1f}pp</font>"
        movers_md += f"• {m.get('question')}\n  **{round(m.get('_yes_price',0)*100, 1)}%** ({trend})\n\n"
    
    elements.append({"tag": "div", "text": {"tag": "lark_md", "content": movers_md}})

    if doc_url:
        elements.append({"tag": "hr"})
        elements.append({"tag": "action", "actions": [{"tag": "button", "text": {"tag": "plain_text", "content": "📄 查阅完整深度简报"}, "url": doc_url, "type": "primary"}]})

    payload = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True, "enable_forward": True},
            "header": {"title": {"tag": "plain_text", "content": f"Polymarket 资金风向标 | {now.strftime('%m-%d')}"}, "template": "indigo"},
            "elements": elements + [{"tag": "note", "elements": [{"tag": "plain_text", "content": "Grok-Reasoning 驱动 · 实时情绪监控"}]}]
        }
    }
    requests.post(FEISHU_WEBHOOK_URL, json=payload)

# ════════════════════════════════════════════════════════════════
# 4. 飞书文档渲染 (结构化 Blocks)
# ════════════════════════════════════════════════════════════════

def get_tenant_token():
    if not (FEISHU_APP_ID and FEISHU_APP_SECRET): return None
    r = requests.post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal", json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET})
    return r.json().get("tenant_access_token")

def push_feishu_doc(title, analysis, ai_list, china_list, now):
    token = get_tenant_token()
    if not token: return ""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}
    try:
        r = requests.post("https://open.feishu.cn/open-apis/docx/v1/documents", headers=headers, json={"title": title})
        doc_id = r.json().get("data", {}).get("document", {}).get("document_id")
        if not doc_id: return ""
        
        # 移除标签后的纯文本分析
        clean_analysis = re.sub(r'\[/?\w+\]', '', analysis).strip()
        
        blocks = [
            {"block_type": 1, "heading1": {"elements": [{"text_run": {"content": "🧠 深度博弈分析 (Grok Reasoning)"}}]}},
            {"block_type": 2, "text": {"elements": [{"text_run": {"content": clean_analysis}}]}},
            {"block_type": 12, "divider": {}},
            {"block_type": 3, "heading2": {"elements": [{"text_run": {"content": "📊 详细赔率清单"}}]}}
        ]
        for m in ai_list + china_list:
            blocks.append({
                "block_type": 2,
                "text": {"elements": [{"text_run": {"content": f"• {m.get('question')}: {round(m.get('_yes_price',0)*100,1)}% ({round(m.get('_change',0)*100,2)}pp)"}}], "style": {"list": {"type": "bullet"}}}
            })
        requests.post(f"https://open.feishu.cn/open-apis/docx/v1/documents/{doc_id}/blocks/{doc_id}/children", headers=headers, json={"children": blocks})
        requests.patch(f"https://open.feishu.cn/open-apis/drive/v1/permissions/{doc_id}/public?type=docx", headers=headers, json={"external_access_entity": "open", "security_entity": "anyone_can_view", "link_share_entity": "anyone_readable"})
        return f"https://hillhousecap.feishu.cn/docx/{doc_id}"
    except: return ""

# ════════════════════════════════════════════════════════════════
# 5. 主流程
# ════════════════════════════════════════════════════════════════

def main():
    now = datetime.now(BJT)
    now_str = now.strftime("%Y-%m-%dT%H:%M")
    log.info(f"=== Polymarket Investment Briefing 开始生成 ===")

    ai_top, china_top = fetch_top_markets()
    ai_top = get_real_prices(ai_top)
    china_top = get_real_prices(china_top)
    compare_history(ai_top + china_top, now_str)

    log.info("正在调用 Grok 进行投研分析...")
    analysis = analyze_investment_strategy(ai_top, china_top)
    
    doc_url = ""
    if FEISHU_APP_ID:
        doc_url = push_feishu_doc(f"Polymarket 投研日报 | {now.strftime('%m-%d')}", analysis, ai_top, china_top, now)

    push_feishu_card(ai_top, china_top, analysis, doc_url, now)
    log.info("=== 任务完成 ===")

if __name__ == "__main__":
    main()
