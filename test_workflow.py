"""测试完整工作流（阶段1~3）- 含商品链接提取 + 动作兼容性检测

运行前请设置以下环境变量（或直接修改代码中的默认值）：
  TEST_VIDEO_URL          原视频公网可访问 URL（mp4）
  TEST_PRODUCT_IMAGE_URL  新商品产品图 URL（jpg/png）
  TEST_PRODUCT_LISTING_URL商品详情页 URL（可选）
示例：
  TEST_VIDEO_URL=https://your-oss/example.mp4 python test_workflow.py
"""
import asyncio
import json
import os
import httpx
import time

API_BASE = os.environ.get("API_BASE", "http://localhost:8000/api/v1")

VIDEO_URL = os.environ.get(
    "TEST_VIDEO_URL",
    "https://your-bucket.oss-cn-beijing.aliyuncs.com/path/to/original.mp4",
)

PRODUCT_IMAGE_URL = os.environ.get(
    "TEST_PRODUCT_IMAGE_URL",
    "https://your-bucket.oss-cn-beijing.aliyuncs.com/path/to/product.jpg",
)

PRODUCT_LISTING_URL = os.environ.get(
    "TEST_PRODUCT_LISTING_URL",
    "https://example.com/product-listing",
)

async def main():
    async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
        # 1. 启动工作流
        print("=" * 60)
        print("启动工作流（full 模式 + 商品链接）...")
        print("=" * 60)
        
        payload = {
            "project_id": f"test_{int(time.time())}",
            "project_name": "宠物便携饮水杯测试",
            "video_url": VIDEO_URL,
            "product_image_url": PRODUCT_IMAGE_URL,
            "product_listing_url": PRODUCT_LISTING_URL,
            "mode": "full"
        }
        
        resp = await client.post(f"{API_BASE}/start-workflow", json=payload)
        print(f"Status: {resp.status_code}")
        result = resp.json()
        print(json.dumps(result, indent=2, ensure_ascii=False))
        
        if resp.status_code != 200:
            print("启动失败!")
            return
        
        project_id = result["project_id"]
        job_id = result["job_id"]
        print(f"\nProject ID: {project_id}")
        print(f"Job ID: {job_id}")
        
        # 2. 轮询任务状态
        print("\n" + "=" * 60)
        print("轮询工作流状态...")
        print("=" * 60)
        
        max_wait = 600  # 最多等待10分钟
        start_time = time.time()
        
        while time.time() - start_time < max_wait:
            await asyncio.sleep(15)
            
            try:
                # 使用 job_manager 查看状态
                status_resp = await client.get(f"{API_BASE}/generation-status/{job_id}")
                if status_resp.status_code == 200:
                    status = status_resp.json()
                    elapsed = int(time.time() - start_time)
                    print(f"[{elapsed}s] Status: {status.get('status')}, Progress: {status.get('progress', 0):.0%}, Message: {status.get('message', '')}")
                    
                    if status.get("status") in ("completed", "failed", "waiting_review"):
                        print("\n最终结果:")
                        print(json.dumps(status, indent=2, ensure_ascii=False))
                        break
                else:
                    # job_id 可能不在 generation_jobs 中（因为用的是 job_manager）
                    elapsed = int(time.time() - start_time)
                    print(f"[{elapsed}s] 工作流仍在执行中...")
            except Exception as e:
                elapsed = int(time.time() - start_time)
                print(f"[{elapsed}s] 查询状态异常: {e}")
        
        print("\n测试完成!")

if __name__ == "__main__":
    asyncio.run(main())
