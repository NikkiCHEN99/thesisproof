from llm_client import parse_thesis
import json

result = parse_thesis("贵州茅台2024年盈利改善来自主营业务")
print(json.dumps(result, ensure_ascii=False, indent=2))
