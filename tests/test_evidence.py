"""页面取证：同站抓取、重定向处理、失败原因与 ZIP 证据包。

末三个用例为接口级测试；其中 test_endpoint_evidence_zip 需要本地示例站
运行在 127.0.0.1:8080（同 test_api_integration.py 的前置条件）。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile

import httpx
import pytest

from app.evidence import EvidenceCrawler, EvidenceError, build_evidence_zip
from app.main import app

SITE = os.environ.get("TEST_SITE_URL", "http://127.0.0.1:8080/index.html")
BASE = "http://site.test"
INDEX = f"{BASE}/index.html"

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------- 造页工具

def list_page(notice_ids: list[str], next_href: str | None = None,
              extra_links: list[str] | None = None) -> bytes:
    items = "".join(
        f'<article class="notice" data-id="{nid}" data-kind="suspension">'
        f'<a class="detail-link" href="/notices/{nid}.html">{nid}</a></article>'
        for nid in notice_ids
    )
    for href in extra_links or []:
        items += (
            '<article class="notice" data-id="X" data-kind="suspension">'
            f'<a class="detail-link" href="{href}">外部</a></article>'
        )
    pager = (
        f'<nav class="pager"><a rel="next" href="{next_href}">下一页</a></nav>'
        if next_href
        else ""
    )
    return f"<html><body><h1>列表</h1>{items}{pager}</body></html>".encode()


def detail_page(notice_id: str) -> bytes:
    return f"""<html><body><article>
<h1 class="notice-title">{notice_id}</h1>
<table class="notice-fields">
  <tr data-field="notice_id"><th>公告编号</th><td>{notice_id}</td></tr>
  <tr data-field="kind"><th>类型</th><td>停运</td></tr>
  <tr data-field="published_at"><th>发布时间</th>
    <td><time datetime="2026-11-08T09:00:00+08:00">2026-11-08 09:00:00+08:00</time></td></tr>
