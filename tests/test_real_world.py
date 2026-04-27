"""Regression tests using real filename and folder patterns from the SMB library.

These cases come from /mnt/smbshare/shared_evreyone/series/ and movies/. They
document what guessit + our wrappers handle correctly so messy real-world
patterns don't silently regress.

Patterns covered (sampled from a 226-folder series library + 121 movies):
  * Year-range folders: "AAAHH (1994-1997)", "Avatar (2005-2014)"
  * All-caps with "Complete TV Series" tail
  * Anime subgroup brackets: [HorribleSubs], [Judas], [Coalgirls], [Vodes]
  * Dual-numbering: "American Dad! S07-S13 (S08-S15)"
  * Three-digit episodes: S01E001 (Hunter x Hunter)
  * Spaced SxxExx: "S01 E01" with space (AAAHH, Avatar)
  * Episode-with-segment-title: "S01E01 - Title A - Title B"
  * Anime numeric-only episodes: "[HorribleSubs] Show - 01 [1080p]"
  * Russian/Cyrillic titles
  * Dual-segment episodes with letter suffix: "S01E01a", "S01E01b" (Hey Arnold RU rip)
  * Movie AKA titles: "Druk.AKA.Another.Round"
  * Movies with semicolons: "Karate Kid; Legends"
  * YIFY/YTS bracketed releases: "[2160p] [4K] [BluRay] [7.1] [YTS.MX]"
"""

from pathlib import Path

from jellyfiler.guesser import guess
from jellyfiler.junk import is_junk
from jellyfiler.models import MediaType

# ---------------------------------------------------------------------------
# Series — patterns that currently work end-to-end
# ---------------------------------------------------------------------------


def test_hey_arnold_dash_separated_dual_segment_title():
    """Hey Arnold S1: 'S01E01 - Title A - Title B' — handled as a single episode."""
    g = guess(
        Path("Hey Arnold! Season 1 1080p")
        / "Hey Arnold! S01E01 - Downtown As Fruits - Eugene's Bike (1080p).mp4"
    )
    assert g.media_type == MediaType.EPISODE
    assert g.title == "Hey Arnold!"
    assert g.season == 1
    assert g.episode == 1


def test_aaaaah_real_monsters_spaced_sxxexx():
    """'S01 E01' (with space) parsed correctly."""
    g = guess(
        Path("Season 1 (1994-95)")
        / "AAAHH, Real Monsters - S01 E01 - The Switching Hour (480p - DVDRip).mp4"
    )
    assert g.media_type == MediaType.EPISODE
    assert g.season == 1
    assert g.episode == 1


def test_hunter_x_hunter_three_digit_episode():
    """S01E001 (zero-padded to 3 digits) → episode=1, not 001 dropped."""
    g = guess(
        Path("[Judas] Hunter x Hunter (2011) - Episodes 001-148")
        / "[Judas] Hunter x Hunter (2011) - S01E001.mkv"
    )
    assert g.media_type == MediaType.EPISODE
    assert g.title == "Hunter x Hunter"
    assert g.year == 2011
    assert g.season == 1
    assert g.episode == 1


def test_american_dad_dual_season_numbering_picks_first():
    """'S07E01 (S08E01)' → guessit returns season=[7,8], _extract picks first → 7 (Fox/TMDB ordering)."""
    g = guess(
        Path("American Dad! S07-S13 (S08-S15) 720p WEB-DL")
        / "Season 7 (Season 8)"
        / "American Dad! S07E01 (S08E01) Hot Water (2x2, Filiza Studio).mkv"
    )
    assert g.media_type == MediaType.EPISODE
    assert g.title == "American Dad!"
    assert g.season == 7
    assert g.episode == 1


def test_avatar_spaced_sxxexx_with_release_group_in_title():
    """Avatar (The Last Airbender) — guessit sees the parenthetical as a release group;
    title comes through as just 'Avatar'. Substring matching on TMDB recovers the show."""
    g = guess(
        Path("AVATAR (2005-2014) - 2010 Movie, The Last Airbender, Legend of Korra - 720p x264")
        / "1. 2005-08 Series - The Last Airbender (720p HDTV)"
        / "Book 2 - Earth (2006)"
        / "Avatar (The Last Airbender) - S02 E22 - The Cave of Two Lovers (720p HDTV).mp4"
    )
    assert g.media_type == MediaType.EPISODE
    assert g.title == "Avatar"  # release_group ate "The Last Airbender"
    assert g.season == 2
    assert g.episode == 22


