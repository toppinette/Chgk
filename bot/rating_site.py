from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

import aiohttp
from bs4 import BeautifulSoup, Tag


class RatingSiteError(Exception):
    pass


@dataclass(slots=True)
class RequestSubmitResult:
    final_url: str
    message: str


@dataclass(slots=True)
class RequestFormData:
    action_url: str
    fields: dict[str, str]


class RatingSiteClient:
    def __init__(self, base_url: str, timeout_seconds: float = 20.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    async def submit_tournament_request(
        self,
        *,
        email: str,
        password: str,
        tournament_id: int,
        venue_id: int,
        date_start: str,
        approximate_teams_count: Optional[int],
        narrator_id: Optional[int],
        comment: Optional[str],
    ) -> RequestSubmitResult:
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        cookie_jar = aiohttp.CookieJar()

        async with aiohttp.ClientSession(
            timeout=timeout,
            cookie_jar=cookie_jar,
            headers=self._build_default_headers(),
        ) as session:
            await self._login(session, email=email, password=password)

            form_url = f"{self.base_url}/tournament/request?tournamentId={tournament_id}"
            form_html, resolved_form_url = await self._get_text(session, form_url)

            form_data = self._extract_request_form(form_html, resolved_form_url)
            self._fill_request_form(
                form_data.fields,
                tournament_id=tournament_id,
                venue_id=venue_id,
                date_start=date_start,
                approximate_teams_count=approximate_teams_count,
                narrator_id=narrator_id,
                comment=comment,
            )

            submit_html, submit_url = await self._post_form(
                session,
                form_data.action_url,
                form_data.fields,
                referer=resolved_form_url,
            )

            self._raise_if_login_page(submit_url, submit_html)
            self._raise_if_site_error(submit_html)

            return RequestSubmitResult(
                final_url=submit_url,
                message="Заявка отправлена. Проверьте статус на странице турнира.",
            )

    async def _login(self, session: aiohttp.ClientSession, *, email: str, password: str) -> None:
        login_url = f"{self.base_url}/login"
        login_html, resolved_login_url = await self._get_text(session, login_url)
        login_form = self._extract_form(login_html, resolved_login_url, action_suffix="/login")

        username_field = self._find_field(login_form.fields, ("username", "_username", "email"))
        password_field = self._find_field(login_form.fields, ("password", "_password", "pass"))

        if username_field is None or password_field is None:
            raise RatingSiteError("Не удалось определить поля логина на сайте rating.")

        login_form.fields[username_field] = email
        login_form.fields[password_field] = password

        remember_field = self._find_field(login_form.fields, ("remember",))
        if remember_field is not None:
            login_form.fields[remember_field] = "on"

        post_html, post_url = await self._post_form(
            session,
            login_form.action_url,
            login_form.fields,
            referer=resolved_login_url,
        )

        if self._is_login_url(post_url):
            if self._contains_login_form(post_html):
                raise RatingSiteError("Логин на сайте не прошёл. Проверьте email/пароль.")

    async def _get_text(
        self, session: aiohttp.ClientSession, url: str
    ) -> tuple[str, str]:
        async with session.get(
            url,
            allow_redirects=True,
            headers={"Accept": "text/html,application/xhtml+xml"},
        ) as response:
            html = await response.text()
            if response.status >= 400:
                raise RatingSiteError(self._http_error_message(response.status, str(response.url)))
            return html, str(response.url)

    async def _post_form(
        self,
        session: aiohttp.ClientSession,
        action_url: str,
        fields: dict[str, str],
        *,
        referer: str,
    ) -> tuple[str, str]:
        origin = self._origin_from_url(referer)
        async with session.post(
            action_url,
            data=fields,
            allow_redirects=True,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "Referer": referer,
                "Origin": origin,
            },
        ) as response:
            html = await response.text()
            if response.status >= 400:
                raise RatingSiteError(self._http_error_message(response.status, str(response.url)))
            return html, str(response.url)

    def _build_default_headers(self) -> dict[str, str]:
        return {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "ru,en;q=0.9",
        }

    def _origin_from_url(self, url: str) -> str:
        parsed = urlparse(url)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
        return self.base_url

    def _http_error_message(self, status: int, url: str) -> str:
        path = urlparse(url).path or "/"
        if status == 403:
            return (
                f"Сайт rating вернул HTTP 403 на {path}. "
                "Обычно это блокировка формы/доступа или anti-bot проверка."
            )
        return f"Сайт rating вернул HTTP {status} на {path}"

    def _extract_request_form(self, html: str, page_url: str) -> RequestFormData:
        form = self._extract_form(html, page_url, action_suffix="/tournament/request")
        lower_names = [name.lower() for name in form.fields]
        if not any("venue" in item for item in lower_names):
            raise RatingSiteError("Форма заявки не найдена на странице турнира.")
        return form

    def _extract_form(self, html: str, page_url: str, *, action_suffix: str) -> RequestFormData:
        soup = BeautifulSoup(html, "html.parser")
        forms = soup.find_all("form")
        if not forms:
            raise RatingSiteError("На странице не найдена форма.")

        target_form: Optional[Tag] = None
        for form in forms:
            action = str(form.get("action") or "")
            if action.endswith(action_suffix) or action_suffix in action:
                target_form = form
                break

        if target_form is None:
            target_form = forms[0]

        action = str(target_form.get("action") or "").strip()
        action_url = urljoin(page_url, action or page_url)
        fields = self._collect_form_fields(target_form)
        return RequestFormData(action_url=action_url, fields=fields)

    def _collect_form_fields(self, form: Tag) -> dict[str, str]:
        fields: dict[str, str] = {}
        submit_candidates: list[tuple[str, str]] = []

        for input_tag in form.find_all("input"):
            if not isinstance(input_tag, Tag):
                continue
            name = str(input_tag.get("name") or "").strip()
            if not name:
                continue
            if input_tag.has_attr("disabled"):
                continue

            input_type = str(input_tag.get("type") or "text").lower()
            value = str(input_tag.get("value") or "")

            if input_type in {"submit", "button", "image"}:
                submit_candidates.append((name, value))
                continue
            if input_type == "checkbox":
                if input_tag.has_attr("checked"):
                    fields[name] = value or "on"
                continue
            if input_type == "radio":
                if input_tag.has_attr("checked"):
                    fields[name] = value
                continue
            if input_type in {"file", "reset"}:
                continue

            fields[name] = value

        for textarea_tag in form.find_all("textarea"):
            if not isinstance(textarea_tag, Tag):
                continue
            name = str(textarea_tag.get("name") or "").strip()
            if not name or textarea_tag.has_attr("disabled"):
                continue
            fields[name] = textarea_tag.text or ""

        for select_tag in form.find_all("select"):
            if not isinstance(select_tag, Tag):
                continue
            name = str(select_tag.get("name") or "").strip()
            if not name or select_tag.has_attr("disabled"):
                continue

            selected_option = select_tag.find("option", selected=True)
            if selected_option is None:
                selected_option = select_tag.find("option")
            if selected_option is None:
                fields.setdefault(name, "")
                continue

            fields[name] = str(selected_option.get("value") or "")

        for name, value in submit_candidates:
            fields.setdefault(name, value)

        return fields

    def _fill_request_form(
        self,
        fields: dict[str, str],
        *,
        tournament_id: int,
        venue_id: int,
        date_start: str,
        approximate_teams_count: Optional[int],
        narrator_id: Optional[int],
        comment: Optional[str],
    ) -> None:
        self._set_field_value(fields, ("tournament", "id"), str(tournament_id))
        self._set_field_value(fields, ("venue",), str(venue_id), required=True)
        self._set_field_value(fields, ("date", "start"), date_start, required=True)

        if approximate_teams_count is not None:
            self._set_field_value(
                fields,
                ("approximate", "teams"),
                str(approximate_teams_count),
            )
            self._set_field_value(fields, ("teams", "count"), str(approximate_teams_count))

        if narrator_id is not None:
            self._set_field_value(fields, ("narrator",), str(narrator_id))
            self._set_field_value(fields, ("host",), str(narrator_id))

        if comment:
            self._set_field_value(fields, ("comment",), comment)

    def _set_field_value(
        self,
        fields: dict[str, str],
        tokens: tuple[str, ...],
        value: str,
        *,
        required: bool = False,
    ) -> None:
        tokens_lower = tuple(token.lower() for token in tokens)
        for name in fields:
            normalized = name.lower().replace("_", "")
            if all(token in normalized for token in tokens_lower):
                fields[name] = value
                return

        if required:
            requested = ", ".join(tokens)
            raise RatingSiteError(f"В форме не найдено обязательное поле: {requested}.")

    def _find_field(
        self,
        fields: dict[str, str],
        candidate_tokens: tuple[str, ...],
    ) -> Optional[str]:
        for token in candidate_tokens:
            token_lower = token.lower()
            for name in fields:
                if token_lower in name.lower():
                    return name
        return None

    def _raise_if_login_page(self, url: str, html: str) -> None:
        if self._is_login_url(url) and self._contains_login_form(html):
            raise RatingSiteError("Сессия истекла: сайт вернул страницу логина.")

    def _is_login_url(self, url: str) -> bool:
        path = urlparse(url).path.rstrip("/")
        return path == "/login"

    def _contains_login_form(self, html: str) -> bool:
        soup = BeautifulSoup(html, "html.parser")
        return soup.find("form", attrs={"action": "/login"}) is not None

    def _raise_if_site_error(self, html: str) -> None:
        soup = BeautifulSoup(html, "html.parser")
        error_nodes = soup.select(
            ".alert-danger, .form-error-message, .error, #error_message, .invalid-feedback"
        )
        messages: list[str] = []
        for node in error_nodes:
            text = node.get_text(" ", strip=True)
            if text:
                messages.append(text)

        if messages:
            raise RatingSiteError(messages[0])
