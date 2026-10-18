"""She must not answer her own voice coming back through the mic, but must still hear the user."""
import time

from iriis.agent.echo import SelfEcho


def make(*sentences, ago=0.0):
    e = SelfEcho()
    for s in sentences:
        e.said(s, at=time.time() - ago)
    return e


def test_her_own_words_are_recognised():
    e = make("So Kamau buys a goat for a family celebration.", "Ties it to a post near the gate.")
    assert e.is_echo("so kamau buys a goat for a family")               # echo, partly transcribed
    assert e.is_echo("ties it to a post near the gate")
    assert e.is_echo("Kamau buys a goat for a family celebration ties it to a post")


def test_the_user_is_still_heard():
    e = make("Pentadecane melts around ten degrees, so it suits a ten degree pack.")
    assert not e.is_echo("what about five degrees")
    assert not e.is_echo("ok thanks")                                     # too short to judge: let it through
    assert not e.is_echo("can you explain how the pack keeps tomatoes fresh")


def test_old_words_are_not_echo():
    e = make("So Kamau buys a goat for a family celebration.", ago=60)    # a minute ago: you quoting her
    assert not e.is_echo("so kamau buys a goat for a family celebration")