def test_anime_subgroup_episode_only_no_season():
    """[HorribleSubs] Show - 01 — only episode number, season missing.
    Planner defaults season to 1 in this case."""
    g = guess(Path("Akame ga Kill!") / "[HorribleSubs] Akame ga Kill! - 01 [1080p].mkv")
    assert g.media_type == MediaType.EPISODE
    assert g.title == "Akame ga Kill!"
    assert g.episode == 1
    assert g.season is None  # planner will default to 1


def test_dot_separated_release_with_release_group_suffix():
    """Avatar.The.Last.Airbender.S01.1080p.Bluray.x265-HiQVE — clean dot-separated parse."""
    g = guess(Path("Avatar.The.Last.Airbender.S01.1080p.Bluray.x265-HiQVE/E01.mkv"))
    # Folder pack — title comes from parent dir
    assert g.title == "Avatar The Last Airbender"
    assert g.season == 1


def test_chucky_loose_release_group_brackets():
    """'h264-EDITH[TGx]' — folder is the release name, the .mkv inside repeats it."""
    g = guess(
        Path("Chucky.S03E01.1080p.WEB.h264-EDITH[TGx]") / "Chucky.S03E01.1080p.WEB.h264-EDITH.mkv"
    )
    assert g.title == "Chucky"
    assert g.season == 3
    assert g.episode == 1


# ---------------------------------------------------------------------------
# Movies — real-world patterns. Tests use the actual filename pattern (either
# top-level file or folder with the same release name).
# ---------------------------------------------------------------------------


def test_movie_yify_format():
    """Alice.in.Wonderland.2010.1080p.BluRay.x264.YIFY (folder + same-named file)."""
    g = guess(
        Path("Alice.in.Wonderland.2010.1080p.BluRay.x264.YIFY")
        / "Alice.in.Wonderland.2010.1080p.BluRay.x264.YIFY.mkv"
    )
    assert g.media_type == MediaType.MOVIE
    assert g.title == "Alice in Wonderland"
    assert g.year == 2010


def test_movie_aka_keeps_full_title():
    """Druk.AKA.Another.Round — guessit keeps 'Druk AKA Another Round'.
    TMDB best_match's substring tier handles 'Druk' or 'Another Round' suffix matching."""
    g = guess(Path("Druk.AKA.Another.Round.2020.1080p.BluRay.x265.mkv"))
    assert g.media_type == MediaType.MOVIE
    assert "Druk" in g.title
    assert "Another Round" in g.title
    assert g.year == 2020


def test_movie_with_semicolon_in_title_preserved():
    """'Karate Kid; Legends (2025)' — punctuation kept verbatim."""
    g = guess(Path("Karate Kid; Legends (2025) 2160p.mkv"))
    assert g.media_type == MediaType.MOVIE
    assert g.title == "Karate Kid; Legends"
    assert g.year == 2025


def test_movie_yts_bracket_format():
    """'1917 (2019) [2160p] [4K] [BluRay] [7.1] [YTS.MX]' as a top-level file."""
    g = guess(Path("1917 (2019) [2160p] [4K] [BluRay] [7.1] [YTS.MX].mkv"))
    # The numeric-only title '1917' can confuse guessit; year is what matters here.
    assert g.year == 2019


def test_movie_blade_runner_2049_long_release():
    """Long release name with REMUX, codec, audio, group — title still extracted cleanly."""
    name = "Blade.Runner.2049.2017.Hybrid.2160p.UHD.Blu-ray.Remux.HEVC.DV.HDR.TrueHD.7.1.Atmos-HDT"
    g = guess(Path(name) / f"{name}.mkv")
    assert "Blade Runner 2049" in g.title
    assert g.year == 2017


