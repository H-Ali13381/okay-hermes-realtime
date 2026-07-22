from __future__ import annotations

from realtime_action_spike.runtime.tokens import LaunchTokenStore


class MutableClock:
    """Simple monotonic clock whose value can be advanced by tests."""

    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def test_issue_and_validation_and_consumption() -> None:
    clock = MutableClock()
    token_factory = iter(["tok-alpha", "tok-bravo"]).__next__
    store = LaunchTokenStore(monotonic_clock=clock, token_factory=token_factory)

    token = store.issue("local-session-01")
    assert token == "tok-alpha"

    assert store.validate(token) == "local-session-01"
    assert store.consume(token) == "local-session-01"
    assert store.consume(token) is None
    assert store.validate(token) is None


def test_tokens_are_ttl_bound_to_monotonic_time() -> None:
    clock = MutableClock()
    token_factory = iter(["tok-ttl"]).__next__
    store = LaunchTokenStore(
        token_ttl_seconds=15.0,
        monotonic_clock=clock,
        token_factory=token_factory,
    )

    token = store.issue("local-session-ttl")

    clock.advance(14.9)
    assert store.validate(token) == "local-session-ttl"

    clock.advance(0.2)
    assert store.validate(token) is None


def test_wrong_token_and_superseded_tokens_reject() -> None:
    clock = MutableClock()
    token_factory = iter(["tok-1", "tok-2"]).__next__
    store = LaunchTokenStore(monotonic_clock=clock, token_factory=token_factory)

    first_token = store.issue("local-session-01")
    second_token = store.issue("local-session-02")

    assert store.consume("not-a-token") is None
    assert store.validate("not-a-token") is None

    assert store.validate(first_token) is None
    assert store.consume(first_token) is None

    assert store.validate(second_token) == "local-session-02"
    assert store.consume(second_token) == "local-session-02"


def test_session_and_token_are_distinct_and_reproducible() -> None:
    clock = MutableClock()
    token_factory = iter(["token-001"]).__next__
    store = LaunchTokenStore(monotonic_clock=clock, token_factory=token_factory)

    token = store.issue("local-session-xyz")

    assert token != "local-session-xyz"

