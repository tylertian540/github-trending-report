#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub Trending 报告邮件发送脚本
================================
功能：读取最新的 Markdown 报告，转换为 HTML 邮件，通过 QQ 邮箱 SMTP 发送

依赖（已内置，无需额外安装）：
    Python 标准库：smtplib、email、pathlib
    可选：pip install markdown  （用于更好的 Markdown→HTML 转换）

运行：
    python send_report_email.py
"""

import os
import re
import smtplib
import datetime
from pathlib import Path
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders

# ============================================================
# 配置区
# ============================================================

# QQ 邮箱 SMTP 配置
# 本地直接填写；GitHub Actions 通过 Secrets 注入（更安全）
SMTP_HOST     = "smtp.qq.com"
SMTP_PORT     = 465
SMTP_USER     = os.environ.get("SMTP_USER",     "595905760@qq.com")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "jnrqpydlicazbbfg")

# 收件人（支持多个，逗号分隔）
_to_raw = os.environ.get("TO_EMAIL", "595905760@qq.com")
TO_EMAILS = [e.strip() for e in _to_raw.split(",") if e.strip()]

# 报告目录（兼容本地 Windows 路径和 GitHub Actions 相对路径）
REPORTS_DIR = Path(os.environ.get("REPORT_DIR", "./reports"))


# ============================================================
# Markdown → HTML 转换（无需第三方库的简易版）
# ============================================================

def md_to_html(md: str) -> str:
    """将 Markdown 转换为 HTML（内置实现，无需 markdown 库）"""
    lines = md.split("\n")
    html_lines = []
    in_table = False
    in_list  = False

    for line in lines:
        # 标题
        if line.startswith("### "):
            if in_list: html_lines.append("</ul>"); in_list = False
            html_lines.append(f'<h3 style="color:#00b4d8;margin:20px 0 8px;border-bottom:1px solid #e0e0e0;padding-bottom:4px">{line[4:]}</h3>')
        elif line.startswith("## "):
            if in_list: html_lines.append("</ul>"); in_list = False
            html_lines.append(f'<h2 style="color:#0077b6;margin:28px 0 10px;background:#f0f8ff;padding:8px 12px;border-left:4px solid #0077b6">{line[3:]}</h2>')
        elif line.startswith("# "):
            html_lines.append(f'<h1 style="color:#023e8a;font-size:22px;margin:0 0 16px">{line[2:]}</h1>')

        # 分割线
        elif line.strip() == "---":
            html_lines.append('<hr style="border:none;border-top:1px solid #dee2e6;margin:16px 0">')

        # 表格
        elif line.startswith("|"):
            cells = [c.strip() for c in line.split("|")[1:-1]]
            if all(re.match(r'^[-:]+$', c) for c in cells):
                continue  # 跳过分隔行
            if not in_table:
                html_lines.append('<table style="border-collapse:collapse;width:100%;margin:10px 0;font-size:13px">')
                in_table = True
                row_html = "".join(
                    f'<th style="background:#0077b6;color:#fff;padding:6px 12px;text-align:left">{c}</th>'
                    for c in cells
                )
                html_lines.append(f"<tr>{row_html}</tr>")
            else:
                row_html = "".join(
                    f'<td style="padding:5px 12px;border-bottom:1px solid #dee2e6">{c}</td>'
                    for c in cells
                )
                html_lines.append(f"<tr>{row_html}</tr>")
        else:
            if in_table:
                html_lines.append("</table>")
                in_table = False

            # 列表项
            if line.startswith("- "):
                if not in_list:
                    html_lines.append('<ul style="margin:4px 0 8px 20px;line-height:1.8">')
                    in_list = True
                content = line[2:]
                content = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', content)
                html_lines.append(f"<li>{content}</li>")
            else:
                if in_list:
                    html_lines.append("</ul>")
                    in_list = False

                if line.strip() == "":
                    html_lines.append("<br>")
                else:
                    # 内联格式：**粗体** `代码` [链接](url)
                    text = line
                    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
                    text = re.sub(r'`(.+?)`', r'<code style="background:#f1f3f4;padding:1px 4px;border-radius:3px;font-family:monospace">\1</code>', text)
                    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2" style="color:#0077b6">\1</a>', text)
                    text = re.sub(r'^> (.+)', r'<blockquote style="border-left:3px solid #0077b6;padding:4px 12px;margin:8px 0;color:#555;background:#f8f9fa">\1</blockquote>', text)
                    html_lines.append(f"<p style='margin:3px 0'>{text}</p>")

    if in_table: html_lines.append("</table>")
    if in_list:  html_lines.append("</ul>")

    return "\n".join(html_lines)


def build_email_html(md_content: str, report_date: str) -> str:
    """生成完整的 HTML 邮件正文"""
    body_html = md_to_html(md_content)

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f5f5f5;font-family:'Helvetica Neue',Arial,'Microsoft YaHei',sans-serif">
<div style="max-width:800px;margin:20px auto;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,.1)">

  <!-- 顶部 Banner -->
  <div style="background:linear-gradient(135deg,#023e8a,#0077b6);padding:28px 32px;color:#fff">
    <div style="font-size:11px;letter-spacing:3px;opacity:.8;margin-bottom:8px">DAILY INTELLIGENCE REPORT</div>
    <div style="font-size:24px;font-weight:700">🔥 GitHub Trending 每日精选</div>
    <div style="font-size:13px;opacity:.85;margin-top:8px">
      {report_date} · 数据来源：GitHub API · AI大模型 | 量化交易 | 爬虫 | 前端 | 后端 | AI Agent | 数据分析 | 运维
    </div>
  </div>

  <!-- 报告正文 -->
  <div style="padding:28px 32px;font-size:14px;line-height:1.7;color:#333">
    {body_html}
  </div>

  <!-- 底部 -->
  <div style="background:#f8f9fa;padding:16px 32px;font-size:12px;color:#888;border-top:1px solid #eee;text-align:center">
    本报告由 GitHub Trending 自动分析工具生成 · 每日 08:00 自动更新 · 请勿直接回复此邮件
  </div>
</div>
</body>
</html>"""


