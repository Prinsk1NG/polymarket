#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Polymarket Watch — v4.0 精英策展版
1. 筛选维度：全网 Top 交易量、AI 核心盘、中国宏观、24h 剧烈波动。
2. 核心输出：这 10 个盘口的“上榜理由”与“信号含义”。
3. 严格禁令：严禁投资建议，严禁箭头推演。
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

# ─── 配置 ──────────────────────────────────────────────────────
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
NOISE_KEYWORDS = ["NBA", "Finals", "Win the game", "CS:", "FIFA", "Betting", "Weather", "Taipei", "Soccer", "League", "Points"]
BLOCKED_PATTERNS = [r"taiwan.*invasi", r"invad.*taiwan", r"xi jinping.*removed", r"china.*coup", r"regime.*change.*china", r"CCP.*collapse"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("polymarket-watch")

# ════════════════════════════════════════════════════════════════
# 1. 维度筛选逻辑 (Dimension Selector)
# ════════════════════════════════════════════════════════════════

def fetch_and_curate_top_10():
    all_raw = []
    for page in range(5):
        try:
            resp = requests.get(f"{GAMMA_API}/markets", params={
                "limit": 100, "offset": page * 100, "active": "true",
                "closed": "false", "order": "volume24hr", "ascending": "false"
            }, timeout=30)
            if resp.status_code == 200: all_raw.extend(resp.json())
        except: break

    # 获取价格与计算变动
    if not HISTORY_FILE.exists(): HISTORY_FILE.write_text(json.dumps({"markets": {}}))
    history = json.loads(HISTORY_FILE.read_text("utf-8"))
    now_str = datetime.now(BJT).strftime("%Y-%m-%d %H:%M")

    for m in all_raw:
        try:
            prices = json.loads(m.get("outcomePrices", "[]"))
            m["_yes_price"] = float(prices[0]) if prices else None
            slug = re.sub(r'[^a-z0-9]+', '-', m.get('question','').lower()).strip('-')[:80]
            m["_slug"] = slug
            prev = history["markets"].get(slug)
            prev_p = prev.get("p") or prev.get("current_probability") if prev else None
            m["_change"] = round(m["_yes_price"] - float(prev_p), 4) if (m["_yes_price"] is not None and prev_p is not None) else 0.0
            history["markets"][slug] = {"p": m["_yes_price"], "t": now_str}
        except: 
            m["_yes_price"], m["_change"] = None, 0.0

    HISTORY_FILE.write_text(json.dumps(history, ensure_ascii=False, indent=2), "utf-8")

    # 开始策展
    selected = []
    seen_ids = set()

    def is_valid(m):
        q = (m.get('question','') + m.get('description','')).lower()
        if m.get('conditionId') in seen_ids: return False
        if any(re.search(p, q, re.I) for p in BLOCKED_PATTERNS): return False
        if any(k.lower() in q for k in NOISE_KEYWORDS): return False
        if m.get('_yes_price') is None: return False
        return True

    # Dimension 1: 全球热钱 (Volume Top 3)
    v_top = [m for m in all_raw if is_valid(m)][:3]
    for m in v_top: m["_reason_type"] = "热钱风向"; selected.append(m); seen_ids.add(m.get('conditionId'))

    # Dimension 2: AI 核心博弈 (Top 3)
    ai_top = [m for m in all_raw if is_valid(m) and any(re.search(rf"\b{re.escape(kw.lower())}\b", (m.get('question','')+m.get('description','')).lower()) for kw in AI_KEYWORDS)][:3]
    for m in ai_top: m["_reason_type"] = "AI 信号"; selected.append(m); seen_ids.add(m.get('conditionId'))

    # Dimension 3: 中国宏观 (Top 2)
    c_top = [m for m in all_raw if is_valid(m) and any(re.search(rf"\b{re.escape(kw.lower())}\b", (m.get('question','')+m.get('description','')).lower()) for kw in CHINA_KEYWORDS)][:2]
    for m in c_top: m["_reason_type"] = "中国视角"; selected.append(m); seen_ids.add(m.get('conditionId'))

    # Dimension 4: 情绪黑天鹅 (Biggest Movers Top 2)
    movers = sorted([m for m in all_raw if is_valid(m)], key=lambda x: abs(x.get("_change", 0)), reverse=True)[:2]
    for m in movers: m["_reason_type"] = "异动预警"; selected.append(m); seen_ids.add(m.get('conditionId'))

    return selected[:10]

# ════════════════════════════════════════════════════════════════
# 2. Grok-Reasoning 分析 (上榜理由撰写)
# ════════════════════════════════════════════════════════════════

def analyze_reasons_with_llm(selected_markets):
    if not XAI_API_KEY: return {}
    
    ctx = ""
    for i, m in enumerate(selected_markets):
        ctx += f"{i+1}. [{m['_reason_type']}] {m.get('question')} | 赔率: {round(m.get('_yes_price',0)*100, 1)}% | 24h变动: {round(m.get('_change',0)*100, 2)}pp\n"

    prompt = f"""
你是一位顶级的一级市场投资分析师。以下是 10 个从 Polymarket 筛选出的预测盘口。
请为每一个盘口撰写一个极其精炼的“上榜理由”。

要求：
1. 解释该信号的【重大含义】：它为什么值得关注？预示了什么风险或机遇？
2. 严禁提供投资建议！严禁出现“加仓、买入、布局、建议”等词。
3. 每条理由控制在 50 字以内。不要有开场白。

数据：
{ctx}
"""
    try:
        client = Client(api_key=XAI_API_KEY)
        chat = client.chat.create(model="grok-4.20-0309-reasoning")
        chat.append(user(prompt))
        res = chat.sample().content.strip()
        res = re.sub(r'<think>.*?</think>', '', res, flags=re.DOTALL).strip()
        
        # 将结果按行拆分归位
        lines = [l.strip() for l in res.split('\n') if len(l.strip()) > 5]
        reasons_map = {}
        for i, m in enumerate(selected_markets):
            reasons_map[m.get('conditionId')] = lines[i] if i < len(lines) else "信号具有显著宏观指示意义。"
        return reasons_map
    except Exception as e:
        log.error(f"Grok 分析失败: {e}")
        return {}

# ════════════════════════════════════════════════════════════════
# 3. 飞书联动
# ════════════════════════════════════════════════════════════════

def push_feishu(selected_markets, reasons_map, now):
    if not FEISHU_WEBHOOK_URL: return
    
    elements = []
    elements.append({"tag": "markdown", "content": "**▌ 🎯 今日上榜预测盘口 (Top 10)**\n\n<font color='grey'>基于交易量、博弈强度及宏观相关性策展筛选</font>"})
    elements.append({"tag": "hr"})

    for m in selected_markets:
        cid = m.get('conditionId')
        reason = reasons_map.get(cid, "上榜理由：该盘口资金博弈强度显著，反映了市场关键预期。")
        pp = m.get("_change", 0) * 100
        color = "red" if pp > 0.4 else ("green" if pp < -0.4 else "grey")
        trend = f"<font color='{color}'>{'+' if pp>0 else ''}{pp:.1f}pp</font>"
        
        content = f"**{m.get('question')}**\n"
        content += f"概率: **{round(m.get('_yes_price',0)*100, 1)}%** ({trend}) | 类型: {m['_reason_type']}\n"
        content += f"<font color='grey'>💡 上榜理由：{reason}</font>\n"
        
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": content}})
        elements.append({"tag": "hr"})

    # 飞书云文档链接按钮 (逻辑同 v3.4 略)
    
    payload = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True, "enable_forward": True},
            "header": {"title": {"tag": "plain_text", "content": f"Polymarket 资金策展 | {now.strftime('%m-%d')}"}, "template": "indigo"},
            "elements": elements + [{"tag": "note", "elements": [{"tag": "plain_text", "content": "Insights by Grok-Reasoning · 每中午 12:00 更新"}]}]
        }
    }
    requests.post(FEISHU_WEBHOOK_URL, json=payload, timeout=15)

