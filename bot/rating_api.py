from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Optional

import aiohttp


class RatingApiError(Exception):
    pass


@dataclass(slots=True)
class TournamentSummary:
    id: int
    name: str
    date_start: str
    date_end: str
    difficulty_forecast: Optional[float]
    editors: list[dict[str, Any]]
    type_short_name: Optional[str]


class RatingApiClient:
    def __init__(self, base_url: str, timeout_seconds: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        payload: Optional[dict[str, Any]] = None,
        bearer_token: Optional[str] = None,
    ) -> Any:
        headers: dict[str, str] = {"Accept": "application/json"}
        if bearer_token:
            headers["Authorization"] = f"Bearer {bearer_token}"

        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        url = f"{self.base_url}{path}"

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.request(
                    method,
                    url,
                    params=params,
                    json=payload,
                    headers=headers,
                ) as response:
                    data: Any
                    content_type = response.headers.get("Content-Type", "")

                    if "application/json" in content_type or "application/ld+json" in content_type:
                        data = await response.json(content_type=None)
                    else:
                        data = await response.text()

                    if response.status >= 400:
                        detail = None
                        if isinstance(data, dict):
                            detail = data.get("detail") or data.get("message")
                        if not detail and isinstance(data, str):
                            detail = data.strip() or None
                        message = detail or f"HTTP {response.status}"
                        raise RatingApiError(message)

                    return data
        except aiohttp.ClientError as exc:
            raise RatingApiError(f"Ошибка соединения с rating API: {exc}") from exc
        except TimeoutError as exc:
            raise RatingApiError("Таймаут при обращении к rating API") from exc

    async def login(self, email: str, password: str) -> str:
        data = await self._request_json(
            "POST",
            "/authentication_token",
            payload={"email": email, "password": password},
        )

        if not isinstance(data, dict) or not data.get("token"):
            raise RatingApiError("API не вернул токен")

        return str(data["token"])

    async def get_current_user(self, token: str) -> dict[str, Any]:
        data = await self._request_json("GET", "/users/test", bearer_token=token)

        if not isinstance(data, dict):
            raise RatingApiError("Некорректный ответ /users/test")

        return data

    async def get_sync_tournaments_by_date(
        self,
        target_date: date,
        *,
        bearer_token: Optional[str] = None,
        page_size: int = 100,
    ) -> list[TournamentSummary]:
        date_iso = target_date.isoformat()
        page = 1
        items: list[TournamentSummary] = []

        while True:
            params = {
                "page": page,
                "itemsPerPage": page_size,
                "type": 3,
                "dateStart[before]": date_iso,
                "dateEnd[after]": date_iso,
                "order[id]": "desc",
            }

            data = await self._request_json(
                "GET",
                "/tournaments",
                params=params,
                bearer_token=bearer_token,
            )

            if not isinstance(data, list):
                raise RatingApiError("Некорректный ответ /tournaments")

            if not data:
                break

            for row in data:
                if not isinstance(row, dict):
                    continue

                tournament_type = row.get("type") or {}
                if not isinstance(tournament_type, dict):
                    tournament_type = {}

                items.append(
                    TournamentSummary(
                        id=int(row.get("id", 0)),
                        name=str(row.get("name", "Без названия")),
                        date_start=str(row.get("dateStart", "")),
                        date_end=str(row.get("dateEnd", "")),
                        difficulty_forecast=row.get("difficultyForecast"),
                        editors=row.get("editors") or [],
                        type_short_name=tournament_type.get("shortName"),
                    )
                )

            if len(data) < page_size:
                break

            page += 1

        return items
