"""页面取证：把当次采集实际收到的 HTML 字节打包为可下载的 ZIP 证据包。

与 /query 的“尽力抓取、错误收集”不同，取证要求**完整或失败**：

* 手动跟随重定向：仅跟进同站跳转；站外跳转在访问目标之前阻断并整体失败；
* 分页循环、达到页数上限、任一页面抓取或解析失败都会中止采集，
  返回具体原因，不生成缺页的证据包；
* 清单（ZIP 内 manifest.json）逐页记录页面类型、请求地址、最终地址与
  SHA-256；同一最终地址只收录一次。
"""
from __future__ import annotations

import hashlib
import io
import json
import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .parser import is_same_site, parse_detail, parse_detail_links, parse_next_page

#: 重定向状态码（其余 3xx 一律视为抓取失败）
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}

#: 取证失败原因码（随 HTTP 502 的 detail.reason 返回）
REASON_FETCH_FAILED = "fetch_failed"
REASON_OFFSITE_REDIRECT = "offsite_redirect_blocked"
REASON_PAGINATION_LOOP = "pagination_loop"
REASON_LIMIT_EXCEEDED = "limit_exceeded"
REASON_PARSE_FAILED = "parse_failed"


class EvidenceError(Exception):
    """取证中止错误：携带机器可读原因码与面向值班员的说明。"""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass
class PageCapture:
    """单个页面的取证记录（内容为实际收到的字节）。"""

    seq: int
    page_type: str  # "list" | "detail"
    request_url: str
    final_url: str
    content: bytes
    sha256: str
    notice_id: str | None = None


@dataclass
class EvidenceResult:
    captures: list[PageCapture] = field(default_factory=list)
    skipped_offsite: list[str] = field(default_factory=list)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


