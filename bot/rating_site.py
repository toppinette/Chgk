from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Optional
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

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
    method: str
    fields: dict[str, str]


@dataclass(slots=True)
class RequestPrefill:
    venue_id: Optional[int] = None
    venue_label: Optional[str] = None
    representative_id: Optional[int] = None
    representative_label: Optional[str] = None
    approximate_teams_count: Optional[int] = None


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
        representative_id: Optional[int],
        date_start: str,
        approximate_teams_count: Optional[int],
        narrator_id: Optional[int],
        comment: Optional[str],
    ) -> RequestSubmitResult:
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        cookie_jar = aiohttp.CookieJar()

        try:
            async with aiohttp.ClientSession(
                timeout=timeout,
                cookie_jar=cookie_jar,
                headers=self._build_default_headers(),
            ) as session:
                await self._login(session, email=email, password=password)

                form_url = f"{self.base_url}/tournament/request?tournamentId={tournament_id}"
                form_html, resolved_form_url = await self._get_text(
                    session,
                    form_url,
                    stage="request_get",
                )
                request_candidates = self._extract_request_candidates(form_html)

                form_data = self._extract_request_form(form_html, resolved_form_url)
                self._fill_request_form(
                    form_data.fields,
                    tournament_id=tournament_id,
                    venue_id=venue_id,
                    representative_id=representative_id,
                    date_start=date_start,
                    approximate_teams_count=approximate_teams_count,
                    narrator_id=narrator_id,
                    comment=comment,
                )

                submit_html, submit_url = await self._submit_request_with_fallback(
                    session=session,
                    action_url=form_data.action_url,
                    form_method=form_data.method,
                    form_fields=form_data.fields,
                    referer=resolved_form_url,
                    tournament_id=tournament_id,
                    request_candidates=request_candidates,
                )

                self._raise_if_login_page(submit_url, submit_html)
                self._raise_if_site_error(submit_html)
                request_id = self._extract_request_id(submit_url, submit_html)
                if request_id is None and not self._has_success_marker(submit_html):
                    raise RatingSiteError(
                        "Сайт не подтвердил отправку заявки (нет requestId/успешного сообщения). "
                        "Проверьте обязательные поля в форме."
                    )

                requests_url = self._build_tournament_requests_url(
                    submit_url=submit_url,
                    tournament_id=tournament_id,
                )
                if request_id is not None:
                    message = (
                        f"Заявка отправлена (requestId={request_id}). "
                        "Проверьте статус в списке заявок турнира."
                    )
                else:
                    message = "Заявка отправлена. Проверьте статус в списке заявок турнира."

                return RequestSubmitResult(
                    final_url=requests_url,
                    message=message,
                )
        except aiohttp.ClientError as exc:
            raise RatingSiteError(f"Ошибка соединения с сайтом rating: {exc}") from exc
        except TimeoutError as exc:
            raise RatingSiteError("Таймаут при обращении к сайту rating") from exc

    async def get_request_prefill(
        self,
        *,
        email: str,
        password: str,
        tournament_id: int,
    ) -> RequestPrefill:
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        cookie_jar = aiohttp.CookieJar()

        try:
            async with aiohttp.ClientSession(
                timeout=timeout,
                cookie_jar=cookie_jar,
                headers=self._build_default_headers(),
            ) as session:
                await self._login(session, email=email, password=password)
                form_url = f"{self.base_url}/tournament/request?tournamentId={tournament_id}"
                form_html, _ = await self._get_text(
                    session,
                    form_url,
                    stage="request_prefill_get",
                )
                return self._extract_request_prefill(form_html)
        except aiohttp.ClientError as exc:
            raise RatingSiteError(f"Ошибка соединения с сайтом rating: {exc}") from exc
        except TimeoutError as exc:
            raise RatingSiteError("Таймаут при обращении к сайту rating") from exc

    async def _submit_request_with_fallback(
        self,
        *,
        session: aiohttp.ClientSession,
        action_url: str,
        form_method: str,
        form_fields: dict[str, str],
        referer: str,
        tournament_id: int,
        request_candidates: list[str],
    ) -> tuple[str, str]:
        normalized_method = (form_method or "GET").strip().upper() or "GET"
        method_variants = [normalized_method]
        if normalized_method == "GET":
            method_variants.append("POST")
        elif normalized_method == "POST":
            method_variants.append("GET")

        fallback_url = self._build_request_fallback_url(
            tournament_id=tournament_id,
            referer=referer,
        )
        url_variants = [action_url]
        if fallback_url != action_url:
            url_variants.append(fallback_url)

        attempts: list[tuple[str, str]] = []
        for url in url_variants:
            for method in method_variants:
                attempts.append((url, method))

        last_405_error: RatingSiteError | None = None
        for index, (url, method) in enumerate(attempts, start=1):
            stage = f"request_submit_{index}_{method.lower()}"
            try:
                return await self._submit_form(
                    session=session,
                    method=method,
                    action_url=url,
                    fields=form_fields,
                    referer=referer,
                    stage=stage,
                )
            except RatingSiteError as exc:
                if "HTTP 405" not in str(exc):
                    raise
                last_405_error = exc
                continue

        if last_405_error is not None:
            candidates = ", ".join(request_candidates[:8]) if request_candidates else "не найдены"
            attempted = ", ".join(f"{method} {urlparse(url).path or '/'}" for url, method in attempts)
            raise RatingSiteError(
                f"{last_405_error} Варианты отправки: {attempted}. "
                f"Возможные request-endpoints в HTML: {candidates}"
            ) from last_405_error

        raise RatingSiteError("Не удалось отправить заявку: все варианты отправки завершились ошибкой.")

    def _build_request_fallback_url(self, *, tournament_id: int, referer: str) -> str:
        parsed = urlparse(referer)
        base = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else self.base_url
        query = urlencode({"tournamentId": tournament_id})
        return f"{base}/tournament/request?{query}"

    def _build_tournament_requests_url(self, *, submit_url: str, tournament_id: int) -> str:
        parsed = urlparse(submit_url)
        base = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else self.base_url
        return f"{base}/tournament/{tournament_id}/requests"

    def _extract_request_id(self, submit_url: str, html: str) -> Optional[int]:
        parsed = urlparse(submit_url)
        query = parse_qs(parsed.query)
        request_values = query.get("requestId", [])
        for value in request_values:
            if value.isdigit():
                return int(value)

        if not html:
            return None

        soup = BeautifulSoup(html, "html.parser")
        # Sometimes requestId is present only in links/actions after redirect.
        for node in soup.select("a[href], form[action]"):
            target = str(node.get("href") or node.get("action") or "")
            if "requestId=" not in target:
                continue
            parsed_target = urlparse(target)
            target_query = parse_qs(parsed_target.query)
            target_values = target_query.get("requestId", [])
            for value in target_values:
                if value.isdigit():
                    return int(value)

        return None

    def _has_success_marker(self, html: str) -> bool:
        if not html:
            return False

        soup = BeautifulSoup(html, "html.parser")
        for selector in (".alert-success", "#success_message", ".success", ".toast-body"):
            node = soup.select_one(selector)
            if node is None:
                continue
            text = node.get_text(" ", strip=True).casefold()
            if text and any(token in text for token in ("усп", "добав", "отправ", "создан", "принят")):
                return True

        text_blob = soup.get_text(" ", strip=True).casefold()
        return any(
            token in text_blob
            for token in (
                "заявка отправлена",
                "заявка успешно",
                "заявка добавлена",
                "заявка принята",
            )
        )

    def _extract_request_candidates(self, html: str) -> list[str]:
        if not html:
            return []

        candidates: set[str] = set()
        # Find URL-like tokens that mention "request"; useful for JS-driven submit endpoints.
        for match in re.finditer(r'["\']([^"\']*request[^"\']*)["\']', html, flags=re.IGNORECASE):
            token = match.group(1).strip()
            if not token:
                continue
            token = token.replace("\\/", "/")
            if token.startswith("http://") or token.startswith("https://") or token.startswith("/"):
                candidates.add(token)

        return sorted(candidates)

    async def _login(self, session: aiohttp.ClientSession, *, email: str, password: str) -> None:
        login_url = f"{self.base_url}/login"
        login_html, resolved_login_url = await self._get_text(
            session,
            login_url,
            stage="login_get",
        )
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
            stage="login_post",
        )

        if self._is_login_url(post_url):
            if self._contains_login_form(post_html):
                raise RatingSiteError("Логин на сайте не прошёл. Проверьте email/пароль.")

        # Explicitly verify that session is authenticated; otherwise request page
        # can fail with a generic 403 "Недостаточно прав", which is misleading.
        home_html, _ = await self._get_text(
            session,
            f"{self.base_url}/",
            stage="login_check_home",
        )
        if self._looks_like_guest_home(home_html):
            raise RatingSiteError(
                "Не удалось подтвердить вход на сайт (гостевой режим после логина). "
                "Проверьте email/пароль."
            )

    async def _get_text(
        self,
        session: aiohttp.ClientSession,
        url: str,
        *,
        stage: str = "get",
    ) -> tuple[str, str]:
        async with session.get(
            url,
            allow_redirects=True,
            headers={"Accept": "text/html,application/xhtml+xml"},
        ) as response:
            html = await response.text()
            if response.status >= 400:
                base = self._http_error_message(response.status, str(response.url))
                detail = self._extract_error_detail_from_html(html)
                headers_hint = self._extract_error_headers(response.headers)
                extra = f"{detail} {headers_hint}".strip()
                if extra:
                    raise RatingSiteError(f"[{stage}] {base} {extra}")
                raise RatingSiteError(f"[{stage}] {base}")
            return html, str(response.url)

    async def _post_form(
        self,
        session: aiohttp.ClientSession,
        action_url: str,
        fields: dict[str, str],
        *,
        referer: str,
        stage: str = "post",
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
                base = self._http_error_message(response.status, str(response.url))
                detail = self._extract_error_detail_from_html(html)
                headers_hint = self._extract_error_headers(response.headers)
                prefix = f"[{stage}] "
                if detail:
                    raise RatingSiteError(f"{prefix}{base} {detail} {headers_hint}".strip())
                raise RatingSiteError(f"{prefix}{base} {headers_hint}".strip())
            return html, str(response.url)

    async def _submit_form(
        self,
        *,
        session: aiohttp.ClientSession,
        method: str,
        action_url: str,
        fields: dict[str, str],
        referer: str,
        stage: str,
    ) -> tuple[str, str]:
        normalized_method = (method or "GET").strip().upper() or "GET"
        headers = {
            "Accept": "text/html,application/xhtml+xml",
            "Referer": referer,
        }
        if normalized_method != "GET":
            headers["Origin"] = self._origin_from_url(referer)

        request_kwargs: dict[str, Any] = {
            "allow_redirects": True,
            "headers": headers,
        }
        if normalized_method == "GET":
            request_kwargs["params"] = fields
        else:
            request_kwargs["data"] = fields

        async with session.request(normalized_method, action_url, **request_kwargs) as response:
            html = await response.text()
            if response.status >= 400:
                base = self._http_error_message(response.status, str(response.url))
                detail = self._extract_error_detail_from_html(html)
                headers_hint = self._extract_error_headers(response.headers)
                prefix = f"[{stage}] "
                if detail:
                    raise RatingSiteError(f"{prefix}{base} {detail} {headers_hint}".strip())
                raise RatingSiteError(f"{prefix}{base} {headers_hint}".strip())
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

    def _extract_error_detail_from_html(self, html: str) -> str:
        if not html:
            return ""

        lowered = html.casefold()
        if "cloudflare" in lowered or "just a moment" in lowered:
            return "Похоже на защиту Cloudflare (бот-запрос отклонен)."

        soup = BeautifulSoup(html, "html.parser")
        for selector in (".alert-danger", ".error", "#error_message", "h1", "title"):
            node = soup.select_one(selector)
            if node is None:
                continue
            text = node.get_text(" ", strip=True)
            if text:
                return f"Деталь: {text[:300]}"

        plain = " ".join(html.split())
        if plain:
            return f"Фрагмент ответа: {plain[:300]}"
        return ""

    def _extract_error_headers(self, headers: aiohttp.typedefs.LooseHeaders) -> str:
        if not headers:
            return ""

        raw_map = dict(headers)
        header_map = {str(key).casefold(): str(value) for key, value in raw_map.items()}

        server = header_map.get("server", "").strip()
        cf_ray = header_map.get("cf-ray", "").strip()
        content_type = header_map.get("content-type", "").strip()
        location = header_map.get("location", "").strip()

        parts: list[str] = []
        if server:
            parts.append(f"Server={server}")
        if cf_ray:
            parts.append(f"CF-Ray={cf_ray}")
        if content_type:
            parts.append(f"Content-Type={content_type}")
        if location:
            parts.append(f"Location={location}")

        if not parts:
            return ""
        return "Заголовки: " + ", ".join(parts)

    def _extract_request_form(self, html: str, page_url: str) -> RequestFormData:
        form = self._extract_form(html, page_url, action_suffix="/tournament/request")
        lower_names = [name.lower() for name in form.fields]
        if not any("venue" in item for item in lower_names):
            raise RatingSiteError("Форма заявки не найдена на странице турнира.")
        return form

    def _extract_request_prefill(self, html: str) -> RequestPrefill:
        soup = BeautifulSoup(html, "html.parser")
        form = self._find_target_form(soup, action_suffix="/tournament/request")
        if form is None:
            return RequestPrefill()

        venue_id, venue_label = self._extract_selected_option(
            form=form,
            candidate_tokens=("venue",),
        )
        representative_id, representative_label = self._extract_selected_option(
            form=form,
            candidate_tokens=("representative",),
        )
        approximate_teams_count = self._extract_integer_input(
            form=form,
            candidate_tokens=(("approximate", "teams"), ("teams", "count")),
        )

        return RequestPrefill(
            venue_id=venue_id,
            venue_label=venue_label,
            representative_id=representative_id,
            representative_label=representative_label,
            approximate_teams_count=approximate_teams_count,
        )

    def _extract_form(self, html: str, page_url: str, *, action_suffix: str) -> RequestFormData:
        soup = BeautifulSoup(html, "html.parser")
        target_form = self._find_target_form(soup, action_suffix=action_suffix)
        if target_form is None:
            raise RatingSiteError("На странице не найдена форма.")

        action = str(target_form.get("action") or "").strip()
        method = str(target_form.get("method") or "GET").strip().upper() or "GET"
        action_url = self._resolve_form_action(page_url, action)
        fields = self._collect_form_fields(target_form)
        return RequestFormData(action_url=action_url, method=method, fields=fields)

    def _find_target_form(self, soup: BeautifulSoup, *, action_suffix: str) -> Optional[Tag]:
        forms = soup.find_all("form")
        if not forms:
            return None

        for form in forms:
            action = str(form.get("action") or "")
            if action.endswith(action_suffix) or action_suffix in action:
                return form

        return forms[0]

    def _extract_selected_option(
        self,
        *,
        form: Tag,
        candidate_tokens: tuple[str, ...],
    ) -> tuple[Optional[int], Optional[str]]:
        select_tag: Optional[Tag] = None
        for candidate in form.find_all("select"):
            if not isinstance(candidate, Tag):
                continue
            name = str(candidate.get("name") or "").strip().lower()
            if not name:
                continue
            normalized = name.replace("_", "")
            if all(token in normalized for token in candidate_tokens):
                select_tag = candidate
                break

        if select_tag is None:
            return None, None

        selected_option = select_tag.find("option", selected=True)
        if selected_option is None:
            selected_option = select_tag.find("option")
        if selected_option is None:
            return None, None

        value = str(selected_option.get("value") or "").strip()
        label = selected_option.get_text(" ", strip=True) or None
        if not value.isdigit():
            return None, label
        return int(value), label

    def _extract_integer_input(
        self,
        *,
        form: Tag,
        candidate_tokens: tuple[tuple[str, ...], ...],
    ) -> Optional[int]:
        for input_tag in form.find_all("input"):
            if not isinstance(input_tag, Tag):
                continue
            name = str(input_tag.get("name") or "").strip()
            if not name:
                continue
            normalized = name.lower().replace("_", "")
            if not any(all(token in normalized for token in tokens) for tokens in candidate_tokens):
                continue

            value = str(input_tag.get("value") or "").strip()
            if value.isdigit():
                return int(value)

        return None

    def _resolve_form_action(self, page_url: str, action: str) -> str:
        raw_action = (action or "").strip()
        if not raw_action or raw_action == "#" or raw_action.casefold().startswith("javascript:"):
            return page_url

        candidate = urljoin(page_url, raw_action)
        parsed = urlparse(candidate)
        if parsed.scheme not in {"http", "https"}:
            return page_url
        return candidate

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
        representative_id: Optional[int],
        date_start: str,
        approximate_teams_count: Optional[int],
        narrator_id: Optional[int],
        comment: Optional[str],
    ) -> None:
        self._set_field_value(fields, ("tournament", "id"), str(tournament_id))
        self._set_field_value(fields, ("venue",), str(venue_id), required=True)
        if representative_id is not None:
            self._set_field_value(fields, ("representative",), str(representative_id), required=True)
        self._set_field_value(fields, ("date", "start"), date_start, required=True)

        if approximate_teams_count is not None:
            self._set_field_value(
                fields,
                ("approximate", "teams"),
                str(approximate_teams_count),
            )
            self._set_field_value(fields, ("teams", "count"), str(approximate_teams_count))

        if narrator_id is not None:
            self._set_field_value(fields, ("narrator",), str(narrator_id), required=True)
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

    def _looks_like_guest_home(self, html: str) -> bool:
        soup = BeautifulSoup(html, "html.parser")
        login_link = soup.select_one('a[href="/login"]')
        if login_link is None:
            return False
        text = login_link.get_text(" ", strip=True).casefold()
        return "вход" in text or "регистрац" in text

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
