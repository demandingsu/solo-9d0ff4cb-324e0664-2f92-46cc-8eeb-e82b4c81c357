"""FastAPI 入口：POST /query 采集并判定临时停运；POST /evidence 页面取证。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, HTTPException, Response

from .crawler import Crawler
from .engine import reconstruct
from .evidence import EvidenceCrawler, EvidenceError, build_evidence_zip
from .models import EvidenceRequest, QueryRequest, QueryResponse

logger = logging.getLogger("bus_notice_api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(10.0, connect=5.0),
        headers={"User-Agent": "bus-suspension-api/1.0"},
    ) as client:
        app.state.http_client = client
        yield


app = FastAPI(
    title="公交临时停运公告采集 API",
    version="1.1.0",
    description=(
        "从运营方分页发布的线路公告中，还原更正/撤销公告链，"
        "返回查询时刻仍有效的临时停运区间；并提供页面取证接口，"
        "把当次采集的网页字节打包留存供离线核对。"
    ),
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/query", response_model=QueryResponse)
async def query(req: QueryRequest) -> QueryResponse:
    crawler = Crawler(app.state.http_client)
    crawl = await crawler.crawl(req.index_url)

    result = reconstruct(crawl.announcements, req.query_time)

    if crawl.errors:
        logger.warning("抓取过程出现 %d 个错误: %s", len(crawl.errors), crawl.errors[:3])

    if not crawl.list_pages:
        raise HTTPException(
            status_code=502,
            detail={
                "message": "公告首页无法抓取或无任何分页内容",
                "errors": crawl.errors,
            },
        )

    return QueryResponse(
        index_url=req.index_url,
        query_time=req.query_time,
        list_pages_crawled=crawl.list_pages,
        detail_pages_crawled=crawl.detail_pages,
        announcements_parsed=len(crawl.announcements),
        active_suspensions=result.active,
        inactive_chains=result.inactive,
        unmatched_references=result.unmatched,
        skipped_offsite_links=crawl.skipped_offsite,
    )


@app.post(
    "/evidence",
    responses={
        200: {"content": {"application/zip": {"schema": {"type": "string", "format": "binary"}}}}
    },
)
async def evidence(req: EvidenceRequest) -> Response:
    """页面取证：沿同站分页抓取列表及详情，把实际收到的 HTML 字节打成 ZIP。

    成功时返回可下载的证据包（内含 manifest.json 清单）；分页循环、达到
    上限、站外跳转或任一页面抓取/解析失败时返回 502 及具体原因，不生成
    缺页的证据包。
    """
    crawler = EvidenceCrawler(app.state.http_client)
    try:
        result = await crawler.crawl(req.index_url)
    except EvidenceError as exc:
        logger.warning("取证中止 %s: %s", exc.reason, exc.message)
        raise HTTPException(
            status_code=502,
            detail={"reason": exc.reason, "message": exc.message},
        ) from exc

    zip_bytes = build_evidence_zip(result, req.index_url)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    logger.info(
        "取证完成 %s: %d 页, ZIP %d 字节", req.index_url, len(result.captures), len(zip_bytes)
    )
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="evidence-{stamp}.zip"'},
    )
