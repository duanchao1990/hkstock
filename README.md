# hkstock
港股公告邮件监控系统

定时检查邮箱，自动提取港交所（HKEX）上市公司公告，并提供 Web 页面和 REST API 查询。
# 用途
港交所要求上市公司须在1小时内将公告同步至企业官网，目前主流方案为收费接口，通常使用香港公司接口年费数万元国内公司接口年费数千元

本方案为免费方案，方便快捷仅须初始化下载存量数据，及每180天更新邮箱授权码即可实现相同功能

# 功能特性
## 邮件自动采集 — 定时（默认15分钟）检查 IMAP 邮箱，解析港交所公告邮件
## 智能解析 — 从邮件中提取日期、标题、链接、公司名称、股票代码
## 本地持久化 — 公告数据存储为 CSV 文件，支持增量更新和去重
## Web 查询界面 — 支持按年份、关键词、股票代码筛选公告
## REST API — 提供公告查询、统计、公司列表等接口
## Webhook 推送 — 新公告可通过 Webhook 推送到外部系统
## OpenAPI 文档 — 自动生成 Swagger 文档（/docs）
快速开始
1. 安装依赖
pip install -r requirements.txt
2. 配置邮箱
编辑 mail_config.yaml：

email:
  server: "imap.qq.com"         # IMAP 服务器地址
  
  username: "your@email.com"    # 邮箱地址
  
  password: "your_password"     # 密码或授权码
  
QQ邮箱需使用授权码，在邮箱设置 → 账户 → POP3/IMAP 服务中生成。

3. 启动服务
python main.py
启动后访问：

Web 页面：http://127.0.0.1:5000
API 文档：http://127.0.0.1:5000/docs
API 接口
接口	方法	说明
/api/announcements	GET	公告列表（分页、筛选）
/api/announcements/stats	GET	公告统计
/api/companies	GET	公司列表
/api/health	GET	健康检查
/api/announcements 参数
参数	类型	默认值	说明
page	int	1	页码
page_size	int	20	每页条数（最大100）
date_from	string	-	起始日期 YYYY-MM-DD
date_to	string	-	截止日期 YYYY-MM-DD
keyword	string	-	标题关键词搜索
stock_code	string	06666	股票代码筛选
示例
# 查询 06666 的公告
curl "http://127.0.0.1:5000/api/announcements?stock_code=06666"

# 查询 2026年6月 的公告
curl "http://127.0.0.1:5000/api/announcements?date_from=2026-06-01&date_to=2026-06-30"

# 按关键词搜索
curl "http://127.0.0.1:5000/api/announcements?keyword=股份購回"
Webhook 推送
在 mail_config.yaml 中配置 webhook_url：

webhook_url: "https://your-webhook.example.com/hook"
推送的 JSON 格式：

{
  "type": "hkstock_announcements",
  "count": 3,
  "announcements": [
    {
      "title": "翌日披露報表 - [股份購回]",
      "link": "https://www1.hkexnews.hk/...",
      "date": "2026-06-12",
      "company_name": "渣打集團",
      "stock_code": "02888"
    }
  ],
  "timestamp": "2026-06-12T19:00:00"
}
数据存储
公告数据保存在 hkex_announcements.csv，字段如下：

字段	说明
日期	YYYY-MM-DD 格式
标题	公告标题
链接	港交所公告 PDF/XLSX 链接
公司名称	上市公司名称
股票代码	5位港股代码
项目结构
# hkstock/
## ├── main.py                    # 主程序（邮件采集 + FastAPI 服务）
## ├── mail_config.yaml           # 邮箱和 Webhook 配置
## ├── annoucement_history.py     # 历史公告数据批量下载
## ├── hkex_announcements.csv     # 公告数据持久化文件
## ├── requirements.txt           # Python 依赖
## ├── templates/
## │   └── index.html             # Web 查询页面
## └── README.md
# 技术栈
## Python 3.10+
## FastAPI — Web 框架
## uvicorn — ASGI 服务器
## imap_tools — IMAP 邮箱客户端
## BeautifulSoup — HTML 解析
## schedule — 定时任务
## PyYAML — 配置管理
