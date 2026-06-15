#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
港股公告邮件监控系统
- 定时检查邮箱，提取港交所公告
- 提供 REST API 查询公告（FastAPI）
- 支持 Webhook 推送新公告
- 自动生成 OpenAPI 文档：http://127.0.0.1:5000/docs
"""

import csv
import re
import sys
import time
import threading
import traceback
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, TypeAlias, cast

import requests
import schedule
import uvicorn
import yaml
from bs4 import BeautifulSoup
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse
from imap_tools import AND, MailBox
from pydantic import BaseModel

# ── 类型定义 ──────────────────────────────────────────
EmailConfig: TypeAlias = dict[str, str]
Announcement: TypeAlias = dict[str, str]

# ── 文件路径 ──────────────────────────────────────────
CONFIG_FILE = "mail_config.yaml"
DATA_FILE = "hkex_announcements.csv"

# ── Pydantic 模型 ──────────────────────────────────────
class AnnouncementItem(BaseModel):
    title: str
    link: str
    date: str
    source_subject: str = ""
    company_name: str = ""
    stock_code: str = ""


class AnnouncementListData(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[AnnouncementItem]


class ApiResponse(BaseModel):
    code: int
    data: AnnouncementListData | dict[str, int | str] | str


class StatsData(BaseModel):
    total: int
    by_date: dict[str, int]
    latest_date: str


class HealthData(BaseModel):
    status: str
    time: str


# ========================================================
# 配置加载
# ========================================================
def load_config() -> dict[str, object]:
    """加载 YAML 配置文件"""
    config_path = Path(CONFIG_FILE)
    if not config_path.exists():
        raise FileNotFoundError(f"配置文件 {CONFIG_FILE} 未找到")
    with open(config_path, "r", encoding="utf-8") as f:
        data: dict[str, object] = cast(dict[str, object], yaml.safe_load(f))
    return data


def get_email_config() -> EmailConfig:
    data = load_config()
    return cast(EmailConfig, data.get("email", {}))


def get_webhook_url() -> str:
    data = load_config()
    return cast(str, data.get("webhook_url", ""))


# ========================================================
# 公告数据持久化（CSV）
# ========================================================
CSV_FIELDS = ["date", "title", "link"]


def _normalize_date(date_str: str) -> str:
    """统一日期格式为 YYYY-MM-DD"""
    if not date_str:
        return ""
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(date_str, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return date_str


def load_announcements() -> list[Announcement]:
    """从 CSV 文件加载所有公告，自动检测编码，统一日期格式"""
    path = Path(DATA_FILE)
    if not path.exists():
        return []
    result: list[Announcement] = []
    # 自动检测编码：先试 utf-8-sig，失败则用 gbk
    encoding = "utf-8-sig"
    try:
        with open(path, "r", encoding="utf-8-sig") as test_f:
            _ = test_f.read(1)
    except (UnicodeDecodeError, UnicodeError):
        encoding = "gbk"

    with open(path, "r", encoding=encoding) as f:
        reader = csv.DictReader(f)
        for row in reader:
            date_val = _normalize_date(row.get("日期", row.get("date", "")))
            title_val = row.get("标题", row.get("title", ""))
            link_val = row.get("链接", row.get("link", ""))
            company_val = row.get("公司名称", row.get("company_name", ""))
            code_val = row.get("股票代码", row.get("stock_code", ""))
            if link_val:
                result.append({
                    "date": date_val,
                    "title": title_val,
                    "link": link_val,
                    "company_name": company_val,
                    "stock_code": code_val,
                })
    # 按日期倒序
    result.sort(key=lambda x: x["date"], reverse=True)
    return result


def _save_csv(anns: list[Announcement]) -> None:
    """保存公告到 CSV 文件"""
    with open(DATA_FILE, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["日期", "标题", "链接", "公司名称", "股票代码"])
        writer.writeheader()
        for ann in anns:
            writer.writerow({
                "日期": ann["date"],
                "标题": ann["title"],
                "链接": ann["link"],
                "公司名称": ann.get("company_name", ""),
                "股票代码": ann.get("stock_code", ""),
            })


def add_new_announcements(new_anns: list[Announcement]) -> list[Announcement]:
    """添加新公告，按 link 去重 + CSV 增量写入，返回本次新增的公告"""
    existing = load_announcements()
    existing_links: set[str] = {a["link"] for a in existing}
    added: list[Announcement] = []
    for ann in new_anns:
        if ann["link"] not in existing_links:
            ann["date"] = _normalize_date(ann.get("date", ""))
            existing.append(ann)
            existing_links.add(ann["link"])
            added.append(ann)
    if added:
        existing.sort(key=lambda x: x["date"], reverse=True)
        _save_csv(existing)
    return added


# ========================================================
# 邮件解析
# ========================================================
def _extract_date_from_link(link: str) -> str:
    """从港交所公告链接中提取日期，如 /2026/0612/ → 2026-06-12"""
    m = re.search(r'/(\d{4})/(\d{2})(\d{2})/', link)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return ""


def _parse_company_line(line: str) -> tuple[str, str]:
    """从"公司名 (股票代码)"中拆分出公司名和股票代码，如 '渣打集團 (02888)' → ('渣打集團', '02888')"""
    m = re.search(r'(.+?)\s*\((\d{4,6})\)', line)
    if m:
        return m.group(1).strip(), m.group(2)
    return line.strip(), ""


def _parse_plain_text_body(text: str) -> list[dict[str, str]]:
    """解析纯文本格式的港交所公告邮件正文，按段落拆分为多条公告

    典型结构（每个公告块）：
        渣打集團 (02888)
        翌日披露報表 - [股份購回]
        翌日披露報表
        https://www1.hkexnews.hk/.../2026/0612/2026061200605_c.pdf

    返回列表，每项含 title（公告标题）、company（公司名+股票代码）、
    company_name（公司名）、stock_code（股票代码）
    """
    # 去掉"上市公司訊息提示："等前缀行
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    # 过滤掉明显是提示语的行
    skip_prefixes = ("上市公司訊息提示", "上市公司的訊息提示", "以下是港交所上市公司公告", "---", "===")
    filtered: list[str] = []
    for line in lines:
        if any(line.startswith(p) for p in skip_prefixes):
            continue
        filtered.append(line)

    if not filtered:
        return []

    # 按链接分组：每条公告以链接结尾，往前找公司名和标题
    result: list[dict[str, str]] = []
    link_indices: list[int] = []
    for i, line in enumerate(filtered):
        if re.search(r'https?://(?:www\d*\.)?hkexnews\.hk/', line):
            link_indices.append(i)

    if not link_indices:
        return []

    for idx in link_indices:
        link = filtered[idx]
        title = ""
        company = ""
        # 往前找标题和公司名，遇到空行或另一个链接则停止
        prev_lines: list[str] = []
        for j in range(idx - 1, max(idx - 5, -1), -1):
            line = filtered[j]
            # 遇到另一个链接或空行则停止向上搜索
            if re.search(r'https?://', line) or not line:
                break
            prev_lines.insert(0, line)

        if not prev_lines:
            result.append({
                "title": "", "company": "", "company_name": "", "stock_code": "", "link": link.strip(),
            })
            continue

        # 分离公司行（含括号+数字）和标题候选行
        company_lines: list[str] = []
        title_candidates: list[str] = []
        for line in prev_lines:
            if re.search(r'\(\d{4,6}\)', line):
                company_lines.append(line)
            else:
                title_candidates.append(line)

        # 公司名取最后一个（通常只有一行）
        company = company_lines[-1] if company_lines else ""

        # 标题策略：取 title_candidates 的倒数第二个（如果有多个），否则取第一个
        # 因为典型结构: 公司行 / 标题行 / 副标题行（紧邻链接）
        if len(title_candidates) >= 2:
            title = title_candidates[-2]  # 跳过紧邻链接的副标题行
        elif title_candidates:
            title = title_candidates[0]

        # 如果标题仍为空，用公司名
        if not title:
            title = company or "\u672a\u77e5\u516c\u544a"

        # 拆分公司名和股票代码
        company_name, stock_code = _parse_company_line(company)

        result.append({
            "title": title.strip(),
            "company": company.strip(),
            "company_name": company_name,
            "stock_code": stock_code,
            "link": link.strip(),
        })

    return result


def extract_announcements_from_email(
    subject: str, html_body: str, email_date: datetime | None = None
) -> list[Announcement]:
    """解析港交所公告邮件，提取标题、日期、链接

    支持两种邮件格式：
    1. HTML 格式（带 <a> 标签）
    2. 纯文本格式（逐行解析）
    """
    announcements: list[Announcement] = []

    # ── 获取纯文本正文 ──
    soup = BeautifulSoup(html_body, "html.parser")
    plain_text = soup.get_text(separator="\n", strip=True)

    # ── 提取所有港交所链接 ──
    link_pattern = re.compile(r'(https?://(?:www\d*\.)?hkexnews\.hk[^\s"\'<>]+)')
    raw_links: list[str] = cast(list[str], re.findall(link_pattern, html_body))

    # 去重
    seen: set[str] = set()
    links: list[str] = []
    for link in raw_links:
        if link not in seen:
            seen.add(link)
            links.append(link)

    if not links:
        return []

    # 默认日期：邮件日期 > 今天
    fallback_date = email_date.strftime("%Y-%m-%d") if email_date else datetime.now().strftime("%Y-%m-%d")

    # ── 策略1: 纯文本逐行解析（优先，更可靠） ──
    text_parsed = _parse_plain_text_body(plain_text)
    if text_parsed:
        # 按链接建立索引
        parsed_map: dict[str, dict[str, str]] = {p["link"]: p for p in text_parsed}
        for link in links:
            info = parsed_map.get(link)
            title = info["title"] if info else ""
            company_name = info.get("company_name", "") if info else ""
            stock_code = info.get("stock_code", "") if info else ""
            # 如果有公司名，拼到标题里
            if info and info.get("company"):
                company = info["company"]
                if company not in title:
                    title = f"{company} {title}"

            # 日期优先从链接提取
            extracted_date = _extract_date_from_link(link) or fallback_date

            if not title:
                title = subject

            announcements.append({
                "title": title.strip(),
                "link": link,
                "date": _normalize_date(extracted_date),
                "source_subject": subject,
                "company_name": company_name,
                "stock_code": stock_code,
            })
        return announcements

    # ── 策略2: HTML <a> 标签解析（回退） ──
    for link in links:
        title = ""
        a_tag = soup.find("a", href=re.compile(re.escape(link[:60])))
        if a_tag:
            title = a_tag.get_text(strip=True)
            if not title or len(title) < 5:
                parent = a_tag.parent
                if parent:
                    title = parent.get_text(separator=" ", strip=True)
                    if len(title) > 150:
                        title = title[:150]

        if not title:
            title = subject

        # 日期优先从链接提取
        extracted_date = _extract_date_from_link(link) or fallback_date

        # 再尝试从标题提取日期作为补充
        date_match = re.search(r'(\d{2,4}[/-]\d{1,2}[/-]\d{1,2})', title)
        if date_match:
            extracted_date = date_match.group(1)

        announcements.append({
            "title": title.strip(),
            "link": link,
            "date": _normalize_date(extracted_date),
            "source_subject": subject,
            "company_name": "",
            "stock_code": "",
        })

    return announcements


# ========================================================
# Webhook 推送
# ========================================================
def push_to_webhook(anns: list[Announcement]) -> bool:
    """将新公告推送到配置的 Webhook 地址"""
    webhook_url = get_webhook_url()
    if not webhook_url:
        print("  [推送] 未配置 webhook_url，跳过推送")
        return False

    try:
        payload = {
            "type": "hkstock_announcements",
            "count": len(anns),
            "announcements": anns,
            "timestamp": datetime.now().isoformat(),
        }
        resp = requests.post(
            webhook_url,
            json=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
            timeout=10,
        )
        print(f"  [推送] Webhook 响应: {resp.status_code} {resp.text[:100]}")
        return resp.status_code < 400
    except Exception as e:
        print(f"  [推送] 失败: {e}")
        return False


# ========================================================
# 邮箱检查主逻辑
# ========================================================
def check_email() -> None:
    """连接邮箱，提取港交所公告"""
    print(f"[{datetime.now()}] 开始检查邮箱...")
    email_config = get_email_config()
    try:
        with MailBox(email_config["server"]).login(
            email_config["username"],
            email_config["password"],
            initial_folder="INBOX",
        ) as mailbox:
            since = (datetime.now() - timedelta(days=7)).date()
            print(f"  搜索最近7天的邮件 (since={since})...")
            msg_count = 0
            all_new: list[Announcement] = []

            for msg in mailbox.fetch(AND(date_gte=since), mark_seen=False):
                msg_count += 1
                sender = msg.from_
                # 筛选港交所相关发件人
                if not any(
                    addr in sender.lower()
                    for addr in ("hkexnews", "hkex.com.hk", "hkexnews.hk")
                ):
                    continue

                print(f"  [{msg.date}] {sender}")
                print(f"  主题: {msg.subject[:80]}")

                html = msg.html or msg.text or ""
                # 解析邮件日期
                email_dt: datetime | None = None
                if msg.date:
                    try:
                        email_dt = msg.date.replace(tzinfo=None)  # type: ignore[call-arg]
                    except Exception:
                        pass

                announcements = extract_announcements_from_email(
                    msg.subject, html, email_dt
                )
                if announcements:
                    added = add_new_announcements(announcements)
                    if added:
                        print(f"  >>> 新增 {len(added)} 条公告:")
                        for ann in added:
                            print(f"      [{ann['date']}] {ann['title'][:60]}")
                            print(f"       {ann['link'][:80]}")
                        all_new.extend(added)
                    else:
                        print(f"  (已有 {len(announcements)} 条，无新增)")
                else:
                    print(f"  (未提取到公告链接)")

            print(f"  共扫描 {msg_count} 封邮件")

            # 推送新增公告
            if all_new:
                _ = push_to_webhook(all_new)

    except Exception as e:
        print(f"检查出错: {e}")
        traceback.print_exc()


# ========================================================
# FastAPI 生命周期 & 应用
# ========================================================
@asynccontextmanager
async def lifespan(_app: FastAPI):
    """应用生命周期：启动时开始邮箱检查，关闭时清理"""
    checker_thread = threading.Thread(target=run_email_checker, daemon=True)
    checker_thread.start()
    yield


app = FastAPI(
    title="港股公告邮件监控系统",
    description="定时检查邮箱提取港交所公告，提供 REST API 查询",
    version="1.0.0",
    lifespan=lifespan,
)


# ========================================================
# FastAPI API 路由
# ========================================================
@app.get("/api/announcements", response_model=dict[str, object])
def api_get_announcements(
    page: Annotated[int, Query(ge=1, description="页码")] = 1,
    page_size: Annotated[int, Query(ge=1, le=100, description="每页条数")] = 20,
    date_from: Annotated[str | None, Query(description="起始日期 YYYY-MM-DD")] = None,
    date_to: Annotated[str | None, Query(description="截止日期 YYYY-MM-DD")] = None,
    keyword: Annotated[str | None, Query(description="标题关键词搜索")] = None,
    stock_code: Annotated[str | None, Query(description="股票代码筛选，默认 06666")] = None,
) -> JSONResponse:
    """获取公告列表，支持分页和日期/关键词/股票代码筛选"""
    anns = load_announcements()

    # 股票代码筛选（默认 06666）
    effective_code = stock_code if stock_code is not None else "06666"
    anns = [a for a in anns if a.get("stock_code", "") == effective_code]

    # 其他筛选
    if date_from:
        anns = [a for a in anns if a.get("date", "") >= date_from]
    if date_to:
        anns = [a for a in anns if a.get("date", "") <= date_to]
    if keyword:
        anns = [a for a in anns if keyword.lower() in a.get("title", "").lower()]

    total = len(anns)
    start = (page - 1) * page_size
    end = start + page_size
    page_data = anns[start:end]

    return JSONResponse(content={
        "code": 0,
        "data": {
            "total": total,
            "page": page,
            "page_size": page_size,
            "items": page_data,
        },
    })


@app.get("/api/announcements/stats", response_model=dict[str, object])
def api_get_stats(
    stock_code: Annotated[str | None, Query(description="股票代码筛选，默认 06666")] = None,
) -> JSONResponse:
    """获取公告统计信息"""
    anns = load_announcements()

    # 股票代码筛选（默认 06666）
    effective_code = stock_code if stock_code is not None else "06666"
    anns = [a for a in anns if a.get("stock_code", "") == effective_code]

    dates: dict[str, int] = {}
    for ann in anns:
        d = ann.get("date", "unknown")
        dates[d] = dates.get(d, 0) + 1

    return JSONResponse(content={
        "code": 0,
        "data": {
            "total": len(anns),
            "by_date": dates,
            "latest_date": anns[0].get("date", "") if anns else "",
        },
    })


@app.get("/api/companies", response_model=dict[str, object])
def api_get_companies() -> JSONResponse:
    """获取所有已记录的公司名称及股票代码列表（去重）"""
    anns = load_announcements()
    seen: set[str] = set()
    companies: list[dict[str, str]] = []
    for ann in anns:
        code = ann.get("stock_code", "")
        name = ann.get("company_name", "")
        if code and code not in seen:
            seen.add(code)
            companies.append({"stock_code": code, "company_name": name})
    # 按股票代码排序
    companies.sort(key=lambda x: x["stock_code"])
    return JSONResponse(content={
        "code": 0,
        "data": companies,
    })


@app.get("/api/health", response_model=dict[str, object])
def api_health() -> JSONResponse:
    """健康检查"""
    return JSONResponse(content={
        "code": 0,
        "status": "ok",
        "time": datetime.now().isoformat(),
    })


# ── 前端页面 ────────────────────────────────────────
TEMPLATES_DIR = Path(__file__).parent / "templates"


@app.get("/")
def serve_index() -> FileResponse:
    """返回公告展示首页"""
    return FileResponse(TEMPLATES_DIR / "index.html")


# ========================================================
# 主入口
# ========================================================
def run_email_checker() -> None:
    """后台线程：定时检查邮箱"""
    _ = schedule.every(15).minutes.do(check_email)  # pyright: ignore[reportUnknownMemberType]
    # 启动后立即执行一次
    check_email()
    while True:
        schedule.run_pending()
        time.sleep(60)


def main() -> None:
    print("=" * 55)
    print("  港股公告邮件监控系统 (FastAPI)")
    print("=" * 55)

    # 验证配置
    try:
        cfg = get_email_config()
        print(f"  邮件服务器: {cfg.get('server')}")
        print(f"  邮箱账户:   {cfg.get('username')}")
    except Exception as e:
        print(f"  配置错误: {e}")
        sys.exit(1)

    webhook = get_webhook_url()
    print(f"  Webhook:    {'已配置' if webhook else '未配置'}")
    print(f"  API 文档:   http://127.0.0.1:5000/docs")
    print(f"  API 地址:   http://127.0.0.1:5000/api/announcements")
    print("=" * 55)

    # 启动 FastAPI 服务（lifespan 中自动启动邮箱检查线程）
    uvicorn.run(app, host="0.0.0.0", port=5000, log_level="info")


if __name__ == "__main__":
    main()
