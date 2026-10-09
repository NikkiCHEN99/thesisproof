from ifind_client import search_notice

results = search_notice(
    query="贵州茅台 2024年度报告 营业收入 产品分类",
    time_start="2025-01-01",
    time_end="2026-10-09",
    size=3,
)

# 注意：iFinD 公告返回的是中文键 —— 公告标题 / 日期 / 公告片段内容，
# 直接取 title / date / snippet 会拿到 None，这里做一层兼容回退。
for r in results:
    title = r.get("title") or r.get("公告标题")
    date = r.get("date") or r.get("日期")
    snippet = r.get("snippet") or r.get("公告片段内容") or r.get("资讯内容") or ""
    print(title, date)
    print(snippet[:200])
    print("---")
