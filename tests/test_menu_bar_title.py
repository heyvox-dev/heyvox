"""Tests for heyvox.hud.menu_bar_title — pure title/tooltip composition.

Phase 14 / SPEC R4 / D-12.

Truncation algorithm (Q4 / RESEARCH.md):
  * len(name) <= max_len               -> return name unchanged
  * first_word <= max_len              -> return first_word (word-boundary preference,
                                          even if subsequent words could also fit)
  * first_word > max_len               -> hard-cutoff: name[:max_len-1] + "…"
  * empty                              -> "None"
"""
import pytest

from heyvox.hud.menu_bar_title import truncate_mic, format_menu_bar_title


class TestTruncateMic:
    """Truncation helper for menu-bar title (D-12, 8-10 char budget)."""

    @pytest.mark.parametrize("name,expected", [
        # Fits within max_len — no truncation
        ("Evolve2 75", "Evolve2 75"),       # 10 chars, fits exactly
        ("Built-in", "Built-in"),           # 8 chars, fits
        ("Tony", "Tony"),                   # short single word

        # Word-boundary preference — first word ≤ max_len
        ("Evolve2 75 UC", "Evolve2"),       # 13 chars > 10; "Evolve2" is 7 ≤ 10
        ("AirPods Pro", "AirPods"),         # 11 chars > 10; "AirPods" is 7 ≤ 10
        ("AirPods Pro Max", "AirPods"),     # 15 chars > 10; "AirPods" is 7 ≤ 10
        ("Tony AirPods Pro", "Tony"),       # word-boundary even when more would fit

        # Hard-cutoff branch — single first word exceeds max_len
        ("BlackShockProX2", "BlackShoc…"),  # 15-char token > 10 → name[:9] + "…"
        ("Steelseries_Arctis_Nova_Pro", "Steelseri…"),  # underscore-joined → hard cutoff

        # Empty → "None"
        ("", "None"),
    ])
    def test_truncation_examples(self, name, expected):
        assert truncate_mic(name) == expected

    def test_custom_max_len(self):
        # max_len=6, first_word "Evolve2" (7 chars) > 6, so hard-cutoff branch:
        # name[: max_len - 1] + "…" = "Evolve2 75 UC"[:5] + "…" = "Evolv…"
        assert truncate_mic("Evolve2 75 UC", max_len=6) == "Evolv…"

    def test_custom_max_len_word_boundary(self):
        # max_len=8, first_word "Evolve2" (7 chars) ≤ 8 → word-boundary returns "Evolve2"
        assert truncate_mic("Evolve2 75 UC", max_len=8) == "Evolve2"


class TestFormatMenuBarTitle:
    """format_menu_bar_title returns dict of title + tooltip + flags."""

    def test_idle_shows_mic_name(self):
        out = format_menu_bar_title(state="idle", friendly_mic="Evolve2 75")
        assert "Evolve2 75" in out["title"]
        assert out["tooltip"] == "Mic: Evolve2 75"
        assert out["use_brand_icon"] is True
        assert out["mute_icon"] is False

    def test_listening_overrides_with_label(self):
        out = format_menu_bar_title(state="listening", friendly_mic="Evolve2 75")
        assert "Recording" in out["title"]
        assert out["tooltip"] == "Mic: Evolve2 75"  # tooltip stays
        assert out["use_brand_icon"] is False

    def test_mic_warning_overrides_state(self):
        out = format_menu_bar_title(
            state="idle", friendly_mic="Evolve2 75", mic_warning="silent mic",
        )
        assert "silent mic" in out["title"]

    def test_held_count_appended(self):
        out = format_menu_bar_title(
            state="idle", friendly_mic="Built-in", held_count=3,
        )
        assert "3" in out["title"]

    def test_speaker_muted_appends_suffix(self):
        out = format_menu_bar_title(
            state="idle", friendly_mic="Built-in", speaker_muted=True,
        )
        assert "\U0001f507" in out["title"]

    def test_mic_muted_sets_mute_icon(self):
        out = format_menu_bar_title(
            state="idle", friendly_mic="Built-in", is_mic_muted=True,
        )
        assert out["mute_icon"] is True
        assert "(muted)" in out["tooltip"]

    def test_empty_mic_shows_none_in_tooltip(self):
        out = format_menu_bar_title(state="idle", friendly_mic="")
        assert out["tooltip"] == "Mic: None"

    def test_crashed_overrides(self):
        out = format_menu_bar_title(
            state="idle", friendly_mic="Built-in", crashed=["kokoro"],
        )
        assert "crashed" in out["title"]