def test_movie_coco_year_only():
    """Coco.2017 — short title, year surfaces."""
    g = guess(
        Path("Coco.2017.2160p.UHD.BluRay.x265-WhiteRhino")
        / "Coco.2017.2160p.UHD.BluRay.x265-WhiteRhino.mkv"
    )
    assert g.title == "Coco"
    assert g.year == 2017


def test_movie_dash_separated_subtitle_kept():
    """The Punisher - War Zone (2008) — guessit splits into title+alternative_title.
    Without combining them, TMDB search for 'The Punisher' year=2008 falls to any-year
    exact match and picks the wrong 2004 / 1989 movie. We combine as 'Title: Subtitle'
    so TMDB finds the canonical 'Punisher: War Zone' (2008)."""
    g = guess(
        Path("The PUNISHER - Complete Movie Trilogy - 720p BrRip x264")
        / "03. The Punisher - War Zone (2008) - 720p BrRip"
        / "The Punisher - War Zone (2008 - 720p).mp4"
    )
    assert g.media_type == MediaType.MOVIE
    assert g.title == "The Punisher: War Zone"
    assert g.year == 2008


def test_movie_dash_subtitle_avengers_endgame():
    """Avengers - Endgame (2019) — generic dash-subtitle pattern, not specific to Punisher."""
    g = guess(Path("Avengers - Endgame (2019) 1080p.mkv"))
    assert g.media_type == MediaType.MOVIE
    assert g.title == "Avengers: Endgame"
    assert g.year == 2019


def test_punisher_first_two_movies_unaffected():
    """The Punisher (1989) and (2004) have no subtitle — must remain just 'The Punisher'."""
    g_1989 = guess(
        Path("The PUNISHER - Complete Movie Trilogy - 720p BrRip x264")
        / "01. The Punisher (1989) - 720p BrRip"
        / "The Punisher (1989 - 720p).mp4"
    )
    assert g_1989.title == "The Punisher"
    assert g_1989.year == 1989

    g_2004 = guess(
        Path("The PUNISHER - Complete Movie Trilogy - 720p BrRip x264")
        / "02. The Punisher (2004) - 720p BrRip"
        / "The Punisher (2004 - 720p).mp4"
    )
    assert g_2004.title == "The Punisher"
    assert g_2004.year == 2004


# ---------------------------------------------------------------------------
# OVAs — routed to Season 00 (Jellyfin's Specials) so they don't collide
# with main-series numbering. Real filenames sampled from the SMB library.
# ---------------------------------------------------------------------------


def test_ova_with_explicit_s00_already_correct():
    """[ShadyCrab] FMA Brotherhood OVA - S00E01 — already tagged S00, untouched."""
    g = guess(
        Path("Fullmetal Alchemist Brotherhood + OVAs + Specials [BD 1080p Hi10 FLAC] [Dual Audio]")
        / "[ShadyCrab] Fullmetal Alchemist Brotherhood OVA - S00E01 [BD][1080p][Hi10][Dual][68CE5759].mkv"
    )
    assert g.media_type == MediaType.EPISODE
    assert g.season == 0
    assert g.episode == 1


def test_one_punch_man_s00_ova():
    """One Punch Man S00E12 OVA — Season 00 folder + explicit S00E."""
    g = guess(
        Path("[Breeze] One Punch Man S01 S02 [1080p BD AV1][dual audio]")
        / "Season 00"
        / "[Breeze] One Punch Man S00E12 OVA [1080p BD AV1][dual audio].mkv"
    )
    assert g.season == 0
    assert g.episode == 12


def test_darker_than_black_ova_routed_to_season_00():
    """'Darker than Black ... OVA 01' — only episode number + OVA token; we force season=0."""
    g = guess(
        Path("Darker Than Black Complete Series [BluRay]")
        / "Season 02 + Ovas"
        / "Darker than Black Gemini of the meteor OVA 01.mkv"
    )
    assert g.media_type == MediaType.EPISODE
    assert g.season == 0  # forced by OVA detection
    assert g.episode == 1


