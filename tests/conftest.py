from __future__ import annotations

import os
from html.parser import HTMLParser
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

import psycopg
import pytest
from fastapi.testclient import TestClient

BASE_DATABASE_URL = os.environ.get(
    "TEST_BASE_DATABASE_URL",
    "postgresql://fiyattakip:fiyattakip@localhost:5435/fiyattakip",
)
TEST_SCHEMA = "fiyattakip_test"


def _build_schema_url(base_url: str, schema: str) -> str:
    parts = urlsplit(base_url)
    query = urlencode({"options": f"-csearch_path={schema} -clock_timeout=5000"}, quote_via=quote)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


TEST_DATABASE_URL = _build_schema_url(BASE_DATABASE_URL, TEST_SCHEMA)


class _SimpleHTMLInspector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.jsonld_blocks: list[str] = []
        self._in_jsonld = False
        self._current_script: list[str] = []
        self.class_elements: dict[str, list[str]] = {}
        self._open_class_stack: list[tuple[str, list[str], list[str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        if tag.lower() == "script" and attr_map.get("type", "").strip().lower() == "application/ld+json":
            self._in_jsonld = True
            self._current_script = []
        classes = attr_map.get("class", "").split()
        if classes:
            self._open_class_stack.append((tag.lower(), classes, []))

    def handle_data(self, data: str) -> None:
        if self._in_jsonld:
            self._current_script.append(data)
        for _, _, buf in self._open_class_stack:
            buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._in_jsonld:
            self.jsonld_blocks.append("".join(self._current_script).strip())
            self._in_jsonld = False
            self._current_script = []
        for idx in range(len(self._open_class_stack) - 1, -1, -1):
            open_tag, classes, buf = self._open_class_stack[idx]
            if open_tag == tag.lower():
                text = "".join(buf).strip()
                for cls in classes:
                    self.class_elements.setdefault(cls, []).append(text)
                self._open_class_stack.pop(idx)
                break


def parse_html(html: str) -> _SimpleHTMLInspector:
    parser = _SimpleHTMLInspector()
    parser.feed(html)
    parser.close()
    return parser


@pytest.fixture(scope="session", autouse=True)
def isolated_test_schema() -> None:
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL

    with psycopg.connect(BASE_DATABASE_URL, autocommit=True) as admin_conn:
        with admin_conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE")
            cur.execute(f"CREATE SCHEMA {TEST_SCHEMA}")

    from app.db import apply_schema, get_connection

    with get_connection() as conn:
        apply_schema(conn)
        conn.commit()

    yield

    with psycopg.connect(BASE_DATABASE_URL, autocommit=True) as admin_conn:
        with admin_conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE")


@pytest.fixture(autouse=True)
def clean_seeded_db(isolated_test_schema: None) -> None:
    from app.reset import reset_demo

    reset_demo()


@pytest.fixture()
def db_conn():
    from app.db import get_connection

    with get_connection() as conn:
        yield conn


@pytest.fixture()
def client():
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client