</table></article></body></html>""".encode()


def make_client(routes: dict[str, httpx.Response],
                log: list[str] | None = None) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if log is not None:
            log.append(url)
        if url in routes:
            return routes[url]
        return httpx.Response(404, text="not found")

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def ok(body: bytes) -> httpx.Response:
    return httpx.Response(200, content=body,
                          headers={"content-type": "text/html; charset=utf-8"})


def redirect(location: str, status: int = 302) -> httpx.Response:
    return httpx.Response(status, headers={"location": location})


def simple_routes() -> dict[str, httpx.Response]:
    """两页列表 + 两条详情的最小站点。"""
    return {
        INDEX: ok(list_page(["N-1"], next_href="/page2.html")),
        f"{BASE}/page2.html": ok(list_page(["N-2"])),
        f"{BASE}/notices/N-1.html": ok(detail_page("N-1")),
        f"{BASE}/notices/N-2.html": ok(detail_page("N-2")),
    }


# ---------------------------------------------------------------- 抓取行为

async def test_collects_list_and_detail_pages_in_order():
    async with make_client(simple_routes()) as client:
        result = await EvidenceCrawler(client).crawl(INDEX)

    assert [c.page_type for c in result.captures] == ["list", "list", "detail", "detail"]
    assert [c.seq for c in result.captures] == [1, 2, 3, 4]
    first = result.captures[0]
    assert first.request_url == INDEX
    assert first.final_url == INDEX
    assert first.sha256 == hashlib.sha256(first.content).hexdigest()
    assert result.captures[2].notice_id == "N-1"


async def test_same_site_redirect_followed_and_final_url_recorded():
    routes = simple_routes()
    routes[INDEX] = redirect("/list.html")
    routes[f"{BASE}/list.html"] = ok(list_page(["N-1"]))
    async with make_client(routes) as client:
        result = await EvidenceCrawler(client).crawl(INDEX)

    first = result.captures[0]
    assert first.request_url == INDEX
    assert first.final_url == f"{BASE}/list.html"
    assert len(result.captures) == 2  # 列表 + 1 条详情


async def test_offsite_redirect_blocked_before_visiting_target():
    log: list[str] = []
    routes = {INDEX: redirect("http://evil.example.org/steal")}
    async with make_client(routes, log) as client:
        with pytest.raises(EvidenceError) as exc_info:
            await EvidenceCrawler(client).crawl(INDEX)

    assert exc_info.value.reason == "offsite_redirect_blocked"
    assert "evil.example.org" in exc_info.value.message
    # 目标地址从未被访问
    assert log == [INDEX]


async def test_pagination_loop_detected():
    routes = {
        INDEX: ok(list_page(["N-1"], next_href="/page2.html")),
        f"{BASE}/page2.html": ok(list_page(["N-1"], next_href="/index.html")),
        f"{BASE}/notices/N-1.html": ok(detail_page("N-1")),
    }
    async with make_client(routes) as client:
        with pytest.raises(EvidenceError) as exc_info:
            await EvidenceCrawler(client).crawl(INDEX)
    assert exc_info.value.reason == "pagination_loop"
    assert "index.html" in exc_info.value.message


async def test_pagination_loop_via_redirect_detected():
    routes = {
        INDEX: ok(list_page(["N-1"], next_href="/page2.html")),
        f"{BASE}/page2.html": redirect("/index.html"),
        f"{BASE}/notices/N-1.html": ok(detail_page("N-1")),
    }
    async with make_client(routes) as client:
        with pytest.raises(EvidenceError) as exc_info:
            await EvidenceCrawler(client).crawl(INDEX)
    assert exc_info.value.reason == "pagination_loop"


async def test_list_page_limit_exceeded():
    routes = {
        INDEX: ok(list_page(["N-1"], next_href="/page2.html")),
        f"{BASE}/page2.html": ok(list_page(["N-2"])),
        f"{BASE}/notices/N-1.html": ok(detail_page("N-1")),
        f"{BASE}/notices/N-2.html": ok(detail_page("N-2")),
    }
    async with make_client(routes) as client:
        with pytest.raises(EvidenceError) as exc_info:
            await EvidenceCrawler(client, max_list_pages=1).crawl(INDEX)
    assert exc_info.value.reason == "limit_exceeded"
    assert "1" in exc_info.value.message


async def test_detail_limit_exceeded():
    async with make_client(simple_routes()) as client:
        with pytest.raises(EvidenceError) as exc_info:
            await EvidenceCrawler(client, max_detail_pages=1).crawl(INDEX)
    assert exc_info.value.reason == "limit_exceeded"


async def test_fetch_failure_http_status():
    routes = {INDEX: httpx.Response(500, text="boom")}
    async with make_client(routes) as client:
        with pytest.raises(EvidenceError) as exc_info:
            await EvidenceCrawler(client).crawl(INDEX)
    assert exc_info.value.reason == "fetch_failed"
    assert "500" in exc_info.value.message


async def test_fetch_failure_network_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(EvidenceError) as exc_info:
            await EvidenceCrawler(client).crawl(INDEX)
    assert exc_info.value.reason == "fetch_failed"


async def test_too_many_redirects():
    routes = {INDEX: redirect("/index.html")}  # 自跳转
    async with make_client(routes) as client:
        with pytest.raises(EvidenceError) as exc_info:
            await EvidenceCrawler(client, max_redirect_hops=3).crawl(INDEX)
    assert exc_info.value.reason == "fetch_failed"
    assert "重定向" in exc_info.value.message


async def test_list_page_structure_unrecognized():
    routes = {INDEX: ok(b"<html><body><p>nothing here</p></body></html>")}
    async with make_client(routes) as client:
        with pytest.raises(EvidenceError) as exc_info:
            await EvidenceCrawler(client).crawl(INDEX)
    assert exc_info.value.reason == "parse_failed"


async def test_detail_parse_failure_aborts():
    bad = b"<html><body><table class='notice-fields'></table></body></html>"
    routes = {
        INDEX: ok(list_page(["N-1"])),
        f"{BASE}/notices/N-1.html": ok(bad),
    }
    async with make_client(routes) as client:
        with pytest.raises(EvidenceError) as exc_info:
            await EvidenceCrawler(client).crawl(INDEX)
    assert exc_info.value.reason == "parse_failed"
    assert "N-1" in exc_info.value.message


async def test_same_final_url_captured_only_once():
    log: list[str] = []
    routes = {
        INDEX: ok(list_page(["N-1"], extra_links=["/notices/alias.html"])),
        f"{BASE}/notices/N-1.html": ok(detail_page("N-1")),
        f"{BASE}/notices/alias.html": redirect("/notices/N-1.html"),
    }
    async with make_client(routes, log) as client:
        result = await EvidenceCrawler(client).crawl(INDEX)

    details = [c for c in result.captures if c.page_type == "detail"]
    assert len(details) == 1
    assert details[0].final_url == f"{BASE}/notices/N-1.html"


async def test_offsite_detail_link_skipped_not_fetched():
    log: list[str] = []
    offsite = "http://external.example.org/notice/N-EXT.html"
    routes = {
        INDEX: ok(list_page(["N-1"], extra_links=[offsite])),
        f"{BASE}/notices/N-1.html": ok(detail_page("N-1")),
    }
    async with make_client(routes, log) as client:
        result = await EvidenceCrawler(client).crawl(INDEX)

    assert result.skipped_offsite == [offsite]
    assert all("external.example.org" not in u for u in log)


# ---------------------------------------------------------------- ZIP 与清单

async def test_zip_contains_manifest_and_raw_bytes():
    offsite = "http://external.example.org/notice/N-EXT.html"
    routes = simple_routes()
    routes[INDEX] = ok(list_page(["N-1"], next_href="/page2.html",
                                 extra_links=[offsite]))
    async with make_client(routes) as client:
        result = await EvidenceCrawler(client).crawl(INDEX)

    zip_bytes = build_evidence_zip(result, INDEX)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()
        assert "manifest.json" in names
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))

        assert manifest["index_url"] == INDEX
        assert manifest["page_count"] == 4
        assert manifest["skipped_offsite_links"] == [offsite]
        assert manifest["generated_at"]

        for entry in manifest["pages"]:
            assert set(entry) >= {
                "seq", "page_type", "request_url", "final_url", "sha256", "file",
            }
            # ZIP 内字节与清单 SHA-256 一致
            payload = zf.read(entry["file"])
            assert hashlib.sha256(payload).hexdigest() == entry["sha256"]

        detail_entries = [p for p in manifest["pages"] if p["page_type"] == "detail"]
        assert {p["notice_id"] for p in detail_entries} == {"N-1", "N-2"}
        # 同一最终地址只收录一次
        finals = [p["final_url"] for p in manifest["pages"]]
        assert len(finals) == len(set(finals))


# ---------------------------------------------------------------- 接口级

async def test_endpoint_evidence_zip():
    """对本地示例站做完整取证：3 个列表页 + 13 条详情，跨站链接跳过。"""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        async with app.router.lifespan_context(app):
            r = await ac.post("/evidence", json={"index_url": SITE})

    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/zip"
    disposition = r.headers["content-disposition"]
    assert disposition.startswith("attachment;") and ".zip" in disposition

    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))

        assert manifest["index_url"] == SITE
        assert manifest["page_count"] == 16
        pages = manifest["pages"]
        assert [p["page_type"] for p in pages].count("list") == 3
        assert [p["page_type"] for p in pages].count("detail") == 13
        assert manifest["skipped_offsite_links"] == [
            "http://external.example.org/notice/N-EXT-001.html"
        ]

        finals = [p["final_url"] for p in pages]
        assert len(finals) == len(set(finals))
        for entry in pages:
            payload = zf.read(entry["file"])
            assert hashlib.sha256(payload).hexdigest() == entry["sha256"]

        notice_ids = {p.get("notice_id") for p in pages if p["page_type"] == "detail"}
        assert notice_ids == {f"N-2026-{i:03d}" for i in range(1, 14)}


async def test_endpoint_fetch_failure_returns_reason():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        async with app.router.lifespan_context(app):
            r = await ac.post(
                "/evidence",
                json={"index_url": "http://127.0.0.1:9/never.html"},
            )
    assert r.status_code == 502
    body = r.json()
    assert body["detail"]["reason"] == "fetch_failed"
    assert body["detail"]["message"]


async def test_endpoint_offsite_redirect_blocked():
    routes = {INDEX: redirect("http://evil.example.org/x")}
    app.state.http_client = make_client(routes)  # 不跑 lifespan，直接注入
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post("/evidence", json={"index_url": INDEX})
    assert r.status_code == 502
    assert r.json()["detail"]["reason"] == "offsite_redirect_blocked"