def test_ova_in_movie_looking_release_forced_to_episode():
    """Black.Lagoon.OVA.1080p... — guessit calls this a movie; we force EPISODE + S00."""
    name = "Black.Lagoon.OVA.1080p.Blu-Ray.10-Bit.Dual-Audio.TrueHD.x265-iAHD"
    g = guess(Path("Black Lagoon (complete)") / name / f"{name}.mkv")
    assert g.media_type == MediaType.EPISODE
    assert g.season == 0


def test_ova_via_parent_directory():
    """A file inside an 'OVA' directory inherits season=0 even without OVA in the filename."""
    g = guess(
        Path("[Anime Time] Attack On Titan (Complete Series) (S01-S04+OVA)")
        / "[Anime Time] Attack On Titan OAD-OVA"
        / "[Anime Time] Attack On Titan - 03.mkv"
    )
    assert g.media_type == MediaType.EPISODE
    assert g.season == 0


def test_dungeon_ova_no_episode_number():
    """[sam] Dungeon ni Deai - OVA — no episode number, but OVA detected → S00, episode prompt later."""
    g = guess(
        Path("Danmachi (is it wrong to pick up girls in a dungeon)(Season 1 to 5 + movie)")
        / "[sam] Dungeon ni Deai wo Motomeru no wa Machigatteiru Darou ka [BD 1080p FLAC]"
        / "[sam] Dungeon ni Deai - OVA [BD 1080p FLAC] [899E43F1].mkv"
    )
    assert g.media_type == MediaType.EPISODE
    assert g.season == 0
    # episode is None — the bare-episode interactive prompt handles it


def test_non_ova_episode_keeps_normal_season():
    """Sanity check: a regular S01E01 episode is NOT forced to season 0."""
    g = guess(Path("Futurama Season 12") / "Futurama.S12E01.1080p.x265-ELiTE.mkv")
    assert g.season == 12  # not 0 — OVA detection didn't fire


# ---------------------------------------------------------------------------
# Junk detection on real filenames
# ---------------------------------------------------------------------------


def test_real_ncop_anime_opening_is_junk(tmp_path):
    f = tmp_path / "[Coalgirls]_Ao_no_Exorcist_NCOP_(1920x1080_Blu-Ray_FLAC)_[E92D4C42].mkv"
    f.touch()
    assert is_junk(f)


def test_real_creditless_op_is_junk(tmp_path):
    f = tmp_path / "Fate_Stay_Night_Creditless_OP1_[720p,BluRay,x264]_-_THORA.mkv"
    f.touch()
    assert is_junk(f)


def test_real_planetes_episode_with_endings_in_title_is_NOT_junk(tmp_path):
    """[OZC]Planetes E19 'Endings Are Always...' — title contains 'Endings' but it's an episode."""
    f = tmp_path / "[OZC]Planetes E19 'Endings Are Always...'.mkv"
    f.touch()
    assert not is_junk(f)


def test_real_hey_arnold_operation_ruthless_not_junk(tmp_path):
    """Episode title contains 'Operation' (starts with OP) — must not be flagged as opening."""
    f = tmp_path / "S01E07a Operation Ruthless.mkv"
    f.touch()
    assert not is_junk(f)


# ---------------------------------------------------------------------------
# Split-episode handling — Hey Arnold RU rip uses S01E01a/b for 11-min halves.
# We strip the letter so guessit parses, then preserve it in the destination
# filename so the two halves don't collide on S01E01.mkv.
# ---------------------------------------------------------------------------


def test_split_episode_a_suffix_parsed_as_episode():
    g = guess(Path("S01E01a Downtown as Fruits.mkv"))
    assert g.media_type == MediaType.EPISODE
    assert g.season == 1
    assert g.episode == 1
    assert g.segment == "a"


def test_split_episode_b_suffix_parsed_as_episode():
    g = guess(Path("S01E01b Eugene's Bike.mkv"))
    assert g.media_type == MediaType.EPISODE
    assert g.season == 1
    assert g.episode == 1
    assert g.segment == "b"


def test_split_episode_no_suffix_is_unaffected():
    """Plain S01E11 (no letter) → segment is None."""
    g = guess(Path("S01E11 Arnold's Christmas.mkv"))
    assert g.media_type == MediaType.EPISODE
    assert g.episode == 11
    assert g.segment is None


