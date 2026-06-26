import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from persistence.database import Base
from persistence import models  # noqa: F401
from services.database_state_service import DatabaseStateService


async def _service():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, DatabaseStateService(factory)


def test_database_state_service_project_asset_shot_review_flow():
    async def scenario():
        engine, service = await _service()
        try:
            project = await service.create_project(
                name="portfolio",
                video_url="https://cdn.example.com/video.mp4",
                product_image_url="https://cdn.example.com/product.png",
                mode="full",
            )
            project_id = project["id"]
            assert project["fields"]["状态"] == "素材准备中"

            await service.update_project_status(project_id, "ANALYZING")
            updated_project = await service.get_project(project_id)
            assert updated_project["fields"]["状态"] == "ANALYZING"

            await service.save_product_brief_draft(
                project_id,
                {
                    "confidence_score": 0.88,
                    "clarification_items": [{"question": "目标人群？"}],
                },
            )
            brief_state = await service.get_product_brief_state(project_id)
            assert brief_state["draft"]["confidence_score"] == 0.88
            assert brief_state["clarifications"][0]["question"] == "目标人群？"

            asset = await service.create_asset(
                project_id=project_id,
                asset_type="video_analysis",
                content='{"ok": true}',
                attachment_url="https://cdn.example.com/asset.json",
            )
            assert asset["fields"]["素材类型"] == "video_analysis"
            assets = await service.get_project_assets(project_id)
            assert len(assets) == 1

            shots = await service.batch_create_shots(
                [
                    {
                        "project_id": project_id,
                        "sequence_number": 1,
                        "original_shot_description": "old",
                        "new_shot_description": "new",
                        "generation_prompt": "prompt",
                    }
                ]
            )
            shot_id = shots[0]["id"]
            await service.update_shot_prompt_status(shot_id, "已通过", "ok")
            await service.update_shot_keyframe(
                shot_id, "https://cdn.example.com/keyframe.png"
            )
            await service.update_shot_status(
                shot_id, "completed", "https://cdn.example.com/shot.mp4"
            )
            loaded_shots = await service.get_project_shots(project_id)
            assert loaded_shots[0]["fields"]["提示词审核状态"] == "已通过"
            assert loaded_shots[0]["fields"]["关键帧图片"].endswith("keyframe.png")
            assert loaded_shots[0]["fields"]["生成状态"] == "completed"

            review = await service.create_review(
                shot_id=shot_id,
                review_type="视频审核",
                result="通过",
                description="ok",
                suggestion="none",
            )
            assert review["fields"]["审核结果"] == "通过"
            reviews = await service.get_project_reviews(project_id)
            assert len(reviews) == 1
        finally:
            await engine.dispose()

    asyncio.run(scenario())