# ============================================================
# 主发送流程
# ============================================================

def find_latest_report() -> Path | None:
    """找到最新的 Markdown 报告文件"""
    if not REPORTS_DIR.exists():
        return None
    reports = sorted(REPORTS_DIR.glob("github_trending_*.md"))
    return reports[-1] if reports else None


def send_email(report_path: Path) -> bool:
    """读取报告并发送邮件"""
    md_content = report_path.read_text(encoding="utf-8")
    report_date = datetime.date.today().strftime("%Y年%m月%d日")

    # 构建邮件
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🔥 GitHub Trending 每日精选 · {report_date}"
    msg["From"]    = f"GitHub趋势助手 <{SMTP_USER}>"
    msg["To"]      = ", ".join(TO_EMAILS)

    # 纯文本备用版本
    plain_text = re.sub(r'[#*`>\[\]()]', '', md_content)
    msg.attach(MIMEText(plain_text, "plain", "utf-8"))

    # HTML 富文本版本
    html_body = build_email_html(md_content, report_date)
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    # 附件：原始 Markdown 文件
    attachment = MIMEBase("application", "octet-stream")
    attachment.set_payload(md_content.encode("utf-8"))
    encoders.encode_base64(attachment)
    attachment.add_header(
        "Content-Disposition",
        f"attachment; filename=\"github_trending_{datetime.date.today().isoformat()}.md\""
    )
    msg.attach(attachment)

    # 发送
    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_USER, TO_EMAILS, msg.as_string())
        print(f"✅ 邮件已发送至：{', '.join(TO_EMAILS)}")
        return True
    except smtplib.SMTPAuthenticationError:
        print("❌ SMTP 认证失败：请检查 QQ 邮箱授权码是否正确")
        return False
    except smtplib.SMTPException as e:
        print(f"❌ 邮件发送失败：{e}")
        return False
    except Exception as e:
        print(f"❌ 未知错误：{e}")
        return False


def main():
    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] 准备发送 GitHub Trending 报告邮件...")

    report = find_latest_report()
    if not report:
        print(f"❌ 未找到报告文件，请先运行 github_trending_analyzer.py")
        print(f"   报告目录：{REPORTS_DIR}")
        return

    print(f"  报告文件：{report.name}")
    send_email(report)


if __name__ == "__main__":
    main()
