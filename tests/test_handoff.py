import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine import handoff


@pytest.fixture(autouse=True)
def clean():
    handoff._pending.clear()
    yield
    handoff._pending.clear()


def test_a_prompt_waits_until_the_player_taps():
    pending = handoff.open_prompt(character_id=1)
    answered = []

    def player():
        time.sleep(0.05)
        handoff.answer(pending.token)

    threading.Thread(target=player).start()
    answered.append(handoff.wait(pending, timeout=5))

    assert answered == [True]


def test_an_unanswered_prompt_gives_up_rather_than_freezing_the_table():
    """A player who walks away, closes the tab or loses signal must not leave
    everybody else waiting on a turn that will never finish."""
    pending = handoff.open_prompt(character_id=1)

    started = time.time()
    answered = handoff.wait(pending, timeout=0.1)

    assert answered is False
    assert time.time() - started < 2


def test_a_double_tap_only_counts_once():
    """Two taps must not roll two dice."""
    pending = handoff.open_prompt(character_id=1)

    assert handoff.answer(pending.token) is True
    assert handoff.answer(pending.token) is False


def test_an_unknown_token_is_refused():
    assert handoff.answer("not-a-real-token") is False
    assert handoff.answer(None) is False
    assert handoff.answer("") is False


def test_a_finished_prompt_is_forgotten():
    """Otherwise a long session accumulates every roll anyone ever made."""
    pending = handoff.open_prompt(character_id=1)
    handoff.answer(pending.token)
    handoff.wait(pending, timeout=1)

    assert handoff.outstanding() == 0
    assert handoff.answer(pending.token) is False


def test_a_timed_out_prompt_is_forgotten_too():
    pending = handoff.open_prompt(character_id=1)
    handoff.wait(pending, timeout=0.05)
    assert handoff.outstanding() == 0


def test_two_players_can_be_waited_on_independently():
    first = handoff.open_prompt(character_id=1)
    second = handoff.open_prompt(character_id=2)

    handoff.answer(second.token)

    assert handoff.wait(second, timeout=1) is True
    assert handoff.wait(first, timeout=0.05) is False


def test_cancelling_removes_it():
    pending = handoff.open_prompt(character_id=1)
    handoff.cancel(pending)
    assert handoff.outstanding() == 0
