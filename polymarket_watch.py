#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Polymarket Watch — v3.1 投资决策完全体
1. 筛选 Top 5 AI + Top 5 中国市场 (基于 24h 交易量)
2. 调用 Grok-Reasoning 进行“共识偏差”与“投资含义”深度推演
3. 自动创建结构化飞书云文档 (非源码模式)
4. 推送四层结构高级飞书卡片
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
CLOB_API = "https://clob.polymarket.com"
HISTORY_FILE = Path(__file__).parent / "market_history.json"
BJT = timezone(timedelta(hours=8))

# 环境变量 (由 GitHub Secrets 传入)
FEISHU_WEBHOOK_URL = os.getenv("FEISHU_WEBHOOK_URL", "")
FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")
XAI_API_KEY = os.getenv("XAI_API_KEY", "")

# ─── 关键词与过滤规则 (严格遵守敏感话题过滤) ────────────────────────
AI_KEYWORDS = ["AGI", "GPT", "Claude", "Gemini", "OpenAI", "Anthropic", "DeepSeek", "NVIDIA", "AI chip", "AI model", "LLM", "GPT-5", "Sora", "xAI"]
CHINA_KEYWORDS = ["China GDP", "trade war", "tariff", "trade deal", "semiconductor", "chip ban", "Huawei", "BYD", "PBOC", "Chinese stocks", "China economy", "TikTok ban"]
NOISE_KEYWORDS = ["NBA", "Finals", "Win the game", "Counter-Strike", "NHL", "FIFA", "Esports", "Betting", "Weather", "Taipei temp", "Basketball"]
BLOCKED_PATTERNS = [r"taiwan.*invasi", r"invad.*taiwan", r"xi jinping.*removed", r"china.*coup", r"regime.*change.*china", r"CCP.*collapse"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("polymarket-watch")

# ════════════════════════════════════════════════════════════════
# 1. 核心抓取与清洗逻辑
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
        except Exception as e:
            log.error(f"抓取数据失败: {e}")
            break
    
    ai_pool, china_pool = [], []
    for m in markets:
        q = (m.get('question','') + m.get('description','')).lower()
        # 敏感过滤
        if any(re.search(p, q, re.I) for p in BLOCKED_PATTERNS): continue
        # 噪音过滤
        if any(k.lower() in q for k in NOISE_KEYWORDS): continue
        
        # 匹配 AI
        if any(re.search(rf"\b{re.escape(kw.lower())}\b", q) for kw in AI_KEYWORDS):
            ai_pool.append(m)
        # 匹配 中国
        elif any(re.search(rf"\b{re.escape(kw.lower())}\b", q) for kw in CHINA_KEYWORDS):
            china_pool.append(m)
            
    # 按交易量选出前 5 个
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
            else:
                m["_yes_price"] = None
        except: m["_yes_price"] = None
    return markets

# ════════════════════════════════════════════════════════════════
# 2. 状态管理：对比历史并计算变动 (含兼容性修复)
# ════════════════════════════════════════════════════════════════

def compare_history(markets, now_str):
    if not HISTORY_FILE.exists():
        HISTORY_FILE.write_text(json.dumps({"markets": {}}))
    
    try:
        history = json.loads(HISTORY_FILE.read_text("utf-8"))
    except:
        history = {"markets": {}}
    
    for m in markets:
        # 生成唯一 ID
        slug = re.sub(r'[^a-z0-9]+', '-', m.get('question','').lower()).strip('-')[:80]
        price = m.get("_yes_price")
        if price is None: continue
        
        prev = history.get("markets", {}).get(slug)
        
        # 兼容性读取：优先读 'p'，不满足则读旧版 'current_probability'
        if prev:
            prev_p = prev.get("p") if "p" in prev else prev.get("current_probability")
            if prev_p is not None:
                m["_change"] = round(price - float(prev_p), 4)
                m["_is_new"] = False
            else:
                m["_change"] = 0.0
                m["_is_new"] = True
        else:
            m["_change"] = 0.0
            m["_is_new"] = True
        
        # 更新状态
        history.setdefault("markets", {})[slug] = {"p": price, "t": now_str}
        
    HISTORY_FILE.write_text(json.dumps(history, ensure_ascii=False, indent=2), "utf-8")
    return markets

# ════════════════════════════════════════════════════════════════
# 3. Grok-Reasoning 分析引擎
# ════════════════════════════════════════════════════════════════

def analyze_markets_with_llm(ai_list, china_list):
    if not XAI_API_KEY: return "分析模块未激活：未检测到 XAI_API_KEY。"
    
    data_ctx = "【全球 AI 进展】\n"
    for m in ai_list:
        data_ctx += f"- {m.get('question')}: 当前赔率 {round(m.get('_yes_price',0)*100, 1)}% (变动: {round(m.get('_change',0)*100, 2)}pp)\n"
    data_ctx += "\n【中国宏观与科技】\n"
    for m in china_list:
        data_ctx += f"- {m.get('question')}: 当前赔率 {round(m.get('_yes_price',0)*100, 1)}% (变动: {round(m.get('_change',0)*100, 2)}pp)\n"

    prompt = f"""
你是一个 Polymarket 预测市场监测助手，服务对象是中国的一级市场（VC/PE）AI 投资人。
请基于以下数据生成中文简报。要求逻辑犀利，拒绝废话。

## 核心任务
1. **今日行动提示**：总结 1-2 条最关键信号。格式：信号 -> 含义 -> 建议关注点。
2. **共识偏差预警**：识别赔率 >90% 或 <10% 的市场，给出“共识检验”：指出反驳该共识的一个核心逻辑。
3. **政策-投资联动**：针对“基础模型”、“应用层”、“芯片Infra”、“出海”等赛道。
   格式：如果你在投[具体赛道]，这个信号意味着[具体含义]。

最新数据：
{data_ctx}
"""
    try:
        client = Client(api_key=XAI_API_KEY)
        chat = client.chat.create(model="grok-4.20-0309-reasoning")
        chat.append(system("你是一位见解毒辣的资深一级市场策略师，只说有价值的干货。"))
        chat.append(user(prompt))
        full_res = chat.sample().content.strip()
        # 清除推理标签
        return re.sub(r'<think>.*?</think>', '', full_res, flags=re.DOTALL).strip()
    except Exception as e:
        return f"分析生成失败: {e}"

# ════════════════════════════════════════════════════════════════
# 4. 飞书联动 (文档 + 卡片)
# ════════════════════════════════════════════════════════════════

def get_tenant_token():
    if not (FEISHU_APP_ID and FEISHU_APP_SECRET): return None
    try:
        r = requests.post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal", 
                          json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET}, timeout=10)
        return r.json().get("tenant_access_token")
    except: return None

