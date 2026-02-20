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


@dataclass(slots=True)
class TownSummary:
    id: int
    name: str
    region_name: Optional[str]
    country_name: Optional[str]


@dataclass(slots=True)
class VenueTypeSummary:
    id: int
    name: str


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

    async def search_towns(
        self,
        query: str,
        *,
        page_size: int = 10,
    ) -> list[TownSummary]:
        data = await self._request_json(
            "GET",
            "/towns",
            params={
                "name": query,
                "itemsPerPage": page_size,
                "page": 1,
            },
        )

        if not isinstance(data, list):
            raise RatingApiError("Некорректный ответ /towns")

        towns: list[TownSummary] = []
        for row in data:
            if not isinstance(row, dict):
                continue

            town_id = row.get("id")
            town_name = row.get("name")
            if not isinstance(town_id, int) or not isinstance(town_name, str):
                continue

            region_name: Optional[str] = None
            country_name: Optional[str] = None

            region = row.get("region")
            if isinstance(region, dict):
                value = region.get("name")
                if isinstance(value, str) and value.strip():
                    region_name = value.strip()

            country = row.get("country")
            if isinstance(country, dict):
                value = country.get("name")
                if isinstance(value, str) and value.strip():
                    country_name = value.strip()

            towns.append(
                TownSummary(
                    id=town_id,
                    name=town_name.strip() or town_name,
                    region_name=region_name,
                    country_name=country_name,
                )
            )

        return towns

    async def get_venue_types(self) -> list[VenueTypeSummary]:
        data = await self._request_json(
            "GET",
            "/venue_types",
            params={"itemsPerPage": 30, "page": 1},
        )

        if not isinstance(data, list):
            raise RatingApiError("Некорректный ответ /venue_types")

        venue_types: list[VenueTypeSummary] = []
        for row in data:
            if not isinstance(row, dict):
                continue

            venue_type_id = row.get("id")
            venue_type_name = row.get("name")
            if not isinstance(venue_type_id, int) or not isinstance(venue_type_name, str):
                continue

            venue_types.append(VenueTypeSummary(id=venue_type_id, name=venue_type_name.strip()))

        if not venue_types:
            raise RatingApiError("API не вернуло типы площадок")

        return venue_types

    async def create_venue(
        self,
        *,
        name: str,
        town_id: int,
        town_name: str,
        venue_type_id: int,
        venue_type_name: str,
        address: Optional[str],
        urls: list[str],
        bearer_token: str,
    ) -> dict[str, Any]:
        optional_fields: dict[str, Any] = {}
        if address:
            optional_fields["address"] = address
        if urls:
            optional_fields["urls"] = urls

        payload_variants: list[dict[str, Any]] = [
            {
                "name": name,
                "town": {"id": town_id, "name": town_name},
                "type": {"id": venue_type_id, "name": venue_type_name},
                **optional_fields,
            },
            {
                "name": name,
                "town": {"name": town_name},
                "type": {"name": venue_type_name},
                **optional_fields,
            },
            {
                "name": name,
                "town": f"/towns/{town_id}",
                "type": f"/venue_types/{venue_type_id}",
                **optional_fields,
            },
            {
                "name": name,
                "town": town_id,
                "type": venue_type_id,
                **optional_fields,
            },
        ]

        last_error: Optional[RatingApiError] = None
        for payload in payload_variants:
            try:
                data = await self._request_json(
                    "POST",
                    "/venues",
                    payload=payload,
                    bearer_token=bearer_token,
                )
            except RatingApiError as exc:
                last_error = exc
                continue

            if not isinstance(data, dict):
                last_error = RatingApiError("Некорректный ответ /venues")
                continue

            return data

        if last_error is not None:
            raise last_error

        raise RatingApiError("Не удалось создать площадку")
