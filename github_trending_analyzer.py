#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub Trending 每日优质项目自动分析工具
=====================================
功能：抓取GitHub Trending、按领域分类、综合评分、筛选TOP10、输出Markdown报告

前置依赖安装（一键）：
    pip install requests python-dateutil

运行方式：
    python github_trending_analyzer.py

配置TOKEN（见下方 GITHUB_TOKEN），可将API限额从60次/小时提升至5000次/小时
"""

import os
import re
import sys
import json
import time
import logging
import requests
import datetime
from pathlib import Path
from dateutil import parser as date_parser
from collections import defaultdict

# ============================================================
# 全局配置区 —— 按需修改
# ============================================================

# GitHub Personal Access Token
# 申请地址：https://github.com/settings/tokens → Generate new token (classic)
# 只需勾选 public_repo 权限即可；留空则使用匿名模式（60次/小时限额）
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")  # 通过环境变量传入；GitHub Actions 自动注入 GH_PAT secret

# 输出目录（Markdown报告保存位置）
# GitHub Actions 中为相对路径 ./reports；本地运行也兼容
OUTPUT_DIR = Path(os.environ.get("REPORT_DIR", "./reports"))

# 请求超时秒数
REQUEST_TIMEOUT = 15

# 最大重试次数（遭遇限流时）
MAX_RETRIES = 3

# 每次重试等待秒数
RETRY_WAIT = 10

# 每个领域最多抓取的仓库数（用于评分候选池）
REPOS_PER_DOMAIN = 30

# 最终每领域输出TOP N
TOP_N = 10

# 半年无更新过滤阈值（天）
STALE_DAYS = 180

# ============================================================
# 日志配置
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ============================================================
# 模块一：领域分类配置
# 每个领域对应一组GitHub搜索关键词，用于Trending抓取时定向筛选
# ============================================================

DOMAIN_KEYWORDS = {
    "AI大模型": [
        "llm", "large language model", "gpt", "chatgpt", "claude", "gemini",
        "transformers", "fine-tune", "rag", "vector database", "embedding",
        "diffusion model", "text-to-image", "multimodal", "llama", "mistral",
        "qwen", "deepseek", "ollama", "vllm", "langchain", "llamaindex",
    ],
    "量化交易": [
        "quantitative trading", "quant", "algorithmic trading", "backtesting",
        "backtrader", "zipline", "vnpy", "akshare", "tushare", "stock trading",
        "crypto trading", "futures trading", "portfolio optimization",
    ],
    "爬虫": [
        "web scraping", "crawler", "scrapy", "playwright", "selenium",
        "beautifulsoup", "puppeteer", "spider", "scraper", "data collection",
        "anti-detection", "bypass captcha",
    ],
    "前端": [
        "react", "vue", "angular", "svelte", "nextjs", "nuxt", "tailwind",
        "typescript", "javascript framework", "ui library", "component library",
        "css framework", "webpack", "vite", "frontend",
    ],
    "后端": [
        "fastapi", "django", "flask", "spring boot", "golang web", "rust web",
        "nodejs", "express", "nestjs", "microservices", "rest api", "graphql",
        "grpc", "backend framework", "database orm",
    ],
    "自动化脚本": [
        "automation script", "rpa", "automate", "workflow automation",
        "python script", "bash automation", "task automation", "cli tool",
        "productivity tool", "devops automation",
    ],
    "AI Agent": [
        "ai agent", "autonomous agent", "multi-agent", "agentic", "autogpt",
        "opendevin", "devin", "agent framework", "tool use", "function calling",
        "computer use", "browser agent", "coding agent",
    ],
    "数据分析": [
        "data analysis", "data visualization", "pandas", "polars", "duckdb",
        "jupyter", "notebook", "matplotlib", "plotly", "echarts", "dashboard",
        "business intelligence", "etl", "data pipeline", "analytics",
    ],
    "运维工具": [
        "devops", "kubernetes", "docker", "monitoring", "observability",
        "prometheus", "grafana", "ci/cd", "infrastructure", "terraform",
        "ansible", "logging", "alerting", "cloud native", "helm",
    ],
    "交易策略脚本": [
        "trading strategy", "strategy backtest", "technical indicator",
        "macd", "rsi", "moving average", "option strategy", "arbitrage",
        "market making", "hft", "signal generation", "alpha",
    ],
}

# ============================================================
# 模块二：GitHub API 数据抓取
# ============================================================

class GitHubAPI:
    """封装GitHub REST API调用，支持Token认证与自动限流重试"""

    BASE_URL = "https://api.github.com"

    def __init__(self, token: str = ""):
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        if token:
            # 清洗Token：去除空白、不可见字符、非ASCII字符，防止header编码报错
            token_clean = token.strip()
            token_clean = "".join(c for c in token_clean if ord(c) < 128)
            if not token_clean:
                logger.warning("Token清洗后为空，请检查Token内容，退回匿名模式")
            else:
                self.session.headers["Authorization"] = f"Bearer {token_clean}"
                logger.info("GitHub Token 已配置，API限额：5000次/小时")
        else:
            logger.warning("未配置GitHub Token，匿名模式限额：60次/小时")

    def _request(self, url: str, params: dict = None) -> dict | list | None:
        """带重试机制的GET请求"""
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = self.session.get(url, params=params, timeout=REQUEST_TIMEOUT)

                # 触发限流
                if resp.status_code == 403 and "rate limit" in resp.text.lower():
                    reset_ts = int(resp.headers.get("X-RateLimit-Reset", time.time() + 60))
                    wait = max(reset_ts - int(time.time()), RETRY_WAIT)
                    # 等待时间过长（>120秒）时直接跳过，避免长时间阻塞
                    if wait > 120:
                        logger.error(
                            f"API已触达限流上限，需等待 {wait}秒（约{wait//60}分钟）。"
                            f"建议配置 GITHUB_TOKEN 提升限额至5000次/小时后重新运行。"
                        )
                        return None
                    logger.warning(f"API限流，{wait}秒后重试（第{attempt}次）...")
                    time.sleep(wait)
                    continue

                if resp.status_code == 200:
                    return resp.json()

                logger.warning(f"HTTP {resp.status_code}：{url}")
                return None

            except requests.exceptions.Timeout:
                logger.warning(f"请求超时（第{attempt}次）：{url}")
                time.sleep(RETRY_WAIT)
            except requests.exceptions.ConnectionError:
                logger.warning(f"网络连接失败（第{attempt}次），{RETRY_WAIT}秒后重试")
                time.sleep(RETRY_WAIT)
            except Exception as e:
                logger.error(f"未知请求错误：{e}")
                return None

        logger.error(f"已达最大重试次数，跳过：{url}")
        return None

    def search_repos(self, query: str, sort: str = "stars", per_page: int = 30) -> list[dict]:
        """搜索仓库，返回仓库列表"""
        url = f"{self.BASE_URL}/search/repositories"
        params = {
            "q": query,
            "sort": sort,
            "order": "desc",
            "per_page": min(per_page, 100),
        }
        data = self._request(url, params)
        if not data:
            return []
        items = data.get("items", [])
        logger.info(f"  查询 '{query[:50]}' → {len(items)} 个结果")
        return items

    def get_repo_details(self, full_name: str) -> dict | None:
        """获取仓库详情（包含更新时间、open issues等）"""
        url = f"{self.BASE_URL}/repos/{full_name}"
        return self._request(url)

    def get_recent_commits(self, full_name: str, since_days: int = 7) -> int:
        """统计最近N天的提交数"""
        since = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=since_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        url = f"{self.BASE_URL}/repos/{full_name}/commits"
        params = {"since": since, "per_page": 100}
        data = self._request(url, params)
        if isinstance(data, list):
            return len(data)
        return 0

    def get_issues_stats(self, full_name: str) -> dict:
        """
        分析issue质量：
        - 总open issues数
        - 近30天closed issues数（反映维护活跃度）
        - 含负面词汇的issue比例（简易情感分析）
        """
        stats = {"open_issues": 0, "closed_30d": 0, "negative_ratio": 0.0}

        # 获取open issues样本（最多30条）
        url = f"{self.BASE_URL}/repos/{full_name}/issues"
        params = {"state": "open", "per_page": 30, "sort": "created", "direction": "desc"}
        open_issues = self._request(url, params)

        if isinstance(open_issues, list):
            stats["open_issues"] = len(open_issues)
            # 简易负面情感词检测
            negative_words = [
                "broken", "bug", "crash", "error", "fail", "terrible",
                "unusable", "deprecated", "abandoned", "not working", "dead",
            ]
            negative_count = 0
            for issue in open_issues:
                title = (issue.get("title") or "").lower()
                body = (issue.get("body") or "").lower()
                if any(w in title or w in body for w in negative_words):
                    negative_count += 1
            if open_issues:
                stats["negative_ratio"] = negative_count / len(open_issues)

        # 获取近30天closed issues数
        since_30d = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        params_closed = {"state": "closed", "per_page": 50, "since": since_30d}
        closed_issues = self._request(url, params_closed)
        if isinstance(closed_issues, list):
            stats["closed_30d"] = len(closed_issues)

        return stats

    def get_readme_score(self, full_name: str) -> float:
        """
        README完整度评分（0~1）：
        检测关键章节：安装说明、使用示例、徽章、图片/截图
        """
        url = f"{self.BASE_URL}/repos/{full_name}/readme"
        data = self._request(url)
        if not data:
            return 0.0

        import base64
        try:
            content = base64.b64decode(data.get("content", "")).decode("utf-8", errors="ignore").lower()
        except Exception:
            return 0.0

        score = 0.0
        # 长度基础分（> 500字符得基础分）
        if len(content) > 500:
            score += 0.2
        if len(content) > 2000:
            score += 0.1

        # 关键章节检测
        sections = {
            "install": ["install", "installation", "getting started", "pip install", "npm install"],
            "usage": ["usage", "example", "quick start", "how to use", "demo"],
            "feature": ["feature", "功能", "特性", "highlights"],
            "image": ["![", "<img", "screenshot", "preview"],
            "badge": ["shields.io", "badge", "travis", "github actions"],
            "license": ["license", "mit", "apache", "gpl"],
        }
        for section, keywords in sections.items():
            if any(k in content for k in keywords):
                score += 0.1 if section not in ("install", "usage") else 0.15

        return min(score, 1.0)


# ============================================================
# 模块三：综合评分算法
# 权重：新增Star 40% | 近7天提交 20% | 文档/正向评论 30% | 持续维护 10%
# ============================================================

def compute_score(repo: dict, commits_7d: int, issues_stats: dict, readme_score: float) -> dict:
    """
    综合评分（满分100）

    参数说明：
        repo         : GitHub API返回的仓库原始数据
        commits_7d   : 最近7天提交数
        issues_stats : issue统计（open数、30天closed数、负面比例）
        readme_score : README完整度（0~1）

    返回：
        包含各维度分数及总分的字典
    """

    # ---------- ① 新增Star评分（权重40%）----------
    # 使用 stargazers_count 作为热度代理（Trending API无法直接获取增量）
    # 归一化：以10000 Star为满分参考上限
    stars = repo.get("stargazers_count", 0)
    star_score = min(stars / 10000, 1.0) * 40

    # ---------- ② 近7天提交活跃度（权重20%）----------
    # 归一化：以50次/7天为满分参考
    commit_score = min(commits_7d / 50, 1.0) * 20

    # ---------- ③ 文档质量 + 正向评论（权重30%）----------
    # readme完整度（0~1）× 15
    doc_score = readme_score * 15

    # issue正向度（1 - 负面比例）× 15，closed数多表示维护积极
    negative_penalty = issues_stats.get("negative_ratio", 0) * 15
    closed_bonus = min(issues_stats.get("closed_30d", 0) / 20, 1.0) * 5
    comment_score = max(15 - negative_penalty + closed_bonus, 0)

    quality_score = doc_score + comment_score  # 最高30分

    # ---------- ④ 持续维护更新（权重10%）----------
    # 以最后推送时间为依据：30天内满分，逐渐衰减到180天为0
    pushed_at = repo.get("pushed_at", "")
    maintenance_score = 0.0
    if pushed_at:
        try:
            last_push = date_parser.parse(pushed_at).replace(tzinfo=datetime.timezone.utc)
            days_since = (datetime.datetime.now(datetime.timezone.utc) - last_push).days
            if days_since <= 30:
                maintenance_score = 10.0
            elif days_since <= 180:
                maintenance_score = max(10 * (1 - (days_since - 30) / 150), 0)
        except Exception:
            pass

    total = star_score + commit_score + quality_score + maintenance_score

    return {
        "star_score": round(star_score, 2),
        "commit_score": round(commit_score, 2),
        "quality_score": round(quality_score, 2),
        "maintenance_score": round(maintenance_score, 2),
        "total": round(total, 2),
    }


# ============================================================
# 模块四：过滤机制
# 淘汰：半年无更新 / 文档残缺 / 大量负面issue / 仅Demo无落地能力
# ============================================================

def is_low_quality(repo: dict, issues_stats: dict, readme_score: float) -> tuple[bool, str]:
    """
    返回 (是否低质量, 原因说明)
    任一条件命中即过滤
    """
    # 1. 半年无更新
    pushed_at = repo.get("pushed_at", "")
    if pushed_at:
        try:
            last_push = date_parser.parse(pushed_at).replace(tzinfo=datetime.timezone.utc)
            if (datetime.datetime.now(datetime.timezone.utc) - last_push).days > STALE_DAYS:
                return True, f"超过{STALE_DAYS}天未更新"
        except Exception:
            pass

    # 2. README严重残缺（< 0.2分）
    if readme_score < 0.2:
        return True, "README文档严重缺失"

    # 3. 负面issue比例超过50%
    if issues_stats.get("negative_ratio", 0) > 0.5:
        return True, "负面issue比例过高(>50%)"

    # 4. 仅Demo性质（通过名称/描述关键词判断）
    name = (repo.get("name") or "").lower()
    desc = (repo.get("description") or "").lower()
    demo_only_signals = ["demo", "example only", "tutorial only", "hello world", "toy project"]
    if any(s in name or s in desc for s in demo_only_signals):
        # Demo项目Stars过低时才过滤
        if repo.get("stargazers_count", 0) < 500:
            return True, "疑似仅Demo无实际落地能力"

    # 5. Fork仓库占比过高（Fork数远超Star通常无原创价值）
    forks = repo.get("forks_count", 0)
    stars = repo.get("stargazers_count", 1)
    if forks > 0 and forks / stars > 3:
        return True, "Fork比例异常（可能为课程作业集合）"

    return False, ""


# ============================================================
# 模块五：领域分类模块
# 将仓库按关键词匹配到最佳领域
# ============================================================

def classify_repo(repo: dict) -> list[str]:
    """
    返回仓库命中的领域列表（可多领域）
    匹配范围：仓库名、描述、topics标签
    """
    name = (repo.get("name") or "").lower()
    desc = (repo.get("description") or "").lower()
    topics = [t.lower() for t in (repo.get("topics") or [])]
    text = f"{name} {desc} {' '.join(topics)}"

    matched = []
    for domain, keywords in DOMAIN_KEYWORDS.items():
        if any(kw.lower() in text for kw in keywords):
            matched.append(domain)

    return matched if matched else ["其他"]


# ============================================================
# 模块六：核心抓取与评分流程
# ============================================================

def fetch_and_score_domain(api: GitHubAPI, domain: str, keywords: list[str]) -> list[dict]:
    """
    针对单个领域：
    1. 用关键词搜索Trending仓库
    2. 获取每个仓库的详细指标
    3. 过滤低质量项目
    4. 综合评分并排序
    返回已评分、已排序的项目列表
    """
    logger.info(f"\n{'='*50}")
    logger.info(f"处理领域：{domain}")

    # 使用前3个关键词搜索（避免过多API调用）
    seen_full_names = set()
    candidates = []

    search_keywords = keywords[:3]
    for kw in search_keywords:
        # 搜索近期创建 + stars排序（模拟Trending效果）
        query = f"{kw} created:>{(datetime.date.today() - datetime.timedelta(days=30)).isoformat()} stars:>50"
        repos = api.search_repos(query, sort="stars", per_page=20)
        for r in repos:
            fn = r.get("full_name", "")
            if fn and fn not in seen_full_names:
                seen_full_names.add(fn)
                candidates.append(r)
        time.sleep(0.5)  # 礼貌性延迟

    if not candidates:
        logger.warning(f"  {domain} 领域：未找到有效候选项目")
        return []

    logger.info(f"  候选项目数：{len(candidates)}，开始详细评估...")

    scored_repos = []
    for i, repo in enumerate(candidates[:REPOS_PER_DOMAIN]):
        full_name = repo.get("full_name", "")
        logger.info(f"  [{i+1}/{min(len(candidates), REPOS_PER_DOMAIN)}] 评估 {full_name}")

        try:
            # 获取提交数
            commits_7d = api.get_recent_commits(full_name, since_days=7)
            time.sleep(0.3)

            # 获取issue统计
            issues_stats = api.get_issues_stats(full_name)
            time.sleep(0.3)

            # 获取README评分
            readme_score = api.get_readme_score(full_name)
            time.sleep(0.3)

            # 过滤低质量项目
            low_q, reason = is_low_quality(repo, issues_stats, readme_score)
            if low_q:
                logger.info(f"    ⚠ 过滤：{reason}")
                continue

            # 综合评分
            score_detail = compute_score(repo, commits_7d, issues_stats, readme_score)

            # 生成推荐理由
            reason_text = generate_recommendation(repo, commits_7d, issues_stats, readme_score, score_detail)

            scored_repos.append({
                "repo": repo,
                "commits_7d": commits_7d,
                "issues_stats": issues_stats,
                "readme_score": readme_score,
                "score": score_detail,
                "recommendation": reason_text,
                "domain": domain,
            })

        except Exception as e:
            logger.error(f"    评估失败：{e}")
            continue

    # 按总分降序排列
    scored_repos.sort(key=lambda x: x["score"]["total"], reverse=True)
    logger.info(f"  {domain} 有效项目：{len(scored_repos)} 个")
    return scored_repos


# ============================================================
# 模块七：推荐理由生成
# ============================================================

def generate_recommendation(repo: dict, commits_7d: int, issues_stats: dict,
                             readme_score: float, score: dict) -> dict:
    """生成结构化推荐文案（核心用途、适用场景、优缺点）"""

    name = repo.get("name", "")
    desc = repo.get("description") or "暂无描述"
    stars = repo.get("stargazers_count", 0)
    forks = repo.get("forks_count", 0)
    language = repo.get("language") or "未知"
    license_info = (repo.get("license") or {}).get("spdx_id", "无")

    # 核心用途（基于描述）
    core_purpose = desc[:120] if len(desc) > 10 else f"{name} 开源项目"

    # 优点分析
    pros = []
    if stars > 5000:
        pros.append(f"高人气（⭐{stars:,}）社区验证充分")
    if commits_7d >= 10:
        pros.append(f"活跃开发中（近7天{commits_7d}次提交）")
    if readme_score >= 0.6:
        pros.append("文档完善，上手成本低")
    if forks > 1000:
        pros.append(f"Fork数{forks:,}，实际使用者众多")
    if issues_stats.get("closed_30d", 0) >= 5:
        pros.append("issue响应及时，维护质量高")
    if not pros:
        pros.append("代码质量稳定，功能聚焦")

    # 缺点分析
    cons = []
    if readme_score < 0.4:
        cons.append("文档有待完善")
    if commits_7d == 0:
        cons.append("近期提交较少，更新节奏慢")
    if issues_stats.get("negative_ratio", 0) > 0.2:
        cons.append("部分issue反映存在已知问题")
    if language in ["", None, "未知"]:
        cons.append("主要语言不明确")
    if not cons:
        cons.append("暂无明显缺陷")

    # 适用场景（基于领域关键词简单推断）
    applicable_scenes = infer_applicable_scenes(repo)

    return {
        "core_purpose": core_purpose,
        "pros": pros,
        "cons": cons,
        "applicable_scenes": applicable_scenes,
        "language": language,
        "license": license_info,
        "stars": stars,
        "forks": forks,
    }


def infer_applicable_scenes(repo: dict) -> str:
    """根据仓库信息推断适用场景"""
    desc = (repo.get("description") or "").lower()
    topics = " ".join(repo.get("topics") or []).lower()
    text = desc + " " + topics

    scenes = []
    scene_map = {
        "个人开发者日常工具": ["cli", "tool", "utility", "productivity", "automation"],
        "企业生产环境部署": ["production", "enterprise", "scalable", "kubernetes", "docker"],
        "学习研究与原型验证": ["research", "paper", "experiment", "demo", "tutorial"],
        "数据科学与分析": ["data", "analysis", "visualization", "notebook", "pandas"],
        "AI应用开发": ["llm", "agent", "ai", "gpt", "model", "inference"],
        "量化/金融开发": ["trading", "quant", "backtest", "strategy", "finance"],
    }

    for scene, keywords in scene_map.items():
        if any(k in text for k in keywords):
            scenes.append(scene)

    return "、".join(scenes[:2]) if scenes else "通用开发场景"


# ============================================================
# 模块八：结果格式化输出
# ============================================================

def print_console_report(all_results: dict[str, list]):
    """控制台清晰打印各领域TOP10"""
    print("\n" + "=" * 70)
    print(f"  🔥 GitHub Trending 每日精选 | {datetime.date.today()}")
    print("=" * 70)

    for domain, repos in all_results.items():
        if not repos:
            print(f"\n[{domain}] 暂无有效数据")
            continue

        print(f"\n{'─'*70}")
        print(f"  📌 {domain}  (共{len(repos)}个优质项目)")
        print(f"{'─'*70}")

        for rank, item in enumerate(repos[:TOP_N], 1):
            repo = item["repo"]
            score = item["score"]
            rec = item["recommendation"]

            print(f"\n  #{rank}  {repo.get('full_name', 'N/A')}")
            print(f"       ⭐ {rec['stars']:,}  🍴 {rec['forks']:,}  "
                  f"💻 {rec['language']}  📄 {rec['license']}")
            print(f"       🔗 {repo.get('html_url', '')}")
            print(f"       📝 {rec['core_purpose'][:80]}")
            print(f"       🎯 适用：{rec['applicable_scenes']}")
            print(f"       ✅ 优点：{'；'.join(rec['pros'][:2])}")
            print(f"       ⚠ 注意：{'；'.join(rec['cons'][:1])}")
            print(f"       📊 综合评分：{score['total']:.1f}/100  "
                  f"[Star:{score['star_score']:.0f} 提交:{score['commit_score']:.0f} "
                  f"质量:{score['quality_score']:.0f} 维护:{score['maintenance_score']:.0f}]")

    print("\n" + "=" * 70)
    print("  报告生成完毕，详细版已保存至 reports/ 目录")
    print("=" * 70 + "\n")


def save_markdown_report(all_results: dict[str, list]) -> Path:
    """保存结构化Markdown每日报告"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.date.today().isoformat()
    filepath = OUTPUT_DIR / f"github_trending_{today}.md"

    lines = [
        f"# 🔥 GitHub Trending 每日精选报告",
        f"",
        f"> **生成时间**：{datetime.datetime.now(datetime.timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M:%S')}  ",
        f"> **数据来源**：GitHub REST API  ",
        f"> **评分权重**：新增Star 40% | 近7天提交 20% | 文档/评论质量 30% | 持续维护 10%",
        f"",
        f"---",
        f"",
        f"## 目录",
        f"",
    ]

    # 生成目录
    valid_domains = [(d, r) for d, r in all_results.items() if r]
    for domain, repos in valid_domains:
        anchor = domain.replace(" ", "-").replace("/", "")
        lines.append(f"- [{domain}](#{anchor})（{min(len(repos), TOP_N)}个项目）")

    lines.append("")
    lines.append("---")
    lines.append("")

    # 各领域详情
    for domain, repos in all_results.items():
        anchor = domain.replace(" ", "-").replace("/", "")
        lines.append(f"## {domain}")
        lines.append("")

        if not repos:
            lines.append("> ⚠ 本次未抓取到有效数据，请检查网络或稍后重试。")
            lines.append("")
            continue

        for rank, item in enumerate(repos[:TOP_N], 1):
            repo = item["repo"]
            score = item["score"]
            rec = item["recommendation"]
            commits = item["commits_7d"]

            lines += [
                f"### #{rank} [{repo.get('full_name', 'N/A')}]({repo.get('html_url', '')})",
                f"",
                f"| 指标 | 数值 |",
                f"|------|------|",
                f"| ⭐ Stars | {rec['stars']:,} |",
                f"| 🍴 Forks | {rec['forks']:,} |",
                f"| 💻 主语言 | {rec['language']} |",
                f"| 📄 许可证 | {rec['license']} |",
                f"| 🔄 近7天提交 | {commits} 次 |",
                f"| 📊 综合评分 | **{score['total']:.1f} / 100** |",
                f"",
                f"**核心用途**：{rec['core_purpose']}",
                f"",
                f"**适用场景**：{rec['applicable_scenes']}",
                f"",
                f"**优点**：",
            ]
            for pro in rec["pros"]:
                lines.append(f"- ✅ {pro}")

            lines.append("")
            lines.append("**注意事项**：")
            for con in rec["cons"]:
                lines.append(f"- ⚠ {con}")

            lines += [
                f"",
                f"**评分明细**：",
                f"- 新增Star分：{score['star_score']:.1f}/40",
                f"- 提交活跃分：{score['commit_score']:.1f}/20",
                f"- 文档/评论分：{score['quality_score']:.1f}/30",
                f"- 持续维护分：{score['maintenance_score']:.1f}/10",
                f"",
                f"---",
                f"",
            ]

    # 页脚
    lines += [
        f"",
        f"## 说明",
        f"",
        f"本报告由 `github_trending_analyzer.py` 自动生成，数据实时来自 GitHub API。",
        f"",
        f"**过滤规则**：",
        f"- 超过 {STALE_DAYS} 天未更新的仓库已排除",
        f"- README评分低于0.2分的文档缺失项目已排除",
        f"- 负面issue比例超过50%的仓库已排除",
        f"- 疑似仅Demo无落地能力（Stars<500）的项目已排除",
    ]

    with open(filepath, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    logger.info(f"Markdown报告已保存：{filepath}")
    return filepath


# ============================================================
# 主流程入口
# ============================================================

def main():
    logger.info("GitHub Trending 分析器启动...")
    logger.info(f"分析领域：{', '.join(DOMAIN_KEYWORDS.keys())}")

    api = GitHubAPI(token=GITHUB_TOKEN)

    # 检查API可用性
    test = api._request("https://api.github.com/rate_limit")
    if test:
        remaining = test.get("rate", {}).get("remaining", "未知")
        logger.info(f"API剩余请求次数：{remaining}")
    else:
        logger.error("无法连接GitHub API，请检查网络或Token配置")
        sys.exit(1)

    all_results: dict[str, list] = {}

    for domain, keywords in DOMAIN_KEYWORDS.items():
        try:
            scored = fetch_and_score_domain(api, domain, keywords)
            all_results[domain] = scored
        except Exception as e:
            logger.error(f"领域 [{domain}] 处理异常：{e}")
            all_results[domain] = []

    # 控制台输出
    print_console_report(all_results)

    # 保存Markdown报告
    report_path = save_markdown_report(all_results)

    # 统计摘要
    total_valid = sum(len(v) for v in all_results.values())
    logger.info(f"\n✅ 分析完成！共评估 {total_valid} 个有效项目")
    logger.info(f"📄 报告路径：{report_path.absolute()}")

    return all_results


# ============================================================
# 程序入口
# ============================================================

if __name__ == "__main__":
    main()


# ============================================================
# ============================================================
#
#   📘 使用文档
#
# ============================================================
# ============================================================
#
# ── 一、环境准备 ─────────────────────────────────────────────
#
#   Python版本要求：>= 3.10（使用了 dict | None 语法）
#
#   安装依赖（仅两个轻量包）：
#       pip install requests python-dateutil
#
# ── 二、申请GitHub Token ────────────────────────────────────
#
#   1. 登录 GitHub → 右上角头像 → Settings
#   2. 左侧菜单最底部 → Developer settings
#   3. Personal access tokens → Tokens (classic)
#   4. Generate new token (classic)
#   5. 填写Note（如：trending-analyzer）
#   6. Expiration 选 No expiration 或90天
#   7. 勾选权限：public_repo（只读公开仓库即可）
#   8. 点击 Generate token，复制保存
#
#   配置方式（推荐环境变量，避免Token写死在代码里）：
#       # Linux/Mac：
#       export GITHUB_TOKEN="ghp_xxxxxxxxxxxxxxxx"
#
#       # Windows CMD：
#       set GITHUB_TOKEN="ghp_你的token"
#
#       # Windows PowerShell：
#       $env:GITHUB_TOKEN="ghp_xxxxxxxxxxxxxxxx"
#
#   或直接修改脚本第38行：
#       GITHUB_TOKEN = "ghp_xxxxxxxxxxxxxxxx"
#
# ── 三、直接运行 ────────────────────────────────────────────
#
#       python github_trending_analyzer.py
#
#   输出：
#       - 控制台：各领域TOP10项目简报
#       - 文件：./reports/github_trending_YYYY-MM-DD.md
#
# ── 四、定时每日自动运行 ─────────────────────────────────────
#
#   【Linux / Mac - crontab】
#       打开终端，输入：
#           crontab -e
#
#       添加以下行（每天早上8点运行）：
#           0 8 * * * cd /你的脚本目录 && GITHUB_TOKEN=ghp_xxx python3 github_trending_analyzer.py >> /tmp/trending.log 2>&1
#
#       查看定时任务：
#           crontab -l
#
#   【Windows - 任务计划程序】
#       方法一：Win+R 输入 taskschd.msc
#           → 创建基本任务
#           → 触发器：每天，时间 08:00
#           → 操作：启动程序
#               程序：python
#               参数：C:\你的路径\github_trending_analyzer.py
#               起始于：C:\你的路径\
#           → 完成
#
#       方法二：PowerShell一键注册：
#           $action = New-ScheduledTaskAction -Execute "python" -Argument "C:\path\github_trending_analyzer.py" -WorkingDirectory "C:\path"
#           $trigger = New-ScheduledTaskTrigger -Daily -At 8am
#           Register-ScheduledTask -TaskName "GitHubTrending" -Action $action -Trigger $trigger
#
# ── 五、自定义配置说明 ────────────────────────────────────────
#
#   修改文件顶部"全局配置区"：
#
#   GITHUB_TOKEN    : GitHub Token（建议用环境变量）
#   OUTPUT_DIR      : 报告保存目录，默认 ./reports/
#   REQUEST_TIMEOUT : 请求超时秒数，默认15秒
#   MAX_RETRIES     : 限流重试次数，默认3次
#   REPOS_PER_DOMAIN: 每领域候选池大小，默认30个
#   TOP_N           : 每领域输出项目数，默认10个
#   STALE_DAYS      : 过滤无更新天数阈值，默认180天
#
#   修改DOMAIN_KEYWORDS可增减领域或调整搜索关键词
#
# ── 六、常见问题 ──────────────────────────────────────────────
#
#   Q: 运行提示 "API限流"？
#   A: 配置GITHUB_TOKEN后可提升至5000次/小时
#
#   Q: 某领域显示"未找到有效候选项目"？
#   A: 尝试调整该领域的DOMAIN_KEYWORDS关键词，或降低Stars门槛
#
#   Q: 运行时间较长？
#   A: 每个项目需3次API调用，10领域×30项目约需900次调用
#      建议减小REPOS_PER_DOMAIN（如改为10）加快速度
#
# ============================================================