def push_feishu_doc(title, analysis, ai_list, china_list):
    token = get_tenant_token()
    if not token: return ""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}
    
    try:
        # 1. 创建文档
        r = requests.post("https://open.feishu.cn/open-apis/docx/v1/documents", 
                          headers=headers, json={"title": title}, timeout=20)
        doc_id = r.json().get("data", {}).get("document", {}).get("document_id")
        if not doc_id: return ""

        # 2. 构建结构化 Block 内容
        blocks = [
            {"block_type": 1, "heading1": {"elements": [{"text_run": {"content": "🧠 深度博弈分析 (Grok Reasoning)"}}]}},
            {"block_type": 2, "text": {"elements": [{"text_run": {"content": analysis}}]}},
            {"block_type": 12, "divider": {}},
            {"block_type": 3, "heading2": {"elements": [{"text_run": {"content": "📊 详细赔率清单"}}]}}
        ]
        for m in ai_list + china_list:
            blocks.append({
                "block_type": 2,
                "text": {"elements": [{"text_run": {"content": f"• {m.get('question')}: {round(m.get('_yes_price',0)*100,1)}% ({round(m.get('_change',0)*100,2)}pp)"}}], "style": {"list": {"type": "bullet"}}}
            })

        requests.post(f"https://open.feishu.cn/open-apis/docx/v1/documents/{doc_id}/blocks/{doc_id}/children",
                      headers=headers, json={"children": blocks}, timeout=20)
        
        # 3. 权限：链接阅读
        requests.patch(f"https://open.feishu.cn/open-apis/drive/v1/permissions/{doc_id}/public?type=docx",
                       headers=headers, json={"external_access_entity": "open", "security_entity": "anyone_can_view", "link_share_entity": "anyone_readable"}, timeout=10)
        
        return f"https://hillhousecap.feishu.cn/docx/{doc_id}"
    except: return ""

def push_feishu_card(ai_list, china_list, analysis, doc_url, now):
    if not FEISHU_WEBHOOK_URL: return
    
    sections = analysis.split("\n\n")
    action_part = sections[0][:500] if sections else "暂无行动提示"
    
    elements = [
        {"tag": "div", "text": {"tag": "lark_md", "content": f"**🎯 今日行动提示**\n{action_part}"}},
        {"tag": "hr"},
    ]

    # 添加 AI 精选
    ai_md = "**▌ 🤖 AI 赛道异动**\n"
    for m in sorted(ai_list, key=lambda x: abs(x.get("_change", 0)), reverse=True)[:3]:
        pp = m["_change"] * 100
        color = "red" if pp > 0.5 else ("green" if pp < -0.5 else "grey")
        ai_md += f"• {m.get('question')[:40]}... **{round(m.get('_yes_price',0)*100, 1)}%** (<font color='{color}'>{'+' if pp>0 else ''}{pp:.1f}pp</font>)\n"
    elements.append({"tag": "markdown", "content": ai_md})

    if doc_url:
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "action",
            "actions": [{
                "tag": "button", "text": {"tag": "plain_text", "content": "📄 查阅完整投资决策报告"},
                "url": doc_url, "type": "primary"
            }]
        })

    payload = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": f"Polymarket 资金风向标 | {now.strftime('%m-%d')}"}, "template": "indigo"},
            "elements": elements + [{"tag": "note", "elements": [{"tag": "plain_text", "content": "Insights by Grok-Reasoning"}]}]
        }
    }
    requests.post(FEISHU_WEBHOOK_URL, json=payload, timeout=15)

# ════════════════════════════════════════════════════════════════
# 5. 主程序
# ════════════════════════════════════════════════════════════════

def main():
    now = datetime.now(BJT)
    now_str = now.strftime("%Y-%m-%dT%H:%M")
    log.info(f"=== Polymarket Watch 开始工作 === {now_str}")

    # 1. 抓取与筛选
    ai_top, china_top = fetch_top_markets()
    ai_top = get_real_prices(ai_top)
    china_top = get_real_prices(china_top)
    
    # 2. 历史对冲 (KeyError 修复在此函数内)
    compare_history(ai_top + china_top, now_str)

    # 3. LLM 深度分析
    log.info("正在调用 Grok 分析师进行投研...")
    analysis = analyze_markets_with_llm(ai_top, china_top)
    
    # 4. 生成云文档
    doc_url = ""
    if FEISHU_APP_ID:
        doc_url = push_feishu_doc(f"Polymarket 深度研报 | {now.strftime('%m-%d %H:%M')}", analysis, ai_top, china_top)
        log.info(f"云文档已就绪: {doc_url}")

    # 5. 发送卡片
    push_feishu_card(ai_top, china_top, analysis, doc_url, now)
    log.info("=== 任务顺利完成 ===")

if __name__ == "__main__":
    main()
