import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.metadata_agent import ResourceMetadata, _classify_failure
from app.services.metadata_wiki_judge import run_search_then_judge
from tests.metadata_corpus.dataset import load_corpus


async def main():
    case = next(c for c in load_corpus()[1]["cases"] if c["id"] == "011c6d44-68cf-43a8-bad3-f0398ce20a95")
    model = AsyncMock()
    model.ainvoke.return_value = SimpleNamespace(
        content=json.dumps({"found": False, "reason": "No matching work found", "content_type": "tv"})
    )
    with (
        patch(
            "app.services.metadata_wiki_judge._execute_search_wikipedia",
            AsyncMock(return_value={"success": False, "error": "Wikipedia request failed: timeout"}),
        ),
        patch("app.services.metadata_wiki_judge.web_fallback_judge", AsyncMock(return_value=None)),
    ):
        verdict, info = await run_search_then_judge(
            model,
            case["input"]["title_raw"],
            "wikipedia",
            react_runner=AsyncMock(),
            msg_builder=MagicMock(),
            fallback_sources=[],
        )
    meta = ResourceMetadata(**verdict, search_error=info.get("error"))
    result = {
        "case_id": case["id"],
        "source_failure": "synthetic timeout for all searches",
        "model_verdict": "synthetic not_found",
        "fallback": "disabled",
        "search_info": info,
        "classification": _classify_failure(meta),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    assert result["classification"] == "transient", result


asyncio.run(main())
