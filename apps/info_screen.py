#!/usr/bin/env python3
"""GDELT-powered Russian news screen for the ArtInChip USB monitor."""

from __future__ import annotations

import argparse
import configparser
import hashlib
import io
import json
import os
import queue
import select
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from html import unescape
from typing import Callable

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont
import qrcode

import aic_time

try:
    import aic_touch
except Exception:
    aic_touch = None


GDELT_DOC_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
CACHE_DIR = ".cache/info_screen"
CARD_IMAGE_CACHE: dict[tuple[str, int, int], Image.Image] = {}
FONT_CACHE: dict[tuple[int, bool], ImageFont.FreeTypeFont | ImageFont.ImageFont] = {}
RU_MONTHS = [
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
]
RU_WEEKDAYS = [
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
]


class MetaDescriptionParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.description = ""
        self.og_description = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        if tag.lower() != "meta":
            return
        values = {key.lower(): (value or "") for key, value in attrs}
        content = values.get("content", "").strip()
        if not content:
            return
        name = values.get("name", "").lower()
        prop = values.get("property", "").lower()
        if name == "description":
            self.description = content
        elif prop == "og:description":
            self.og_description = content

    def best(self) -> str:
        return " ".join(unescape(self.description or self.og_description).split())


@dataclass
class Article:
    topic: str
    title: str
    url: str
    seen_date: str
    domain: str
    source_country: str
    image_url: str
    source_language: str = ""
    description: str = ""
    headline_ru: str = ""
    summary_ru: str = ""
    full_ru: str = ""


@dataclass(frozen=True)
class NewsLayout:
    top: int
    footer_h: int
    gap: int
    card_w: int
    viewport_h: int
    card_h: int
    slot_h: int


@dataclass(frozen=True)
class TouchAction:
    kind: str
    x: int
    y: int
    dx: int = 0
    dy: int = 0


def loading_article() -> Article:
    return Article(
        topic="Система",
        title="Загрузка",
        url="",
        seen_date="",
        domain="",
        source_country="",
        image_url="",
        source_language="ru",
        description="Получаю новости из GDELT...",
        headline_ru="Загрузка",
        summary_ru="Получаю новости из GDELT...",
        full_ru="Получаю новости из GDELT...",
    )


class NewsState:
    def __init__(self):
        self._lock = threading.Lock()
        self.articles: list[Article] = [loading_article()]
        self.last_refresh = 0.0
        self.last_error = ""
        self.refreshing = False

    def get_articles(self) -> list[Article]:
        with self._lock:
            return list(self.articles)

    def start_refresh(self):
        with self._lock:
            self.refreshing = True

    def add_article(self, article: Article):
        if not article.url:
            return
        with self._lock:
            existing = [item.url for item in self.articles if item.url]
            if article.url in existing:
                return
            if not existing:
                self.articles = [article]
            else:
                self.articles.append(article)

    def finish_refresh(self, articles: list[Article], error: str = ""):
        with self._lock:
            if articles:
                merged = list(articles)
                known = {article.url for article in merged if article.url}
                for article in self.articles:
                    if article.url and article.url not in known:
                        merged.append(article)
                        known.add(article.url)
                self.articles = merged[:40]
            self.last_refresh = time.monotonic()
            self.last_error = error
            self.refreshing = False


def read_config(path: str) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.optionxform = str
    with open(path, "r", encoding="utf-8") as fh:
        cfg.read_file(fh)
    return cfg


def cfg_bool(cfg: configparser.ConfigParser, section: str, key: str, default: bool) -> bool:
    if not cfg.has_option(section, key):
        return default
    return cfg.getboolean(section, key)


def cfg_int(cfg: configparser.ConfigParser, section: str, key: str, default: int) -> int:
    if not cfg.has_option(section, key):
        return default
    return cfg.getint(section, key)


def cfg_float(cfg: configparser.ConfigParser, section: str, key: str, default: float) -> float:
    if not cfg.has_option(section, key):
        return default
    return cfg.getfloat(section, key)


