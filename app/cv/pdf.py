"""Disposable parser process: PDF bytes in, bounded JSON out, no file paths/URLs."""

import asyncio
import io
import json
import sys
from contextlib import suppress

import psutil
from pypdf import PdfReader

from app.domain import DomainError

MAX_BYTES = 5 * 1024 * 1024
MAX_PAGES = 10
MAX_TEXT = 16000
MAX_STREAM = 1024 * 1024
MAX_MEMORY = 512 * 1024 * 1024


def parse_pdf(data):
    if len(data) > MAX_BYTES:
        raise DomainError(413, "cv_file_too_large")
    if not data.startswith(b"%PDF-") or b"%%EOF" not in data[-1024:]:
        raise DomainError(422, "invalid_pdf")
    try:
        reader = PdfReader(io.BytesIO(data), strict=True)
        if reader.is_encrypted:
            raise DomainError(422, "encrypted_pdf")
        if not 1 <= len(reader.pages) <= MAX_PAGES:
            raise DomainError(422, "cv_page_limit")
        pages, total = [], 0
        for number, page in enumerate(reader.pages, 1):
            contents = page.get_contents()
            if contents is not None and len(contents.get_data()) > MAX_STREAM:
                raise DomainError(422, "cv_stream_limit")
            text = (page.extract_text() or "").strip()
            total += len(text)
            if total > MAX_TEXT:
                raise DomainError(422, "cv_text_limit")
            # Any image-only/empty page requires explicit OCR/review, never silent omission.
            if len(text) < 20:
                raise DomainError(422, "cv_ocr_or_text_required")
            pages.append({"page": number, "text": text})
        return pages
    except DomainError:
        raise
    except Exception as exc:
        raise DomainError(422, "invalid_pdf") from exc


async def extract_pdf(data, *, timeout=15):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "app.cv.pdf",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    communication = asyncio.create_task(process.communicate(data))
    try:
        async with asyncio.timeout(timeout):
            while not communication.done():
                await asyncio.wait({communication}, timeout=0.05)
                with suppress(psutil.NoSuchProcess):
                    if psutil.Process(process.pid).memory_info().rss > MAX_MEMORY:
                        raise DomainError(422, "cv_parser_resource_limit")
            output, _ = await communication
        if process.returncode != 0 or len(output) > MAX_TEXT * 8:
            raise DomainError(422, "cv_parser_failed")
        parsed = json.loads(output)
        if "error" in parsed:
            raise DomainError(422, parsed["error"])
        return parsed["pages"]
    except TimeoutError as exc:
        raise DomainError(422, "cv_parser_timeout") from exc
    finally:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
        await process.wait()
        communication.cancel()
        await asyncio.gather(communication, return_exceptions=True)


if __name__ == "__main__":
    # Hard address-space ceiling where available, in addition to parent RSS monitoring.
    if sys.platform != "win32":
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (MAX_MEMORY, MAX_MEMORY))
    try:
        result = {"pages": parse_pdf(sys.stdin.buffer.read(MAX_BYTES + 1))}
    except DomainError as error:
        result = {"error": error.code}
    print(json.dumps(result, ensure_ascii=True))