def test_split_episode_destination_keeps_letter():
    """S01E01a/b → S01E01a.mkv / S01E01b.mkv so the two halves don't collide."""
    from jellyfiler.models import GuessedMedia, TmdbMatch
    from jellyfiler.planner import _episode_destination

    match = TmdbMatch(tmdb_id=1, title="Hey Arnold!", year=1996, media_type=MediaType.EPISODE)
    guessed_a = GuessedMedia(
        source_path=Path("S01E01a.mkv"),
        media_type=MediaType.EPISODE,
        title="Hey Arnold!",
        season=1,
        episode=1,
        segment="a",
    )
    guessed_b = GuessedMedia(
        source_path=Path("S01E01b.mkv"),
        media_type=MediaType.EPISODE,
        title="Hey Arnold!",
        season=1,
        episode=1,
        segment="b",
    )
    dest_a = _episode_destination(Path("/dest"), match, guessed_a, Path("S01E01a.mkv"))
    dest_b = _episode_destination(Path("/dest"), match, guessed_b, Path("S01E01b.mkv"))
    assert dest_a == Path("/dest/Hey Arnold!/Season 01/S01E01a.mkv")
    assert dest_b == Path("/dest/Hey Arnold!/Season 01/S01E01b.mkv")
    assert dest_a != dest_b  # critical: they don't collide


# ---------------------------------------------------------------------------
# Year ranges in parent dir names — "Season 1 (1994-95)" airing range, not
# the show's premiere year. We drop year-from-parent when a range is present.
# ---------------------------------------------------------------------------


def test_year_range_in_season_folder_is_not_treated_as_year():
    """'Season 1 (1994-95)' must NOT leak year=1994 into GuessedMedia."""
    g = guess(Path("Season 1 (1994-95)") / "episode.mkv")
    assert g.year is None
    assert g.season == 1


def test_year_range_in_avatar_umbrella_folder_suppressed():
    """Top-level 'AVATAR (2005-2014)' year range likewise suppressed."""
    g = guess(
        Path("AVATAR (2005-2014) - 2010 Movie, The Last Airbender, Legend of Korra - 720p x264")
        / "Avatar.S01E01.mkv"
    )
    # Year shouldn't be 2005 from the umbrella folder's date range.
    assert g.year is None


def test_book_with_year_range_suppressed():
    """'Book 3 - Fire (2007-08)' season folder shouldn't pull year from the range either."""
    g = guess(Path("Book 3 - Fire (2007-08)") / "Avatar.S03E01.mkv")
    assert g.year is None


def test_single_year_in_parent_dir_still_works():
    """Sanity: a real single-year movie folder still backfills year (regression guard)."""
    g = guess(Path("Blade.Runner.2049.2017.UHD") / "Blade.Runner.2049.mkv")
    assert g.year == 2017


# ---------------------------------------------------------------------------
# Statistical sanity check — patterns we expect to dominate the library
# ---------------------------------------------------------------------------


def test_common_pattern_smoke():
    """Smoke test that a representative spread of real folder names parse without exception."""
    samples = [
        "AAAHH, REAL MONSTERS (1994-1997) - Complete ANIMATED TV Series, S01-S04 - 480p DVDRip x264",
        "Akame ga Kill!",
        "American Dad! S07-S13 (S08-S15) 720p WEB-DL",
        "Avatar.The.Last.Airbender.S01.1080p.Bluray.x265-HiQVE",
        "BATMAN Cartoons (1992-2015) - The FIVE Complete Animated Series - 480p-720p x264",
        "[HorribleSubs] Akame ga Kill! - 01 [1080p].mkv",
        "[Judas] Hunter x Hunter (2011) (Complete Series + Movies) [BD 1080p]",
        "Chucky.S03E01.1080p.WEB.h264-EDITH[TGx]",
        "Futurama.S12.1080p.x265-ELiTE",
        "Hey Arnold! Season 1 1080p",
    ]
    for s in samples:
        # Just verify guess() doesn't raise on any of these
        result = guess(Path(s) / "file.mkv")
        assert result is not None