def news_layout(width: int, height: int, visible_count: int) -> NewsLayout:
    top = 8
    footer_h = 34
    bottom = height - footer_h - 6
    gap = 4
    card_w = width - 16
    viewport_h = max(1, bottom - top)
    card_h = max(36, (viewport_h - gap * (visible_count - 1)) // visible_count)
    slot_h = card_h + gap
    return NewsLayout(top, footer_h, gap, card_w, viewport_h, card_h, slot_h)


def hit_news_card(x: int, y: int, width: int, height: int, visible_count: int) -> int | None:
    layout = news_layout(width, height, visible_count)
    if x < 8 or x >= width - 8:
        return None
    local_y = y - layout.top
    if local_y < 0 or local_y >= layout.viewport_h:
        return None
    slot = local_y // layout.slot_h
    if slot < 0 or slot >= visible_count:
        return None
    if local_y - slot * layout.slot_h >= layout.card_h:
        return None
    return slot


def button_rects(width: int, height: int) -> dict[str, tuple[int, int, int, int]]:
    return {
        "back": (14, 10, 132, 48),
        "qr": (width - 88, 10, width - 14, 48),
    }


def hit_button(x: int, y: int, rects: dict[str, tuple[int, int, int, int]]) -> str:
    for name, (left, top, right, bottom) in rects.items():
        if left <= x <= right and top <= y <= bottom:
            return name
    return ""


def scroll_index(start: int, delta: int, article_count: int) -> int:
    if article_count <= 0:
        return 0
    return (start + delta) % article_count


def article_signature(articles: list[Article]) -> tuple[tuple[str, str], ...]:
    return tuple((article.url, article.headline_ru or article.title) for article in articles)


def article_urls(articles: list[Article]) -> tuple[str, ...]:
    return tuple(article.url for article in articles if article.url)


def has_translation(article: Article) -> bool:
    return bool(article.url and article.headline_ru and article.summary_ru and article.full_ru)


def translated_article_by_url(articles: list[Article]) -> dict[str, Article]:
    return {article.url: article for article in articles if has_translation(article)}


def reuse_translation(article: Article, cached: Article) -> Article:
    article.description = cached.description or article.description
    article.headline_ru = cached.headline_ru
    article.summary_ru = cached.summary_ru
    article.full_ru = cached.full_ru
    article.source_language = cached.source_language or article.source_language
    return article


def http_get_json(url: str, params: dict[str, str | int], timeout: float) -> dict:
    full_url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(full_url, headers={"User-Agent": "aic-info-screen/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = resp.read()
    return json.loads(payload.decode("utf-8", errors="replace"))


def fetch_gdelt_topic(
    topic: str,
    query: str,
    timespan: str,
    max_records: int,
    sort: str,
    timeout: float,
    retries: int,
    retry_after_429: float,
) -> list[Article]:
    params = {
        "query": query,
        "mode": "ArtList",
        "format": "json",
        "maxrecords": max_records,
        "sort": sort,
        "timespan": timespan,
    }
    for attempt in range(retries + 1):
        try:
            data = http_get_json(GDELT_DOC_URL, params, timeout)
            break
        except urllib.error.HTTPError as exc:
            if exc.code != 429 or attempt >= retries:
                raise
            time.sleep(retry_after_429)
    articles = []
    for item in data.get("articles", []):
        title = unescape(str(item.get("title", "")).strip())
        url = str(item.get("url", "")).strip()
        if not title or not url:
            continue
        articles.append(
            Article(
                topic=topic,
                title=title,
                url=url,
                seen_date=str(item.get("seendate", "")).strip(),
                domain=str(item.get("domain", "")).strip(),
                source_country=str(item.get("sourcecountry", "")).strip(),
                image_url=str(item.get("socialimage", "")).strip(),
                source_language=str(item.get("language", "")).strip(),
            )
        )
    return articles


def post_json(url: str, payload: dict, timeout: float) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "aic-info-screen/0.1"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        try:
            return post_json_via_nc(url, payload, timeout)
        except Exception as nc_exc:
            raise RuntimeError(f"{exc}; nc fallback failed: {nc_exc}") from nc_exc
    if not data.strip():
        raise RuntimeError(f"empty JSON response from {url}")
    return json.loads(data)


def post_json_via_nc(url: str, payload: dict, timeout: float) -> dict:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "http" or not parsed.hostname:
        raise RuntimeError(f"nc fallback supports only plain http URLs, got {url}")
    port = parsed.port or 80
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    body = json.dumps(payload).encode("utf-8")
    host_header = parsed.hostname if port == 80 else f"{parsed.hostname}:{port}"
    request = (
        f"POST {path} HTTP/1.1\r\n"
        f"Host: {host_header}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("ascii") + body
    raw, stderr, returncode = run_nc_http_request(parsed.hostname, port, request, timeout)
    if returncode not in (0, None) and not raw:
        raise RuntimeError(stderr or f"nc exited with code {returncode}")
    header, response_body = split_final_http_response(raw)
    if not header:
        raise RuntimeError("invalid HTTP response from nc fallback")
    status_line = header.splitlines()[0].decode("ascii", errors="replace")
    parts = status_line.split()
    status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    if status < 200 or status >= 300:
        raise RuntimeError(status_line)
    if has_chunked_transfer(header):
        response_body = decode_chunked_body(response_body)
    data = response_body.decode("utf-8", errors="replace")
    if not data.strip():
        raise RuntimeError(f"empty JSON response from {url}")
    return json.loads(data)


def run_nc_http_request(host: str, port: int, request: bytes, timeout: float) -> tuple[bytes, str, int | None]:
    # vLLM/uvicorn cancels generation when the client half-closes immediately.
    # Keep nc stdin open while reading the response instead of using communicate(input=...).
    proc = subprocess.Popen(
        ["nc", "-w", str(max(1, int(timeout))), host, str(port)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdin is not None
    assert proc.stdout is not None
    assert proc.stderr is not None
    chunks: list[bytes] = []
    errors: list[bytes] = []
    deadline = time.monotonic() + timeout + 2
    try:
        proc.stdin.write(request)
        proc.stdin.flush()
        streams = [proc.stdout, proc.stderr]
        while time.monotonic() < deadline:
            readable, _, _ = select.select(streams, [], [], min(0.25, max(0.0, deadline - time.monotonic())))
            for stream in readable:
                data = os.read(stream.fileno(), 8192)
                if stream is proc.stdout:
                    if data:
                        chunks.append(data)
                elif data:
                    errors.append(data)
            raw = b"".join(chunks)
            if raw and http_response_complete(raw):
                break
            if proc.poll() is not None:
                for stream, target in ((proc.stdout, chunks), (proc.stderr, errors)):
                    while True:
                        readable, _, _ = select.select([stream], [], [], 0)
                        if not readable:
                            break
                        data = os.read(stream.fileno(), 8192)
                        if not data:
                            break
                        target.append(data)
                break
        return b"".join(chunks), b"".join(errors).decode("utf-8", errors="replace").strip(), proc.poll()
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()


def split_final_http_response(raw: bytes) -> tuple[bytes, bytes]:
    rest = raw
    while True:
        header, sep, body = rest.partition(b"\r\n\r\n")
        if not sep:
            return b"", b""
        status_line = header.splitlines()[0].decode("ascii", errors="replace")
        parts = status_line.split()
        status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        if 100 <= status < 200:
            rest = body
            continue
        return header, body


def has_chunked_transfer(header: bytes) -> bool:
    return b"transfer-encoding: chunked" in header.lower()


def content_length_from_header(header: bytes) -> int | None:
    for line in header.splitlines()[1:]:
        name, sep, value = line.partition(b":")
        if sep and name.strip().lower() == b"content-length":
            try:
                return int(value.strip())
            except ValueError:
                return None
    return None


def http_response_complete(raw: bytes) -> bool:
    header, body = split_final_http_response(raw)
    if not header:
        return False
    if has_chunked_transfer(header):
        return b"\r\n0\r\n" in body or b"\r\n0\r\n\r\n" in body
    length = content_length_from_header(header)
    return length is not None and len(body) >= length


def decode_chunked_body(body: bytes) -> bytes:
    out = bytearray()
    rest = body
    while rest:
        line, sep, rest = rest.partition(b"\r\n")
        if not sep:
            break
        try:
            size = int(line.split(b";", 1)[0].strip(), 16)
        except ValueError:
            return body
        if size == 0:
            break
        if len(rest) < size:
            return body
        out.extend(rest[:size])
        rest = rest[size + 2 :] if rest[size : size + 2] == b"\r\n" else rest[size:]
    return bytes(out)


def post_json_with_retries(
    url: str,
    payload: dict,
    timeout: float,
    retries: int,
    retry_delay: float,
    verbose: bool = False,
) -> dict:
    last_exc: Exception | None = None
    attempts = max(1, retries + 1)
    for attempt in range(attempts):
        try:
            return post_json(url, payload, timeout)
        except Exception as exc:
            last_exc = exc
            if "empty JSON response" in str(exc):
                break
            if any(marker in str(exc) for marker in ("HTTP Error 404", "HTTP/1.1 404", "HTTP Error 405", "HTTP/1.1 405")):
                break
            if attempt >= attempts - 1:
                break
            delay = retry_delay * (attempt + 1)
            if verbose:
                print(f"model retry {attempt + 1}/{attempts - 1} after error: {exc}")
            time.sleep(delay)
    raise last_exc or RuntimeError(f"failed to post JSON to {url}")


def fetch_article_description(url: str, timeout: float = 6) -> str:
    if not url:
        return ""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "aic-info-screen/0.1"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content_type = resp.headers.get("Content-Type", "")
            if "text/html" not in content_type.lower():
                return ""
            html = resp.read(320_000).decode("utf-8", errors="replace")
        parser = MetaDescriptionParser()
        parser.feed(html)
        return parser.best()
    except Exception:
        return ""


def parse_model_news_pair(
    text: str,
    max_headline_chars: int,
    max_summary_chars: int,
    max_full_chars: int,
) -> tuple[str, str, str, str]:
    cleaned = strip_model_thinking(text)
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
    if "{" in cleaned and "}" in cleaned:
        cleaned = cleaned[cleaned.find("{") : cleaned.rfind("}") + 1]
    try:
        data = json.loads(cleaned)
        headline = " ".join(str(data.get("headline", "")).split())
        summary = " ".join(str(data.get("summary", "") or data.get("card", "")).split())
        full = " ".join(str(data.get("full_translation", "") or data.get("full_ru", "") or summary).split())
        language = " ".join(str(data.get("source_language", "") or data.get("lang", "")).split())
        if headline or summary:
            return headline[:max_headline_chars], summary[:max_summary_chars], full[:max_full_chars], normalize_language_prefix(language)
    except Exception:
        pass

    lines = [" ".join(line.split()) for line in cleaned.splitlines() if line.strip()]
    if len(lines) >= 2:
        return lines[0][:max_headline_chars], lines[1][:max_summary_chars], " ".join(lines[:4])[:max_full_chars], ""
    line = lines[0] if lines else cleaned
    return line[:max_headline_chars], line[:max_summary_chars], line[:max_full_chars], ""


def strip_model_thinking(text: str) -> str:
    cleaned = text.strip()
    while "<think>" in cleaned and "</think>" in cleaned:
        start = cleaned.find("<think>")
        end = cleaned.find("</think>", start) + len("</think>")
        cleaned = (cleaned[:start] + cleaned[end:]).strip()
    return cleaned


def normalize_language_prefix(value: str) -> str:
    value = value.strip().lower()
    aliases = {
        "english": "en",
        "английский": "en",
        "russian": "ru",
        "русский": "ru",
        "french": "fr",
        "german": "de",
        "spanish": "es",
        "italian": "it",
        "chinese": "zh",
        "japanese": "ja",
        "korean": "ko",
        "arabic": "ar",
    }
    value = aliases.get(value, value)
    if not value:
        return ""
    return value[:2].upper()


def headline_with_language(article: Article) -> str:
    headline = article.headline_ru or article.title
    prefix = normalize_language_prefix(article.source_language)
    if prefix:
        return f"[{prefix}] {headline}"
    return headline


def article_original_text(article: Article) -> str:
    parts = [article.title]
    if article.description and article.description != article.title:
        parts.append(article.description)
    return " ".join(" ".join(part.split()) for part in parts if part.strip())


def has_cyrillic(text: str) -> bool:
    return any("а" <= char.lower() <= "я" or char.lower() == "ё" for char in text)


def clean_model_translation(text: str) -> str:
    lines = [" ".join(line.split()) for line in text.splitlines() if line.strip()]
    russian = [line for line in lines if has_cyrillic(line)]
    cleaned = " ".join(russian or lines).strip()
    return cleaned.lstrip(" .:-–—")


def meaningful_chars(text: str) -> int:
    return sum(1 for char in text if char.isalnum())


def summarize_openai(cfg: configparser.ConfigParser, article: Article) -> tuple[str, str, str, str]:
    base_url = cfg.get("model", "base_url", fallback="http://192.168.228.55:8085").rstrip("/")
    model = cfg.get("model", "model", fallback="local-model")
    model_api = cfg.get("model", "api", fallback="").strip().lower()
    timeout = cfg_float(cfg, "model", "timeout_seconds", 45)
    retries = cfg_int(cfg, "model", "retries", 2)
    retry_delay = cfg_float(cfg, "model", "retry_delay_seconds", 2.0)
    variant_retry_delay = cfg_float(cfg, "model", "variant_retry_delay_seconds", 0.5)
    chat_completions = cfg_bool(cfg, "model", "chat_completions", True)
    structured_completion = cfg_bool(cfg, "model", "structured_completion", False)
    verbose = cfg_bool(cfg, "display", "verbose", False)
    temperature = cfg_float(cfg, "model", "temperature", 0.2)
    min_translation_chars = cfg_int(cfg, "model", "min_translation_chars", 18)
    max_summary_chars = cfg_int(cfg, "model", "max_chars", 120)
    max_headline_chars = cfg_int(cfg, "model", "headline_chars", 62)
    max_full_chars = cfg_int(cfg, "model", "full_chars", 520)
    prompt = (
        "Сделай русскую новостную карточку и полный русский текст для экрана 480x480. "
        "Всегда переводи headline, summary и full_translation на русский язык. "
        "Верни только JSON без markdown: "
        '{"source_language":"en","headline":"короткий русский заголовок","summary":"суть новости на русском","full_translation":"полный русский перевод новости"}. '
        "source_language - ISO 639-1 язык исходного заголовка/описания, например en, ru, de. "
        f"headline до {max_headline_chars} символов. "
        f"summary до {max_summary_chars} символов: одна короткая строка, смысл новости без искажения. "
        f"full_translation до {max_full_chars} символов: связный перевод заголовка и описания. "
        "Не добавляй фактов, которых нет в заголовке или описании.\n\n"
        f"Тема: {article.topic}\n"
        f"Язык из GDELT: {article.source_language}\n"
        f"Заголовок: {article.title}\n"
        f"Описание: {article.description}\n"
        f"Источник: {article.domain}"
    )

    if not model_api:
        model_api = "openai_chat" if chat_completions else "llama_cpp"

    def model_url(kind: str) -> str:
        parsed_path = urllib.parse.urlparse(base_url).path.rstrip("/")
        if kind == "chat":
            if parsed_path.endswith("/v1/chat/completions"):
                return base_url
            if parsed_path.endswith("/v1"):
                return base_url + "/chat/completions"
            return base_url + "/v1/chat/completions"
        if kind == "ollama_chat":
            if parsed_path.endswith("/api/chat"):
                return base_url
            return base_url + "/api/chat"
        if kind == "ollama_generate":
            if parsed_path.endswith("/api/generate"):
                return base_url
            return base_url + "/api/generate"
        if parsed_path.endswith("/completion"):
            return base_url
        return base_url + "/completion"

    def chat_completion(prompt_text: str, tokens: int = 260) -> str:
        data = post_json_with_retries(
            model_url("chat"),
            {
                "model": model,
                "messages": [
                    {"role": "system", "content": "Ты переводчик и редактор русскоязычного новостного экрана."},
                    {"role": "user", "content": prompt_text},
                ],
                "temperature": temperature,
                "max_tokens": tokens,
            },
            timeout,
            retries,
            retry_delay,
            verbose,
        )
        choices = data.get("choices") or []
        if choices:
            first = choices[0]
            message = first.get("message") if isinstance(first, dict) else None
            if isinstance(message, dict):
                content = strip_model_thinking(str(message.get("content", ""))).strip()
                if content:
                    return content
            content = strip_model_thinking(str(first.get("text", "") if isinstance(first, dict) else "")).strip()
            if content:
                return content
        content = strip_model_thinking(str(data.get("content", "") or data.get("response", ""))).strip()
        if content:
            return content
        raise RuntimeError("chat model returned empty content")

    def completion(prompt_text: str, tokens: int = 120, params: dict | None = None) -> str:
        def ollama_generate() -> str:
            data = post_json_with_retries(
                model_url("ollama_generate"),
                {
                    "model": model,
                    "prompt": prompt_text,
                    "stream": False,
                    "options": {
                        "temperature": temperature,
                        "num_predict": tokens,
                    },
                },
                timeout,
                retries,
                retry_delay,
                verbose,
            )
            content = strip_model_thinking(str(data.get("response", "") or data.get("content", ""))).strip()
            if content:
                return content
            raise RuntimeError("ollama model returned empty content")

        def llama_completion() -> str:
            generation = {
                "prompt": prompt_text,
                "temperature": temperature,
                "n_predict": tokens,
                "max_tokens": tokens,
                "cache_prompt": False,
                "id_slot": 0,
            }
            if params:
                generation.update(params)
            data = post_json_with_retries(
                model_url("completion"),
                generation,
                timeout,
                retries,
                retry_delay,
                verbose,
            )
            content = strip_model_thinking(str(data.get("content", "") or data.get("response", ""))).strip()
            if not content:
                raise RuntimeError("model returned empty content")
            return content

        if model_api in ("openai", "openai_chat", "chat", "chat_completions", "vllm", "vllm_chat"):
            return chat_completion(prompt_text, tokens)
        if model_api in ("ollama", "ollama_generate", "ollama_api"):
            return ollama_generate()
        if model_api in ("llama", "llama_cpp", "completion"):
            return llama_completion()
        if model_api != "auto":
            raise RuntimeError(f"unsupported model api: {model_api}")

        order = (chat_completion, ollama_generate, llama_completion) if chat_completions else (llama_completion, ollama_generate, chat_completion)
        errors = []
        for fn in order:
            try:
                return fn(prompt_text, tokens) if fn is chat_completion else fn()
            except Exception as exc:
                errors.append(str(exc))
                if verbose:
                    print(f"model endpoint fallback after error: {exc}")
        raise RuntimeError("; ".join(errors))

    def compact_source(text: str, limit: int) -> str:
        cleaned = " ".join(text.split())
        if len(cleaned) <= limit:
            return cleaned
        sentence = cleaned.split(". ", 1)[0].strip()
        if 20 <= len(sentence) <= limit:
            return sentence
        return cleaned[:limit].rsplit(" ", 1)[0] or cleaned[:limit]

    def prompt_variants(source: str, concise: bool = True) -> list[str]:
        if concise:
            return [
                "Translate to Russian. Return only Russian text. Text: " + source,
                "Translate this news text into Russian, preserving meaning and not adding facts: " + source,
                "Russian translation only: " + source,
                "Rewrite in concise Russian, same meaning: " + source,
            ]
        return [
            "Translate the full news text to Russian. Return only Russian text. Do not summarize. Text: " + source,
            "Full Russian translation only, preserving all facts and details: " + source,
            "Translate to Russian without shortening or adding facts: " + source,
        ]

    def generation_variants(tokens: int) -> list[dict]:
        return [
            {"temperature": temperature, "top_k": 40, "top_p": 0.95, "min_p": 0.05, "n_predict": tokens, "max_tokens": tokens},
            {"temperature": max(0.05, temperature / 2), "top_k": 20, "top_p": 0.85, "min_p": 0.02, "repeat_penalty": 1.05, "n_predict": tokens + 24, "max_tokens": tokens + 24},
            {"temperature": min(0.45, temperature + 0.2), "top_k": 60, "top_p": 0.92, "min_p": 0.01, "repeat_penalty": 1.1, "n_predict": tokens + 48, "max_tokens": tokens + 48},
        ]

    def validate_translation(translated: str, source: str) -> str:
        cleaned = clean_model_translation(translated)
        if not has_cyrillic(cleaned):
            raise RuntimeError("model response is not Russian")
        min_chars = min(min_translation_chars, max(8, meaningful_chars(source) // 3))
        if meaningful_chars(cleaned) < min_chars:
            raise RuntimeError(f"model response is too short: {meaningful_chars(cleaned)} < {min_chars}")
        return cleaned

    def translate_short(text: str, limit: int, tokens: int, concise: bool = True) -> str:
        candidates = [
            compact_source(text, limit),
            compact_source(text, max(60, limit // 2)),
            compact_source(text, 48),
        ]
        last_exc: Exception | None = None
        for candidate in candidates:
            if not candidate:
                continue
            for prompt_text in prompt_variants(candidate, concise=concise):
                for params in generation_variants(tokens):
                    try:
                        translated = completion(prompt_text, tokens, params=params)
                        return validate_translation(translated, candidate)
                    except Exception as exc:
                        last_exc = exc
                        if verbose:
                            print(
                                f"model short translate failed for {len(candidate)} chars "
                                f"temp={params.get('temperature')} top_k={params.get('top_k')}: {exc}"
                            )
                        if variant_retry_delay > 0:
                            time.sleep(variant_retry_delay)
        raise last_exc or RuntimeError("empty text for translation")

    if chat_completions and model_api not in ("ollama", "ollama_generate", "ollama_api", "llama", "llama_cpp", "completion"):
        try:
            text = chat_completion(prompt, 260)
            result = parse_model_news_pair(text, max_headline_chars, max_summary_chars, max_full_chars)
            if result[0] and result[1]:
                return result
        except Exception as exc:
            if verbose:
                print(f"model chat endpoint failed, fallback to /completion: {exc}")

    original = article_original_text(article)
    if structured_completion:
        try:
            text = completion("Return only JSON. Translate this news to Russian. Keys: source_language, headline, summary, full_translation. " + compact_source(original, 240), 260)
            result = parse_model_news_pair(text, max_headline_chars, max_summary_chars, max_full_chars)
            if result[0] and result[1]:
                return result
        except Exception as exc:
            if verbose:
                print(f"model structured completion failed, fallback to short translations: {exc}")

    headline = translate_short(article.title, 90, 80)
    summary_source = article.description or article.title
    summary = translate_short(summary_source, 120, 90)
    full_source_limit = min(max_full_chars, max(180, len(original)))
    full_tokens = min(520, max(180, full_source_limit // 3))
    full = translate_short(original, full_source_limit, full_tokens, concise=False)
    language = normalize_language_prefix(article.source_language)
    return headline[:max_headline_chars], summary[:max_summary_chars], full[:max_full_chars], language


def fallback_news_pair(article: Article, max_headline_chars: int, max_summary_chars: int, max_full_chars: int) -> tuple[str, str, str, str]:
    headline = " ".join(article.title.split())[:max_headline_chars]
    summary_source = article.description or article.title
    summary = " ".join(summary_source.split())[:max_summary_chars]
    full = article_original_text(article)[:max_full_chars]
    return headline, summary, full, normalize_language_prefix(article.source_language)


def summarize_articles(
    cfg: configparser.ConfigParser,
    articles: list[Article],
    verbose: bool,
    on_article: Callable[[Article], None] | None = None,
    stop_event: threading.Event | None = None,
    known_articles: dict[str, Article] | None = None,
) -> list[Article]:
    enabled = cfg_bool(cfg, "model", "enabled", True)
    max_summary_chars = cfg_int(cfg, "model", "max_chars", 120)
    max_headline_chars = cfg_int(cfg, "model", "headline_chars", 62)
    max_full_chars = cfg_int(cfg, "model", "full_chars", 520)
    description_timeout = cfg_float(cfg, "gdelt", "article_description_timeout_seconds", 2.0)
    provider = cfg.get("model", "provider", fallback="openai").strip().lower()
    translated: list[Article] = []
    for article in articles:
        if stop_event is not None and stop_event.is_set():
            break
        cached = known_articles.get(article.url) if known_articles is not None else None
        if cached is not None:
            translated.append(reuse_translation(article, cached))
            if verbose:
                print(f"model cache hit: {article.topic}: {article.url}")
            continue
        article.description = fetch_article_description(article.url, timeout=description_timeout)
        if not enabled:
            article.headline_ru, article.summary_ru, article.full_ru, article.source_language = fallback_news_pair(article, max_headline_chars, max_summary_chars, max_full_chars)
            translated.append(article)
            if known_articles is not None and article.url:
                known_articles[article.url] = article
            if on_article is not None:
                on_article(article)
            continue
        try:
            if provider != "openai":
                raise RuntimeError(f"unsupported model provider: {provider}")
            article.headline_ru, article.summary_ru, article.full_ru, model_language = summarize_openai(cfg, article)
            if model_language:
                article.source_language = model_language
            if not article.headline_ru or not article.summary_ru:
                raise RuntimeError("model returned empty headline or summary")
            translated.append(article)
            if known_articles is not None and article.url:
                known_articles[article.url] = article
            if on_article is not None:
                on_article(article)
            if verbose:
                print(f"model ok: {article.topic}: [{article.source_language}] {article.headline_ru} / {article.summary_ru}")
        except Exception as exc:
            if verbose:
                print(f"model skipped untranslated article {article.url}: {exc}")
    return translated


def load_news(
    cfg: configparser.ConfigParser,
    verbose: bool,
    on_article: Callable[[Article], None] | None = None,
    stop_event: threading.Event | None = None,
    existing_articles: list[Article] | None = None,
) -> list[Article]:
    if not cfg.has_section("topics"):
        raise RuntimeError("config has no [topics] section")
    timespan = cfg.get("gdelt", "timespan", fallback="24h")
    max_records = cfg_int(cfg, "gdelt", "max_records_per_topic", 4)
    sort = cfg.get("gdelt", "sort", fallback="DateDesc")
    delay = cfg_float(cfg, "gdelt", "request_delay_seconds", 6)
    retries = cfg_int(cfg, "gdelt", "retries", 1)
    retry_after_429 = cfg_float(cfg, "gdelt", "retry_after_429_seconds", 30)
    translated: list[Article] = []
    known_articles = translated_article_by_url(existing_articles or [])
    topics = list(cfg.items("topics"))
    for index, (topic, query) in enumerate(topics):
        if stop_event is not None and stop_event.is_set():
            break
        if index:
            delay_until = time.monotonic() + delay
            while time.monotonic() < delay_until:
                if stop_event is not None and stop_event.is_set():
                    break
                time.sleep(0.1)
        try:
            batch = fetch_gdelt_topic(
                topic,
                query,
                timespan,
                max_records,
                sort,
                20,
                retries,
                retry_after_429,
            )
            if verbose:
                print(f"gdelt {topic}: {len(batch)} articles")
            translated.extend(
                summarize_articles(
                    cfg,
                    batch,
                    verbose,
                    on_article=on_article,
                    stop_event=stop_event,
                    known_articles=known_articles,
                )
            )
        except Exception as exc:
            if verbose:
                print(f"gdelt failed for {topic}: {exc}")
    return translated


def ensure_cache_dir():
    os.makedirs(CACHE_DIR, exist_ok=True)


def cache_path_for_url(url: str) -> str:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return os.path.join(CACHE_DIR, f"{digest}.img")


def normalize_background_image(img: Image.Image) -> Image.Image:
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "error",
                message="Palette images with Transparency expressed in bytes.*",
                category=UserWarning,
            )
            if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                return flatten_transparency(img)
            return img.convert("RGB")
    except Exception:
        return flatten_transparency(img)


def flatten_transparency(img: Image.Image, background: tuple[int, int, int] = (8, 11, 17)) -> Image.Image:
    rgba = img.convert("RGBA")
    base = Image.new("RGBA", rgba.size, (*background, 255))
    base.alpha_composite(rgba)
    return base.convert("RGB")


def fetch_image(url: str, timeout: float = 8) -> Image.Image | None:
    if not url:
        return None
    ensure_cache_dir()
    path = cache_path_for_url(url)
    try:
        if os.path.exists(path):
            with Image.open(path) as img:
                return normalize_background_image(img)
        req = urllib.request.Request(url, headers={"User-Agent": "aic-info-screen/0.1"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read(2_000_000)
        with open(path, "wb") as fh:
            fh.write(data)
        with Image.open(io.BytesIO(data)) as img:
            return normalize_background_image(img)
    except Exception:
        return None


def cover_image(img: Image.Image, width: int, height: int) -> Image.Image:
    scale = max(width / img.width, height / img.height)
    new_size = (max(1, int(img.width * scale)), max(1, int(img.height * scale)))
    resized = img.resize(new_size, Image.Resampling.LANCZOS)
    left = (resized.width - width) // 2
    top = (resized.height - height) // 2
    return resized.crop((left, top, left + width, top + height))


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    cached = FONT_CACHE.get((size, bold))
    if cached is not None:
        return cached
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial Bold.ttf" if bold else "/Library/Fonts/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ]
    for path in candidates:
        if path and os.path.exists(path):
            result = ImageFont.truetype(path, size)
            FONT_CACHE[(size, bold)] = result
            return result
    result = ImageFont.load_default()
    FONT_CACHE[(size, bold)] = result
    return result


def text_width(draw: ImageDraw.ImageDraw, text: str, fnt) -> int:
    bbox = draw.textbbox((0, 0), text, font=fnt)
    return bbox[2] - bbox[0]


def ellipsize(draw: ImageDraw.ImageDraw, text: str, fnt, max_width: int) -> str:
    text = " ".join(text.split())
    if text_width(draw, text, fnt) <= max_width:
        return text
    suffix = "..."
    lo = 0
    hi = len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        candidate = text[:mid].rstrip() + suffix
        if text_width(draw, candidate, fnt) <= max_width:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + suffix


def wrap_text_lines(
    draw: ImageDraw.ImageDraw,
    text: str,
    fnt,
    max_width: int,
    max_lines: int,
) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for index, word in enumerate(words):
        trial = word if not current else f"{current} {word}"
        if text_width(draw, trial, fnt) <= max_width:
            current = trial
        else:
            if current:
                lines.append(current)
                if len(lines) == max_lines:
                    return lines[:-1] + [ellipsize(draw, current + " " + " ".join(words[index:]), fnt, max_width)]
            current = word
    if current:
        lines.append(current)
    if len(lines) > max_lines:
        kept = lines[:max_lines]
        kept[-1] = ellipsize(draw, " ".join(lines[max_lines - 1 :]), fnt, max_width)
        return kept
    return [ellipsize(draw, line, fnt, max_width) for line in lines]


def russian_datetime() -> str:
    now = datetime.now()
    return (
        f"{now:%H:%M:%S} {now.day:02d} {RU_MONTHS[now.month - 1]} "
        f"{now:%y} {RU_WEEKDAYS[now.weekday()]}"
    )


def card_background(article: Article, width: int, height: int) -> Image.Image:
    background_id = article.image_url or f"{article.topic}:{article.domain}:{article.title}"
    key = (background_id, width, height)
    cached = CARD_IMAGE_CACHE.get(key)
    if cached is not None:
        return cached.copy()

    bg = fetch_image(article.image_url)
    if bg is None:
        seed = int(hashlib.sha256((article.topic + article.domain + article.title).encode("utf-8")).hexdigest()[:6], 16)
        base = (16 + seed % 24, 22 + (seed >> 4) % 24, 30 + (seed >> 8) % 30)
        accent = (20 + (seed >> 12) % 45, 58 + (seed >> 16) % 55, 70 + (seed >> 20) % 50)
        bg = Image.blend(Image.new("RGB", (width, height), base), Image.new("RGB", (width, height), accent), 0.45)
    else:
        bg = cover_image(bg, width, height).filter(ImageFilter.GaussianBlur(radius=0.8))
        bg = ImageEnhance.Color(bg).enhance(0.8)

    dark = Image.new("RGB", (width, height), (0, 0, 0))
    bg = Image.blend(bg, dark, 0.58)
    CARD_IMAGE_CACHE[key] = bg.copy()
    return bg


def render_news_card(article: Article, width: int, height: int, index: int) -> Image.Image:
    card = card_background(article, width, height).convert("RGBA")
    shade = Image.new("RGBA", (width, height), (0, 0, 0, 72))
    card = Image.alpha_composite(card, shade)

    draw = ImageDraw.Draw(card)
    headline_font = font(13, bold=True)
    summary_font = font(13)

    draw.rounded_rectangle((0, 0, width - 1, height - 1), radius=7, outline=(255, 255, 255, 42), width=1)

    headline = headline_with_language(article)
    draw.text((10, 4), ellipsize(draw, headline, headline_font, width - 20), fill=(248, 250, 252), font=headline_font)

    summary = article.summary_ru or article.description or article.title
    draw.text((10, 23), ellipsize(draw, summary, summary_font, width - 20), fill=(216, 226, 236), font=summary_font)

    mask = Image.new("L", (width, height), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle((0, 0, width - 1, height - 1), radius=7, fill=255)
    clipped = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    clipped.paste(card, (0, 0), mask)
    return clipped


def visible_article(articles: list[Article], start_index: int, offset: int) -> Article:
    if not articles:
        return loading_article()
    return articles[(start_index + offset) % len(articles)]


def render_news_board(
    articles: list[Article],
    width: int,
    height: int,
    start_index: int,
    previous_start_index: int,
    transition_progress: float,
    visible_count: int,
) -> Image.Image:
    img = Image.new("RGB", (width, height), (7, 9, 13))
    layout = news_layout(width, height, visible_count)

    in_transition = transition_progress < 1.0 and previous_start_index != start_index
    base_start = previous_start_index if in_transition else start_index
    y_offset = -int(layout.slot_h * max(0.0, min(1.0, transition_progress))) if in_transition else 0

    viewport = Image.new("RGBA", (width, layout.viewport_h), (0, 0, 0, 0))
    cards_to_draw = visible_count + (1 if in_transition else 0)
    for i in range(cards_to_draw):
        y = y_offset + i * layout.slot_h
        if y >= layout.viewport_h or y + layout.card_h <= 0:
            continue
        article = visible_article(articles, base_start, i)
        card = render_news_card(article, layout.card_w, layout.card_h, (base_start + i) % max(1, len(articles)))
        viewport.alpha_composite(card, (8, y))

    img = img.convert("RGBA")
    img.alpha_composite(viewport, (0, layout.top))

    footer = Image.new("RGBA", (width, layout.footer_h + 6), (0, 0, 0, 155))
    footer_draw = ImageDraw.Draw(footer)
    clock_font = font(17, bold=True)
    clock = russian_datetime()
    clock_w = text_width(footer_draw, clock, clock_font)
    footer_draw.text(((width - clock_w) // 2, 8), clock, fill=(236, 241, 246), font=clock_font)
    img.alpha_composite(footer, (0, height - layout.footer_h - 2))

    if len(articles) == 1 and articles[0].url == "":
        status_font = font(12)
        status = "ожидание обновления"
        draw = ImageDraw.Draw(img)
        draw.text((width - text_width(draw, status, status_font) - 10, 3), status, fill=(130, 143, 158), font=status_font)

    return img.convert("RGB")


def render_button(draw: ImageDraw.ImageDraw, rect: tuple[int, int, int, int], label: str):
    left, top, right, bottom = rect
    draw.rounded_rectangle(rect, radius=7, fill=(20, 32, 44), outline=(108, 207, 238), width=1)
    fnt = font(16, bold=True)
    label_w = text_width(draw, label, fnt)
    bbox = draw.textbbox((0, 0), label, font=fnt)
    label_h = bbox[3] - bbox[1]
    draw.text(
        ((left + right - label_w) // 2, (top + bottom - label_h) // 2 - 2),
        label,
        fill=(240, 247, 252),
        font=fnt,
    )


def render_footer_clock(img: Image.Image, width: int, height: int):
    footer_h = 34
    footer = Image.new("RGBA", (width, footer_h + 6), (0, 0, 0, 155))
    footer_draw = ImageDraw.Draw(footer)
    clock_font = font(17, bold=True)
    clock = russian_datetime()
    clock_w = text_width(footer_draw, clock, clock_font)
    footer_draw.text(((width - clock_w) // 2, 8), clock, fill=(236, 241, 246), font=clock_font)
    img.alpha_composite(footer, (0, height - footer_h - 2))


def detail_background(article: Article, width: int, height: int) -> Image.Image:
    bg = fetch_image(article.image_url)
    if bg is None:
        bg = card_background(article, width, height)
    else:
        bg = cover_image(bg, width, height).filter(ImageFilter.GaussianBlur(radius=1.1))
    bg = ImageEnhance.Color(bg).enhance(0.75)
    return Image.blend(bg, Image.new("RGB", (width, height), (0, 0, 0)), 0.68)


DETAIL_HEADER_H = 58
DETAIL_FOOTER_H = 38


def detail_viewport_height(height: int) -> int:
    return max(1, height - DETAIL_HEADER_H - DETAIL_FOOTER_H)


def detail_text_blocks(article: Article, width: int):
    measure = Image.new("RGB", (width, 1))
    draw = ImageDraw.Draw(measure)
    margin = 20
    title_font = font(22, bold=True)
    body_font = font(16)
    label_font = font(12, bold=True)
    meta_font = font(13)
    url_font = font(12)
    text_w = width - margin * 2

    meta = article.topic
    if article.domain:
        meta = f"{article.topic} / {article.domain}"
    title = headline_with_language(article)
    body = article.full_ru or article.summary_ru or article.description or article.title

    blocks = [
        ("meta", [ellipsize(draw, meta.upper(), meta_font, text_w)], meta_font, (92, 218, 255), 17),
        ("title", wrap_text_lines(draw, title, title_font, text_w, 24), title_font, (255, 255, 255), 26),
        ("label", ["ПОЛНЫЙ ТЕКСТ"], label_font, (92, 218, 255), 17),
        ("body", wrap_text_lines(draw, body, body_font, text_w, 160), body_font, (224, 233, 242), 20),
    ]
    if article.url:
        blocks.extend(
            [
                ("label", ["ССЫЛКА"], label_font, (92, 218, 255), 17),
                ("url", wrap_text_lines(draw, article.url, url_font, text_w, 4), url_font, (160, 174, 188), 15),
            ]
        )
    return blocks


def detail_content_height(article: Article, width: int) -> int:
    blocks = detail_text_blocks(article, width)
    y = 16
    for _, lines, _, _, line_h in blocks:
        y += len(lines) * line_h + 12
    return y + 10


def detail_max_scroll(article: Article, width: int, height: int) -> int:
    return max(0, detail_content_height(article, width) - detail_viewport_height(height))


def render_detail(article: Article, width: int, height: int, scroll_y: int = 0) -> Image.Image:
    img = detail_background(article, width, height).convert("RGBA")
    margin = 20
    viewport_h = detail_viewport_height(height)
    content_h = detail_content_height(article, width)
    max_scroll = max(0, content_h - viewport_h)
    scroll_y = max(0, min(int(scroll_y), max_scroll))

    content = Image.new("RGBA", (width, content_h), (0, 0, 0, 0))
    content_draw = ImageDraw.Draw(content)
    y = 16
    for _, lines, fnt, color, line_h in detail_text_blocks(article, width):
        for line in lines:
            content_draw.text((margin, y), line, fill=color, font=fnt)
            y += line_h
        y += 12

    visible = content.crop((0, scroll_y, width, scroll_y + viewport_h))
    img.alpha_composite(visible, (0, DETAIL_HEADER_H))

    draw = ImageDraw.Draw(img)
    draw.rectangle((0, 0, width, DETAIL_HEADER_H), fill=(0, 0, 0, 185))

    rects = button_rects(width, height)
    render_button(draw, rects["back"], "Назад")
    render_button(draw, rects["qr"], "QR")
    if max_scroll > 0:
        track_top = DETAIL_HEADER_H + 8
        track_bottom = height - DETAIL_FOOTER_H - 8
        track_h = max(1, track_bottom - track_top)
        thumb_h = max(24, int(track_h * viewport_h / content_h))
        thumb_y = track_top + int((track_h - thumb_h) * scroll_y / max_scroll)
        draw.rounded_rectangle((width - 8, track_top, width - 4, track_bottom), radius=2, fill=(255, 255, 255, 42))
        draw.rounded_rectangle((width - 9, thumb_y, width - 3, thumb_y + thumb_h), radius=3, fill=(108, 207, 238, 190))
    render_footer_clock(img, width, height)
    return img.convert("RGB")


def make_qr(article: Article, size: int) -> Image.Image:
    payload = article.url or article.title
    qr = qrcode.QRCode(border=2, box_size=8)
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    return img.resize((size, size), Image.Resampling.NEAREST)


def render_qr(article: Article, width: int, height: int) -> Image.Image:
    img = Image.new("RGBA", (width, height), (7, 9, 13, 255))
    draw = ImageDraw.Draw(img)
    title_font = font(20, bold=True)
    small_font = font(13)
    margin = 22
    draw.rectangle((0, 0, width, DETAIL_HEADER_H), fill=(0, 0, 0, 185))
    rects = button_rects(width, height)
    render_button(draw, rects["back"], "Назад")

    title = headline_with_language(article)
    draw.text((margin, DETAIL_HEADER_H + 12), ellipsize(draw, title, title_font, width - margin * 2), fill=(248, 250, 252), font=title_font)

    qr_size = min(width - 104, height - 210)
    qr_img = make_qr(article, qr_size)
    qr_x = (width - qr_size) // 2
    qr_y = DETAIL_HEADER_H + 56
    img.paste(qr_img.convert("RGBA"), (qr_x, qr_y))
    draw.rounded_rectangle((qr_x - 8, qr_y - 8, qr_x + qr_size + 8, qr_y + qr_size + 8), radius=8, outline=(108, 207, 238), width=2)

    if article.domain:
        draw.text((margin, qr_y + qr_size + 20), ellipsize(draw, article.domain, small_font, width - margin * 2), fill=(170, 184, 198), font=small_font)

    render_footer_clock(img, width, height)
    return img.convert("RGB")


def touch_worker(
    actions: queue.Queue[TouchAction],
    width: int,
    height: int,
    cfg: configparser.ConfigParser,
    verbose: bool,
    stop_event: threading.Event,
):
    if aic_touch is None:
        if verbose:
            print("touch disabled: aic_touch import failed")
        return

    tap_max = cfg_int(cfg, "touch", "tap_max_pixels", 28)
    double_seconds = cfg_float(cfg, "touch", "double_tap_seconds", 0.35)
    drag_step = max(1, cfg_int(cfg, "touch", "drag_pixels_per_step", 52))
    stale_after_drag_reports = cfg_int(cfg, "touch", "stale_after_drag_reports", 3)
    stale_after_tap_reports = cfg_int(cfg, "touch", "stale_after_tap_reports", 3)
    stale_still_pixels = cfg_int(cfg, "touch", "stale_still_pixels", 1)
    no_report_release_polls = cfg_int(cfg, "touch", "no_report_release_polls", 8)
    try:
        reader = aic_touch.TouchReader(
            width,
            height,
            tap_max_pixels=tap_max,
            stale_after_drag_reports=stale_after_drag_reports,
            stale_after_tap_reports=stale_after_tap_reports,
            still_pixels=stale_still_pixels,
            no_report_release_polls=no_report_release_polls,
        )
    except Exception as exc:
        if verbose:
            print(f"touch disabled: {exc}")
        return

    down_event = None
    last_pressed_event = None
    drag_anchor = None
    drag_started = False
    last_event = None
    pending_tap: tuple[float, int, int] | None = None

    def finish_touch(ended_at: float):
        nonlocal down_event, last_pressed_event, drag_anchor, drag_started, pending_tap, last_event
        if not down_event:
            return
        down_time, down_x, down_y = down_event
        _, up_x, up_y = last_pressed_event or (ended_at, down_x, down_y)
        dx = up_x - down_x
        dy = up_y - down_y
        moved = max(abs(dx), abs(dy))
        if not drag_started and moved <= tap_max and ended_at - down_time <= 0.65:
            if pending_tap and ended_at - pending_tap[0] <= double_seconds:
                actions.put(TouchAction("double_tap", up_x, up_y))
                pending_tap = None
            else:
                pending_tap = (ended_at, up_x, up_y)
        down_event = None
        last_pressed_event = None
        drag_anchor = None
        drag_started = False
        last_event = None

    try:
        while not stop_event.is_set():
            now = time.monotonic()
            if pending_tap and now - pending_tap[0] > double_seconds:
                _, tap_x, tap_y = pending_tap
                actions.put(TouchAction("tap", tap_x, tap_y))
                pending_tap = None

            try:
                event = reader.read(timeout_ms=40)
            except Exception as exc:
                if verbose and not stop_event.is_set():
                    print(f"touch read failed: {exc}")
                break
            if event is None:
                time.sleep(0.01)
                continue
            if last_event and event.pressed == last_event.pressed and event.x == last_event.x and event.y == last_event.y:
                continue

            if event.pressed and (last_event is None or not last_event.pressed):
                down_event = (now, event.x, event.y)
                last_pressed_event = (now, event.x, event.y)
                drag_anchor = (event.x, event.y)
                drag_started = False
            elif event.pressed:
                last_pressed_event = (now, event.x, event.y)
                if down_event and drag_anchor:
                    _, down_x, down_y = down_event
                    moved_total = max(abs(event.x - down_x), abs(event.y - down_y))
                    if moved_total > tap_max:
                        drag_started = True
                        pending_tap = None
                    if drag_started:
                        anchor_x, anchor_y = drag_anchor
                        dx = event.x - anchor_x
                        dy = event.y - anchor_y
                        if max(abs(dx), abs(dy)) >= drag_step:
                            actions.put(TouchAction("drag", event.x, event.y, dx, dy))
                            drag_anchor = (event.x, event.y)
            elif not event.pressed and last_event and last_event.pressed and down_event:
                finish_touch(now)
            last_event = event
    finally:
        try:
            reader.close()
        except Exception:
            pass


def image_to_jpeg(img: Image.Image, quality: int, subsampling: int) -> bytes:
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=quality, subsampling=subsampling, progressive=False)
    return out.getvalue()


def refresh_worker(
    cfg: configparser.ConfigParser,
    state: NewsState,
    refresh_seconds: float,
    verbose: bool,
    stop_event: threading.Event,
):
    while not stop_event.is_set():
        state.start_refresh()
        try:
            existing = state.get_articles()
            fresh = load_news(cfg, verbose, on_article=state.add_article, stop_event=stop_event, existing_articles=existing)
            state.finish_refresh(fresh)
            if verbose:
                print(f"news refresh complete: {len(fresh)} articles")
        except Exception as exc:
            state.finish_refresh([], str(exc))
            if verbose:
                print(f"news refresh failed: {exc}")

        deadline = time.monotonic() + refresh_seconds
        while not stop_event.is_set() and time.monotonic() < deadline:
            time.sleep(0.5)


def run(config_path: str, once: bool):
    cfg = read_config(config_path)
    verbose = cfg_bool(cfg, "display", "verbose", True)
    quality = cfg_int(cfg, "display", "quality", 60)
    subsampling = cfg_int(cfg, "display", "subsampling", 2)
    chunk_size = cfg_int(cfg, "display", "chunk_size", 4096)
    visible_count = max(1, cfg_int(cfg, "display", "visible_news", 9))
    animation_seconds = max(0.0, cfg_float(cfg, "display", "animation_seconds", 2))
    idle_frame_interval = max(0.2, cfg_float(cfg, "display", "idle_frame_interval_seconds", cfg_float(cfg, "display", "frame_interval_seconds", 1.0)))
    active_frame_interval = max(1 / 60, cfg_float(cfg, "display", "active_frame_interval_seconds", 1 / 30))
    refresh_seconds = max(60.0, cfg_float(cfg, "display", "refresh_minutes", 30) * 60)
    touch_enabled = cfg_bool(cfg, "touch", "enabled", True)
    drag_pixels_per_step = max(1, cfg_int(cfg, "touch", "drag_pixels_per_step", 52))
    back_debounce_seconds = max(0.0, cfg_float(cfg, "touch", "back_debounce_seconds", 0.35))

    dev, ep_out, ep_in = aic_time.open_device(aic_time.VID, aic_time.PID)
    params = aic_time.get_params(dev)
    aic_time.authenticate(ep_out, ep_in, verbose=verbose, chunk_size=chunk_size)
    if verbose:
        print(params)

    state = NewsState()
    stop_event = threading.Event()
    touch_actions: queue.Queue[TouchAction] = queue.Queue()
    worker = threading.Thread(
        target=refresh_worker,
        args=(cfg, state, refresh_seconds, verbose, stop_event),
        daemon=True,
    )
    touch_thread: threading.Thread | None = None
    if not once:
        worker.start()
        if touch_enabled:
            touch_thread = threading.Thread(
                target=touch_worker,
                args=(touch_actions, params.width, params.height, cfg, verbose, stop_event),
                daemon=True,
            )
            touch_thread.start()

    frame_id = 0
    view_start = 0
    previous_view_start = 0
    slide_started = time.monotonic()
    transition_started = slide_started - animation_seconds
    seen_article_signature: tuple[tuple[str, str], ...] = ()
    seen_article_urls: tuple[str, ...] = ()
    mode = "list"
    selected_index = 0
    detail_scroll = 0
    fast_until = time.monotonic()
    suppress_card_taps_until = 0.0

    try:
        while True:
            now = time.monotonic()
            articles = state.get_articles()
            if not articles:
                articles = [loading_article()]
            current_signature = article_signature(articles)
            current_urls = article_urls(articles)
            if current_signature != seen_article_signature:
                previous_urls = seen_article_urls
                seen_article_signature = current_signature
                seen_article_urls = current_urls
                new_urls = [url for url in current_urls if url not in previous_urls]
                if previous_urls and new_urls and mode == "list":
                    previous_view_start = view_start
                    view_start = next((idx for idx, article in enumerate(articles) if article.url in new_urls), view_start)
                    slide_started = now
                    transition_started = now
                    fast_until = now + max(0.5, animation_seconds)
                    if verbose:
                        print(f"news list updated: {len(new_urls)} new article(s), start {view_start + 1}")
                elif not previous_urls:
                    view_start = 0
                    previous_view_start = 0
                    slide_started = now
                    transition_started = now - animation_seconds
                    fast_until = now + 0.5
                if selected_index >= len(articles):
                    selected_index = 0
                    detail_scroll = 0
            if view_start >= len(articles):
                view_start = 0
                previous_view_start = 0
                slide_started = now
            if selected_index >= len(articles):
                selected_index = 0
                detail_scroll = 0

            while True:
                try:
                    action = touch_actions.get_nowait()
                except queue.Empty:
                    break

                if action.kind == "double_tap":
                    if mode != "list":
                        mode = "list"
                    else:
                        previous_view_start = view_start
                        view_start = scroll_index(view_start, 1, len(articles))
                    slide_started = now
                    transition_started = now if previous_view_start != view_start else now - animation_seconds
                    fast_until = now + 0.8
                    if verbose:
                        print(f"touch double_tap: mode={mode}, start={view_start + 1}")
                    continue

                if action.kind == "drag":
                    steps = max(1, round(abs(action.dy) / drag_pixels_per_step))
                    if abs(action.dy) >= abs(action.dx):
                        delta = steps if action.dy < 0 else -steps
                    else:
                        delta = steps if action.dx < 0 else -steps
                    if mode == "list":
                        view_start = scroll_index(view_start, delta, len(articles))
                        previous_view_start = view_start
                    elif mode == "detail":
                        max_detail_scroll = detail_max_scroll(articles[selected_index], params.width, params.height)
                        detail_scroll = max(0, min(max_detail_scroll, detail_scroll - action.dy))
                    if mode == "qr":
                        mode = "list"
                        detail_scroll = 0
                    slide_started = now
                    transition_started = now - animation_seconds
                    fast_until = now + 0.8
                    if verbose:
                        print(f"touch drag: delta={delta}, mode={mode}, start={view_start + 1}, selected={selected_index + 1}")
                    continue

                if action.kind != "tap":
                    continue

                if mode == "list":
                    if now < suppress_card_taps_until:
                        if verbose:
                            print("touch tap: suppressed after back")
                        continue
                    slot = hit_news_card(action.x, action.y, params.width, params.height, visible_count)
                    if slot is not None:
                        selected_index = (view_start + slot) % len(articles)
                        detail_scroll = 0
                        mode = "detail"
                        fast_until = now + 0.8
                        if verbose:
                            print(f"touch tap: open detail {selected_index + 1}/{len(articles)}")
                elif mode == "detail":
                    button = hit_button(action.x, action.y, button_rects(params.width, params.height))
                    if button == "back":
                        mode = "list"
                        detail_scroll = 0
                        suppress_card_taps_until = now + back_debounce_seconds
                        fast_until = now + 0.8
                    elif button == "qr":
                        mode = "qr"
                        fast_until = now + 0.8
                    if verbose and button:
                        print(f"touch tap: button={button}, mode={mode}")
                elif mode == "qr":
                    button = hit_button(action.x, action.y, button_rects(params.width, params.height))
                    if button == "back":
                        mode = "detail"
                        fast_until = now + 0.8
                    if verbose and button:
                        print(f"touch tap: button={button}, mode={mode}")

            if animation_seconds > 0:
                transition_progress = min(1.0, (now - transition_started) / animation_seconds)
            else:
                transition_progress = 1.0
            active_render = transition_progress < 1.0 or now < fast_until
            if mode == "detail":
                max_detail_scroll = detail_max_scroll(articles[selected_index], params.width, params.height)
                detail_scroll = max(0, min(detail_scroll, max_detail_scroll))
                img = render_detail(articles[selected_index], params.width, params.height, detail_scroll)
            elif mode == "qr":
                img = render_qr(articles[selected_index], params.width, params.height)
            else:
                img = render_news_board(
                    articles,
                    params.width,
                    params.height,
                    view_start,
                    previous_view_start,
                    transition_progress,
                    visible_count,
                )
            jpeg = image_to_jpeg(img, quality, subsampling)
            aic_time.send_jpeg_frame(ep_out, jpeg, frame_id, chunk_size)
            if verbose:
                print(
                    f"sent info frame {frame_id}: mode={mode}, news {min(visible_count, len(articles))}/{len(articles)}, "
                    f"start {view_start + 1}, selected {selected_index + 1}, {len(jpeg)} bytes"
                )
            frame_id += 1

            if once:
                break
            time.sleep(active_frame_interval if active_render else idle_frame_interval)
    except KeyboardInterrupt:
        if verbose:
            print("stopped")
    finally:
        stop_event.set()
        if touch_thread is not None:
            touch_thread.join(timeout=1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="info_screen.ini")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    run(args.config, args.once)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
