import asyncio
import json
import time
from typing import Any, Optional

import aiohttp

from services.logger import logger as logging
from utils.http_client import get_http_session

logging = logging.bind(service="cobalt")


class _CobaltRetryableError(Exception):
    """Transient cobalt/proxy failure that should be retried."""


_RETRYABLE_ERROR_CODES = {
    "error.api.fetch.empty",
}


def _build_auth_header(api_key: Optional[str]) -> Optional[str]:
    if not api_key:
        return None
    normalized = api_key.strip().strip('"').strip("'")
    if not normalized:
        return None
    return f"Api-Key {normalized}"


async def fetch_cobalt_data(
    base_url: Optional[str],
    api_key: Optional[str],
    payload: dict[str, Any],
    *,
    source: str = "cobalt",
    timeout: int = 15,
    attempts: int = 3,
    retry_delay: float = 0.0,
) -> Optional[dict[str, Any]]:
    started_at = time.perf_counter()
    if not base_url:
        logging.error("COBALT_API_URL is not configured for %s", source)
        return None

    auth_header = _build_auth_header(api_key)
    if not auth_header:
        logging.error("COBALT_API_KEY is missing or invalid for %s requests", source)
        return None

    endpoint = f"{base_url.rstrip('/')}/"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": auth_header,
    }
    total_attempts = max(1, attempts)
    last_retryable_error: Optional[_CobaltRetryableError] = None

    for attempt in range(1, total_attempts + 1):
        try:
            attempt_started_at = time.perf_counter()
            session = await get_http_session()
            try:
                async with session.post(
                    endpoint,
                    json=payload,
                    headers=headers,
                    timeout=timeout,
                ) as resp:
                    raw_body = await resp.text()
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                raise _CobaltRetryableError(f"request_error={exc}") from exc

            if not raw_body or not raw_body.strip():
                if resp.status >= 500:
                    raise _CobaltRetryableError(
                        f"empty_response status={resp.status} content_type={resp.headers.get('Content-Type')}"
                    )
                logging.error(
                    "Cobalt returned empty response: status=%s content_type=%s",
                    resp.status,
                    resp.headers.get("Content-Type"),
                )
                return None

            try:
                data = json.loads(raw_body)
            except ValueError:
                snippet = raw_body.strip().replace("\n", " ")[:300]
                if resp.status >= 500:
                    raise _CobaltRetryableError(
                        f"non_json status={resp.status} content_type={resp.headers.get('Content-Type')} body={snippet}"
                    )
                logging.error(
                    "Cobalt returned non-JSON response: status=%s content_type=%s body=%s",
                    resp.status,
                    resp.headers.get("Content-Type"),
                    snippet,
                )
                return None

            if resp.status != 200:
                error_code = None
                error_context = None
                if isinstance(data, dict):
                    error_obj = data.get("error") or {}
                    if isinstance(error_obj, dict):
                        error_code = error_obj.get("code")
                        error_context = error_obj.get("context")

                if resp.status >= 500 or error_code in _RETRYABLE_ERROR_CODES:
                    raise _CobaltRetryableError(
                        f"status={resp.status} code={error_code} context={error_context}"
                    )

                logging.error(
                    "Cobalt request failed: status=%s code=%s context=%s",
                    resp.status,
                    error_code,
                    error_context,
                )
                return None

            if not isinstance(data, dict):
                logging.error("Invalid Cobalt response type: %s", type(data))
                return None

            logging.perf(
                "cobalt_request",
                duration_ms=(time.perf_counter() - attempt_started_at) * 1000.0,
                source=source,
                attempt=attempt,
                status=resp.status,
            )
            logging.perf(
                "cobalt_request_total",
                duration_ms=(time.perf_counter() - started_at) * 1000.0,
                source=source,
                attempts=attempt,
            )
            return data
        except _CobaltRetryableError as exc:
            last_retryable_error = exc
            if attempt < total_attempts:
                logging.warning(
                    "Retrying cobalt request: source=%s attempt=%s/%s reason=%s",
                    source,
                    attempt + 1,
                    total_attempts,
                    exc,
                )
                if retry_delay > 0:
                    await asyncio.sleep(retry_delay)
                continue
            break
        except Exception as exc:
            logging.exception("Unexpected cobalt client exception: source=%s error=%s", source, exc)
            return None

    if last_retryable_error:
        logging.error(
            "Cobalt request failed after retries: source=%s error=%s",
            source,
            last_retryable_error,
        )
    return None