# ════════════════════════════════════════════════════════════════
# 4. 文档渲染
# ════════════════════════════════════════════════════════════════

def create_doc(selected_markets, reasons_map, now):
    if not (FEISHU_APP_ID and FEISHU_APP_SECRET): return ""
    try:
        r_token = requests.post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal", 
                              json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET})
        token = r_token.json().get("tenant_access_token")
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}
        
        # 1. 创建
        r_doc = requests.post("https://open.feishu.cn/open-apis/docx/v1/documents", headers=headers, json={"title": f"Polymarket 投资策展 | {now.strftime('%m-%d')}"})
        doc_id = r_doc.json().get("data", {}).get("document", {}).get("document_id")
        
        # 2. 写入
        blocks = [{"block_type": 1, "heading1": {"elements": [{"text_run": {"content": "📊 今日 Top 10 精选预测明细"}}]}}]
        for m in selected_markets:
            content = f"盘口: {m.get('question')}\n概率: {round(m.get('_yes_price',0)*100, 1)}%\n上榜理由: {reasons_map.get(m.get('conditionId'), '')}"
            blocks.append({"block_type": 2, "text": {"elements": [{"text_run": {"content": content}}], "style": {"list": {"type": "bullet"}}}})
        
        requests.post(f"https://open.feishu.cn/open-apis/docx/v1/documents/{doc_id}/blocks/{doc_id}/children", headers=headers, json={"children": blocks})
        requests.patch(f"https://open.feishu.cn/open-apis/drive/v1/permissions/{doc_id}/public?type=docx", headers=headers, json={"external_access_entity": "open", "security_entity": "anyone_can_view", "link_share_entity": "anyone_readable"})
        return f"https://hillhousecap.feishu.cn/docx/{doc_id}"
    except: return ""

# ════════════════════════════════════════════════════════════════
# 5. 主程序
# ════════════════════════════════════════════════════════════════

def main():
    now = datetime.now(BJT)
    log.info("=== Polymarket Elite Curation 开始 ===")

    # 1. 筛选 10 条精英数据
    selected = fetch_and_curate_top_10()
    
    # 2. 生成上榜理由
    reasons = analyze_reasons_with_llm(selected)
    
    # 3. 文档
    doc_url = create_doc(selected, reasons, now)
    
    # 4. 推送
    push_feishu(selected, reasons, now)
    log.info("=== 简报送达 ===")

if __name__ == "__main__":
    main()
