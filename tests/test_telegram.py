from __future__ import annotations

import json
import logging

import httpx
import pytest

from app.telegram import Notifier, notifier_from_env

FAKE_TOKEN = "123456789:SECRET_TEST_TOKEN_DO_NOT_LEAK"
FAKE_CHAT_ID = "-10099887766"


def test_dry_run_makes_zero_network_calls() -> None:
    # Kabul: given Notifier with dry_run=True and a MockTransport,
    # expect send() returns 'dry_run' and invokes the transport zero times
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"ok": True})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        notifier = Notifier(
            token=FAKE_TOKEN,
            chat_id=FAKE_CHAT_ID,
            dry_run=True,
            client=client,
        )
        status = notifier.send("Test uyarısı")

    assert status == "dry_run"
    assert calls == 0


@pytest.mark.parametrize(
    ("token", "chat_id"),
    [(None, FAKE_CHAT_ID), ("", FAKE_CHAT_ID), ("   ", FAKE_CHAT_ID), (FAKE_TOKEN, None), (FAKE_TOKEN, "")],
)
def test_missing_token_or_chat_id_forces_dry_run(token: str | None, chat_id: str | None) -> None:
    # Kabul: given dry_run=False but token or chat_id is missing/blank,
    # expect Notifier forces dry_run=True, returns 'dry_run', and makes zero transport calls
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"ok": True})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        notifier = Notifier(token=token, chat_id=chat_id, dry_run=False, client=client)
        assert notifier.dry_run is True
        status = notifier.send("Fiyat düştü")

    assert status == "dry_run"
    assert calls == 0


def test_send_200_returns_sent_and_hides_token_from_logs_and_repr(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Kabul: given dry_run=False with valid token+chat_id and Telegram returning 200,
    # expect POST to api.telegram.org/bot<TOKEN>/sendMessage with chat_id+text JSON body,
    # status=='sent', and token never appears in return value, repr, or logs
    captured: list[tuple[str, str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        captured.append((request.method, str(request.url), body))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    with caplog.at_level(logging.DEBUG):
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            notifier = Notifier(
                token=FAKE_TOKEN,
                chat_id=FAKE_CHAT_ID,
                dry_run=False,
                client=client,
            )
            status = notifier.send("Filtre Kahve: 2499.90 -> 2300.00")

    assert status == "sent"
    assert len(captured) == 1
    method, url, body = captured[0]
    assert method == "POST"
    assert url == f"https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage"
    assert body == {"chat_id": FAKE_CHAT_ID, "text": "Filtre Kahve: 2499.90 -> 2300.00"}
    assert FAKE_TOKEN not in status
    assert FAKE_TOKEN not in repr(notifier)
    assert FAKE_TOKEN not in caplog.text


def test_429_retries_once_after_retry_after_sleep() -> None:
    # Kabul: given Telegram responds 429 with parameters.retry_after=3 then 200,
    # expect sleep(3) is called once, transport is called exactly 2 times, and send() returns 'sent'
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                429,
                json={"ok": False, "error_code": 429, "parameters": {"retry_after": 3}},
            )
        return httpx.Response(200, json={"ok": True})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        notifier = Notifier(
            token=FAKE_TOKEN,
            chat_id=FAKE_CHAT_ID,
            dry_run=False,
            client=client,
            sleep=sleeps.append,
        )
        status = notifier.send("Stok değişti")

    assert status == "sent"
    assert calls == 2
    assert sleeps == [3]


def test_double_429_returns_failed_after_two_calls() -> None:
    # Kabul: given Telegram responds 429 twice in a row,
    # expect exactly 2 calls (1 retry only), sleep called once, and send() returns 'failed'
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            429,
            json={"ok": False, "error_code": 429, "parameters": {"retry_after": 2}},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        notifier = Notifier(
            token=FAKE_TOKEN,
            chat_id=FAKE_CHAT_ID,
            dry_run=False,
            client=client,
            sleep=sleeps.append,
        )
        status = notifier.send("Fiyat düştü")

    assert status == "failed"
    assert calls == 2
    assert sleeps == [2]


def test_5xx_returns_failed_with_single_call_and_no_token_leak(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Kabul: given Telegram responds 500, expect send() makes exactly 1 call (no retry),
    # sleeps zero times, returns 'failed', and does not leak token into logs
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, text="Internal Server Error")

    with caplog.at_level(logging.DEBUG):
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            notifier = Notifier(
                token=FAKE_TOKEN,
                chat_id=FAKE_CHAT_ID,
                dry_run=False,
                client=client,
                sleep=sleeps.append,
            )
            status = notifier.send("Uyarı")

    assert status == "failed"
    assert calls == 1
    assert sleeps == []
    assert FAKE_TOKEN not in caplog.text


def test_network_error_returns_failed_with_single_call_and_no_token_leak(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Kabul: given a transport ConnectError containing the request URL (with token),
    # expect send() returns 'failed' after 1 call and never logs or leaks the token
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError(f"failed connecting to {request.url}", request=request)

    with caplog.at_level(logging.DEBUG):
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            notifier = Notifier(
                token=FAKE_TOKEN,
                chat_id=FAKE_CHAT_ID,
                dry_run=False,
                client=client,
            )
            status = notifier.send("Uyarı")

    assert status == "failed"
    assert calls == 1
    assert FAKE_TOKEN not in caplog.text


def test_notifier_from_env_defaults_and_opt_in_real_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Kabul: given unset TELEGRAM_* env vars, expect notifier_from_env() returns dry_run=True;
    # given TELEGRAM_DRY_RUN='false' with token+chat_id, expect dry_run=False;
    # given TELEGRAM_DRY_RUN='false' without token, expect dry_run=True
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_DRY_RUN", raising=False)

    default_notifier = notifier_from_env()
    assert default_notifier.dry_run is True

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", FAKE_CHAT_ID)
    assert notifier_from_env().dry_run is True

    monkeypatch.setenv("TELEGRAM_DRY_RUN", "false")
    live_notifier = notifier_from_env()
    assert live_notifier.dry_run is False

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert notifier_from_env().dry_run is True


def test_429_with_huge_retry_after_fails_without_sleeping_or_retrying() -> None:
    # Kabul: given 429 with retry_after=3600 (above cap), expect no sleep, 1 call, 'failed'
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            429, json={"ok": False, "parameters": {"retry_after": 3600}}
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        notifier = Notifier(
            token=FAKE_TOKEN,
            chat_id=FAKE_CHAT_ID,
            dry_run=False,
            client=client,
            sleep=sleeps.append,
        )
        status = notifier.send("x")

    assert status == "failed"
    assert calls == 1
    assert sleeps == []