class EvidenceCrawler:
    """同站分页 + 详情取证抓取。任何一步失败都抛 :class:`EvidenceError`。"""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        max_list_pages: int = 50,
        max_detail_pages: int = 500,
        max_redirect_hops: int = 10,
    ) -> None:
        self._client = client
        self._max_list_pages = max_list_pages
        self._max_detail_pages = max_detail_pages
        self._max_redirect_hops = max_redirect_hops

    async def _fetch(self, url: str, site_base: str) -> tuple[str, bytes, str]:
        """抓取单个页面，手动跟随重定向。

        返回 (最终地址, 原始字节, 解码文本)。站外跳转在访问目标前阻断。
        """
        current = url
        for _ in range(self._max_redirect_hops):
            try:
                resp = await self._client.get(current, follow_redirects=False)
            except httpx.HTTPError as exc:
                raise EvidenceError(
                    REASON_FETCH_FAILED,
                    f"页面抓取失败 {current}: {type(exc).__name__}: {exc}",
                ) from exc

            if resp.status_code in _REDIRECT_STATUSES:
                location = resp.headers.get("location")
                if not location:
                    raise EvidenceError(
                        REASON_FETCH_FAILED,
                        f"页面抓取失败 {current}: HTTP {resp.status_code} 重定向缺少 Location 头",
                    )
                target = urljoin(current, location)
                if not is_same_site(target, site_base):
                    # 关键约束：在访问目标之前阻断站外跳转
                    raise EvidenceError(
                        REASON_OFFSITE_REDIRECT,
                        f"站外跳转已在访问目标前阻断: {current} -> {target}",
                    )
                current = target
                continue

            if not 200 <= resp.status_code < 300:
                raise EvidenceError(
                    REASON_FETCH_FAILED,
                    f"页面抓取失败 {current}: HTTP {resp.status_code}",
                )
            return current, resp.content, resp.text

        raise EvidenceError(
            REASON_FETCH_FAILED,
            f"页面抓取失败 {url}: 重定向次数超过上限 {self._max_redirect_hops}",
        )

    async def crawl(self, index_url: str) -> EvidenceResult:
        result = EvidenceResult()
        captured_final: set[str] = set()

        def add_capture(
            page_type: str, request_url: str, final_url: str, content: bytes,
            notice_id: str | None = None,
        ) -> None:
            captured_final.add(final_url)
            result.captures.append(
                PageCapture(
                    seq=len(result.captures) + 1,
                    page_type=page_type,
                    request_url=request_url,
                    final_url=final_url,
                    content=content,
                    sha256=_sha256(content),
                    notice_id=notice_id,
                )
            )

        # 1) 列表分页：沿同站 rel=next 逐页抓取，循环/超限即中止
        seen_list_requests: set[str] = set()
        detail_queue: list[str] = []
        seen_detail_requests: set[str] = set()
        list_count = 0
        page_url: str | None = index_url
        while page_url is not None:
            if page_url in seen_list_requests:
                raise EvidenceError(
                    REASON_PAGINATION_LOOP,
                    f"检测到分页循环: {page_url} 被重复请求，已中止",
                )
            if list_count >= self._max_list_pages:
                raise EvidenceError(
                    REASON_LIMIT_EXCEEDED,
                    f"列表分页达到上限 {self._max_list_pages} 页仍有后续分页，已中止",
                )
            seen_list_requests.add(page_url)

            final_url, content, text = await self._fetch(page_url, index_url)
            if final_url in captured_final:
                raise EvidenceError(
                    REASON_PAGINATION_LOOP,
                    f"检测到分页循环: {page_url} 的最终地址 {final_url} 已收录，已中止",
                )

            detail_links = parse_detail_links(text, final_url)
            next_url = parse_next_page(text, final_url)
            if not detail_links and next_url is None:
                # 既无公告条目也无下一页：确认是否存在分页导航，否则结构无法识别
                soup = BeautifulSoup(text, "html.parser")
                if soup.select_one("nav.pager") is None:
                    raise EvidenceError(
                        REASON_PARSE_FAILED,
                        f"列表页解析失败 {final_url}: 未找到公告条目或分页导航",
                    )

            add_capture("list", page_url, final_url, content)
            list_count += 1

            for link_url, _id, _kind in detail_links:
                if not is_same_site(link_url, index_url):
                    result.skipped_offsite.append(link_url)
                    continue
                if link_url not in seen_detail_requests:
                    seen_detail_requests.add(link_url)
                    detail_queue.append(link_url)

            if next_url is not None and not is_same_site(next_url, index_url):
                result.skipped_offsite.append(next_url)
                next_url = None
            page_url = next_url

        # 2) 详情页：同一最终地址只收录一次，解析失败即中止
        detail_count = 0
        for detail_url in detail_queue:
            if detail_count >= self._max_detail_pages:
                raise EvidenceError(
                    REASON_LIMIT_EXCEEDED,
                    f"详情页达到上限 {self._max_detail_pages} 条仍有未抓取页面，已中止",
                )
            final_url, content, text = await self._fetch(detail_url, index_url)
            if final_url in captured_final:
                continue
            try:
                ann = parse_detail(text, final_url)
            except ValueError as exc:
                raise EvidenceError(
                    REASON_PARSE_FAILED,
                    f"详情页解析失败 {final_url}: {exc}",
                ) from exc
            add_capture("detail", detail_url, final_url, content, notice_id=ann.notice_id)
            detail_count += 1

        return result


def _arcname(cap: PageCapture) -> str:
    """ZIP 内文件名：pages/序号-类型-最终地址文件名（已净化，序号保证唯一）。"""
    path = urlparse(cap.final_url).path
    name = posixpath.basename(path.rstrip("/")) or "index.html"
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    return f"pages/{cap.seq:04d}-{cap.page_type}-{name}"


def build_manifest(result: EvidenceResult, index_url: str) -> dict:
    """取证清单：逐页记录页面类型、请求地址、最终地址与 SHA-256。"""
    pages: list[dict] = []
    for cap in result.captures:
        entry: dict = {
            "seq": cap.seq,
            "page_type": cap.page_type,
            "request_url": cap.request_url,
            "final_url": cap.final_url,
            "sha256": cap.sha256,
            "file": _arcname(cap),
        }
        if cap.notice_id:
            entry["notice_id"] = cap.notice_id
        pages.append(entry)
    return {
        "index_url": index_url,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "page_count": len(pages),
        "pages": pages,
        "skipped_offsite_links": result.skipped_offsite,
    }


def build_evidence_zip(result: EvidenceResult, index_url: str) -> bytes:
    """把抓取结果打成 ZIP：manifest.json + pages/ 下逐页原始字节。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        manifest = build_manifest(result, index_url)
        zf.writestr(
            "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        )
        for cap in result.captures:
            zf.writestr(_arcname(cap), cap.content)
    return buf.getvalue()
