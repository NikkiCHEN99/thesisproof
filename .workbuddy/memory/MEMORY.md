# thesisproof 项目约定

## 项目定位
投资命题验证台：把投资命题拆解为可验证子问题（LLM），再用 iFinD 的公告/新闻检索结果作为证据。

## 文件职责
- `app.py` —— Streamlit 前端（唯一入口，`streamlit run app.py`）。三栏布局：
  左=子问题+状态，中=证据卡按 support/oppose/unverifiable 分栏，右=冲突标记（红色）+结论卡；
  底部固定合规声明「本产品仅用于投资研究辅助，不构成投资建议。」
- `orchestrator.py` —— 验证编排，`verify_thesis(thesis) -> dict`（拆解→检索→分类→冲突→结论）。
  返回 `{thesis, clarified_thesis, subject, time_range, time_window, sub_questions, sources,
  retrieval_summary, evidence, conflicts, verdict, errors}`
- `llm_client.py` —— DeepSeek 调用 + **所有提示词集中在这里**：`PROMPT_TEMPLATE`（拆解）、
  `CONCLUSION_PROMPT` + `build_conclusion_prompt()`（结论）、`COMPLIANCE_RULES`（合规红线）、
  `detect_prediction_intent()`（预测类命题前置闸门）
- `ifind_client.py` —— iFinD 检索封装，`search_notice` / `search_news`
- `test_ifind.py` / `test_llm.py` —— 检索 / 拆解冒烟测试
- `test.py` —— 早期占位脚本

## 合规红线（改提示词时别漏）
- `COMPLIANCE_RULES` 五条：①禁「涨/跌/买入/卖出/推荐」及同义替换 ②禁承诺收益
  ③结论只能五选一（支持/部分支持/反对/证据不足/存在冲突）④变化条件必须「若……，则结论转弱/转强/存疑」
  ⑤涉及预测涨跌/买卖时机/目标价 → 拒绝并回复「仅做事实梳理」（`REFUSAL_SUMMARY`）。
- 预测类命题有**三道闸门**：命题级 `detect_prediction_intent`（确定性，命中即不检索）→
  提示词第一步合规判断 → `_normalize_verdict` 兜底（summary 含「仅做事实梳理」也算拒绝）。
  **实测光靠提示词不够**：模型会只改 summary 文案却照常给 `partial_support`。
- verdict 结构：`{refused, verdict, summary, change_conditions}`；两个结论提示词都别写死中文 verdict，
  代码用英文 key + `VERDICT_ALIAS` 兼容中文。

## 失败语义（重要，别改坏）
- **来源层证据卡** `sources`（顶层 + 每个子问题下各一份），`status ∈ ok / missing / failed`：
  `missing`=检索成功但空（note「公告层无证据」），`failed`=重试 1 次后仍失败（note「公告层失败」）。
- **重试策略单层化**：`ifind_client.search_*` 与 `llm_client.chat_json` 都有可选 `max_attempts`；
  编排层调用时传 `max_attempts=1`，由编排层自己重试（`SOURCE_ATTEMPTS=2` / `VERDICT_ATTEMPTS=2`），
  避免两层叠加成多次请求。改这两处时注意别把重试又叠起来。
- **短路兜底**（不调 LLM）：全部来源 failed → `insufficient`/`数据获取失败`；
  全部来源为空 → `insufficient`/`未检索到相关证据`；结论两次非法 JSON → `insufficient`/`结论生成失败`。
- `errors` 只记**真问题**（failed、LLM 异常）；`missing` 只进 `retrieval_summary.messages`，不污染 errors。

## 验证方式
- Streamlit 改动优先用官方 `streamlit.testing.v1.AppTest` 做真机验证（会真实加载运行时，
  能抓出桩测试查不出的 API 用法错误），记得先 `at.text_input[0].set_value(...)` 再点按钮。
- 隔离 venv 已装 `streamlit` / `requests` / `python-dotenv`。

## 代码约定
- 对外函数统一「失败降级」：`llm_client` 失败返回 `{}`，`ifind_client` 失败返回 `[]`，
  `verify_thesis` 失败返回带 `verdict.verdict="insufficient"` 的完整结构。均 `print` 错误。
- 新增可选参数时保持**默认值不变**（向后兼容），新增字段一律**纯增量**（勿删除既有键，app.py 依赖它们）。
- 每个对外函数打印带前缀的调试信息，如 `[ifind:search_notice]`、`[llm:parse_thesis]`、`[orchestrator]`。
- iFinD 返回的是**中文键**（`公告标题`/`资讯标题`/`日期`/`公告片段内容`/`资讯内容`/`URL`）。
- 提示词模板一律用 `__XXX__` 占位 + `str.replace`，**不要用 `str.format`**（模板含 JSON 大括号会被误判）。
- 提示词**统一放 `llm_client.py`**，`orchestrator.py` 只保留数据流与 `CLASSIFY_PROMPT`；新增面向用户的提示词别散落到别处。
- iFinD 并发上限 `Semaphore(2)`；但账号级限流（429）仍会触发，靠 `ifind_client` 的退避重试兜底。

## 环境与密钥
- 密钥放 `.env`（已被 `.gitignore` 忽略，内含**真实**密钥，切勿提交/外泄），键名：`DEEPSEEK_API_KEY`、`FUYAO_API_KEY`、`IFIND_MCP_KEY`。
- `llm_client.py` 用 python-dotenv 加载：**显式指定路径** `load_dotenv(Path(__file__).resolve().parent / ".env")`，不用裸 `load_dotenv()`（裸调用在 `python -c`/交互式环境下会退回 CWD 查找而读不到）。
- iFinD skill 路径：`/Users/nikkiwithnicci/.workbuddy/skills/ifind-finance-data`，其 `call.py` 依赖 `requests`。
- `requirements.txt`：`streamlit>=1.29`（`st.container(border=True)` 需要）、`requests`、`python-dotenv`。
