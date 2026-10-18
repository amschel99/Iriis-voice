import pytest

from iriis.tts.text_normalize import extract_urls, normalize, say_number


@pytest.mark.parametrize("src,expected", [
    ("KSh 1,500", "fifteen hundred shillings"),
    ("It costs KSh 250.", "It costs two hundred and fifty shillings."),
    ("Ksh. 1,000,000", "one million shillings"),
    ("KES 3.50", "three shillings and fifty cents"),
    ("500 bob", "five hundred shillings"),
    ("$20", "twenty dollars"),
    ("KSh 2.5m", "two million five hundred thousand shillings"),
    ("at 14:30", "at fourteen thirty"),
    ("at 9:05 am", "at nine oh five A M"),
    ("at 7pm", "at seven P M"),
    ("at 10:00", "at ten o'clock"),
    ("50%", "fifty percent"),
    ("the 3rd time", "the third time"),
    ("in 2026", "in twenty twenty-six"),
    ("3.14", "three point one four"),
    ("Dr. Otieno", "Doctor Otieno"),
    ("16 GB RAM", "sixteen gigabytes ram"),
    ("-5 degrees", "minus five degrees"),
])
def test_normalize(src, expected):
    assert normalize(src) == expected


def test_say_number_hundreds():
    assert say_number(1500) == "fifteen hundred"
    assert say_number(2000) == "two thousand"
    assert say_number(1234) == "one thousand two hundred and thirty-four"


def test_markdown_is_stripped():
    out = normalize("**Hello** there!\n- first item\n- second item\n# Heading")
    assert "*" not in out and "#" not in out and "-" not in out
    assert "Hello there!" in out


def test_code_and_urls_are_not_read():
    out = normalize("Run this:\n```\nrm -rf x\n```\nSee https://example.com/page for more")
    assert "rm" not in out and "https" not in out and "example" not in out
    assert "on screen" in out
    text, urls = extract_urls("Visit www.nation.africa today")
    assert urls == ["www.nation.africa"] and "on screen" in text


def test_shouting_is_spoken_as_words_not_letters():
    from iriis.tts.text_normalize import calm_shouting
    assert calm_shouting("BRO. SIT THE FUCK DOWN.") == "bro. sit the fuck down."
    assert calm_shouting("ONE. FUCKING. PERSON.") == "one. fucking. person."
    assert calm_shouting("I AM THE AI!") == "I am the AI!"
    assert calm_shouting("Check the CPU and RAM") == "Check the CPU and RAM"
    assert calm_shouting("Take the SGR to Mombasa") == "Take the SGR to Mombasa"
    assert calm_shouting("WHAT THE FUCK, AMSCHEL?!") == "what the fuck, amschel?!"
