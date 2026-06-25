"""
链接分析诊断脚本 - 分步打印 extract_product_listing 的每一层中间产物

用法:
    python scripts/debug_listing.py <URL>

输出:
    STEP 1: HTTP 抓取 (状态码 / HTML 大小 / SPA 数据块计数)
    STEP 2: HTML -> 文本清洗 (清洗后大小 / 文本预览 / 关键字命中)
    STEP 3: Gemini 结构化提取 (完整 JSON)
    STEP 4: 字段完整性审计 (必填字段得分)
"""
import asyncio
import sys
import json
import os
import re

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
from services.gemini_service import GeminiService
from config import settings


URL = sys.argv[1] if len(sys.argv) > 1 else ""
assert URL, "usage: python scripts/debug_listing.py <url>"


async def main():
    print("=" * 80)
    print("[STEP 1] HTTP Fetch")
    print("=" * 80)
    ua = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    try:
        async with httpx.AsyncClient(proxy=None, timeout=httpx.Timeout(30.0)) as c:
            r = await c.get(URL, headers={"User-Agent": ua}, follow_redirects=True)
        print(f"status={r.status_code}")
        print(f"final_url={r.url}")
        print(f"content-type={r.headers.get('content-type')}")
        html = r.text
        print(f"html_size={len(html)} chars")
    except Exception as e:
        print(f"FETCH FAILED: {e}")
        return

    # SPA 数据块扫描（帮助判断是否需要无头浏览器）
    ld_json = re.findall(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
        html, flags=re.DOTALL | re.IGNORECASE,
    )
    mod_data = re.findall(r'window\.__moduleData__\s*=\s*(\{.*?\});?\s*</script>', html, flags=re.DOTALL)
    run_params = re.findall(r'window\.runParams\s*=\s*(\{.*?\});?\s*</script>', html, flags=re.DOTALL)
    print(f"ld+json blobs: {len(ld_json)}")
    print(f"window.__moduleData__ blobs: {len(mod_data)}")
    print(f"window.runParams blobs: {len(run_params)}")

    print()
    print("=" * 80)
    print("[STEP 2] HTML -> Text Cleanup")
    print("=" * 80)
    text = GeminiService._extract_text_from_html(html)
    print(f"text_size={len(text)} chars")
    print("text preview (first 1500 chars):")
    print("-" * 80)
    print(text[:1500])
    print("-" * 80)

    if len(text.strip()) < 50:
        print(">>> ABORT: text too short, Gemini stage skipped <<<")
        return

    print()
    print("=" * 80)
    print("[STEP 3] Gemini Structured Extraction")
    print("=" * 80)
    gemini = GeminiService(settings.GEMINI_API_KEY)
    try:
        result = await gemini.extract_product_listing(URL, use_cache=False)
    except Exception as e:
        print(f"EXCEPTION: {e}")
        return
    if not result:
        print(">>> Gemini returned None / empty <<<")
        return

    print("Gemini JSON:")
    print(json.dumps(result, ensure_ascii=False, indent=2))

    print()
    print("=" * 80)
    print("[STEP 4] Field Completeness Audit")
    print("=" * 80)
    required = [
        "product_name", "category", "key_selling_points",
        "physical_form", "functional_features",
    ]
    score = 0
    for k in required:
        v = result.get(k)
        ok = bool(v) and v != [] and v != {}
        print(f"  [{'x' if ok else ' '}] {k}: {'OK' if ok else 'MISSING/EMPTY'}")
        if ok:
            score += 1
    ksp = result.get("key_selling_points") or []
    print(f"  key_selling_points count: {len(ksp)} (need >=3)")
    ff = result.get("functional_features") or []
    print(f"  functional_features count: {len(ff)}")
    print(f"\nCompleteness score: {score}/{len(required)}")


if __name__ == "__main__":
    asyncio.run(main())
