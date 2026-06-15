from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
import time
import csv
from datetime import datetime

# ========== 配置 ==========
driver_path = r'C:\Users\Raytine\chromedriver.exe'
service = Service(executable_path=driver_path)

chrome_options = Options()
chrome_options.add_experimental_option("debuggerAddress", "127.0.0.1:9222")

driver = webdriver.Chrome(service=service, options=chrome_options)
print("连接成功！")

# 切换到已打开的标签页（假设是最后一个）
print("当前打开的页面数量:", len(driver.window_handles))
driver.switch_to.window(driver.window_handles[-1])

# ========== 解析当前页公告（支持合并标题） ==========
def get_page_announcements():
    announcements = []
    rows = driver.find_elements(By.CSS_SELECTOR, "table.table tbody tr")
    for row in rows:
        cells = row.find_elements(By.TAG_NAME, "td")
        if len(cells) < 4:
            continue   # 跳过不完整的行

        # 日期：第1列（索引0）
        date_str = cells[0].text.strip()

        # 标题：从第4列（索引3）中合并 headline 和 doc-link 的文本
        fourth_cell = cells[3]
        headline = ""
        doc_link_text = ""
        link_href = ""

        # 提取 headline 文本
        headline_elem = fourth_cell.find_element(By.CSS_SELECTOR, "div.headline") \
            if fourth_cell.find_elements(By.CSS_SELECTOR, "div.headline") else None
        if headline_elem:
            headline = headline_elem.text.strip()

        # 提取 doc-link 中的 <a> 文本和链接
        link_elem = fourth_cell.find_element(By.CSS_SELECTOR, "div.doc-link a") \
            if fourth_cell.find_elements(By.CSS_SELECTOR, "div.doc-link a") else None
        if link_elem:
            doc_link_text = link_elem.text.strip()
            link_href = link_elem.get_attribute("href")
            # 如果链接是相对路径，补全为绝对URL
            if link_href and link_href.startswith("/"):
                link_href = "https://www1.hkexnews.hk" + link_href

        # 合并标题（用空格连接）
        if headline and doc_link_text:
            title = f"{headline} {doc_link_text}"
        elif headline:
            title = headline
        elif doc_link_text:
            title = doc_link_text
        else:
            title = fourth_cell.text.strip()   # 兜底

        # 只保留有效的公告（必须有链接）
        if link_href:
            announcements.append({
                "日期": date_str,
                "标题": title,
                "链接": link_href
            })
    return announcements

# ========== 翻页并收集所有公告 ==========
all_announcements = []
page_num = 1

while True:
    print(f"正在抓取第 {page_num} 页...")
    page_data = get_page_announcements()
    all_announcements.extend(page_data)
    print(f"  本页抓取 {len(page_data)} 条，累计 {len(all_announcements)} 条")

    # 尝试点击下一页
    try:
        next_btn = driver.find_element(By.CSS_SELECTOR, "a.next, li.next a")
        if "disabled" in next_btn.get_attribute("class").lower():
            print("已是最后一页，停止翻页。")
            break
        next_btn.click()
        time.sleep(2)   # 等待页面加载
        page_num += 1
    except Exception as e:
        print("未找到下一页按钮或已是最后一页。")
        break

# ========== 导出 CSV ==========
if all_announcements:
    output_file = f"hkex_announcements_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    with open(output_file, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["日期", "标题", "链接"])
        writer.writeheader()
        writer.writerows(all_announcements)
    print(f"\n✅ 成功导出 {len(all_announcements)} 条公告到文件：{output_file}")
else:
    print("未抓取到任何公告数据，请检查页面结构或列索引。")

driver.quit()