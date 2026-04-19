"""Tests for AniList anime detection heuristic."""

from jellyfiler.anilist import looks_like_anime


def test_detects_subgroup_prefix():
    assert looks_like_anime("[HorribleSubs] Steins;Gate - 01 [720p].mkv")


def test_detects_erai_raws():
    assert looks_like_anime("[Erai-raws] Vinland Saga S2 - 24 [1080p].mkv")


def test_detects_bd_source():
    assert looks_like_anime("Fullmetal.Alchemist.Brotherhood.BDRip.1080p.mkv")


def test_detects_ova():
    assert looks_like_anime("Evangelion.OVA.mkv")


def test_normal_show_not_anime():
    assert not looks_like_anime("Breaking.Bad.S01E01.1080p.BluRay.x264.mkv")


def test_normal_movie_not_anime():
    assert not looks_like_anime("Blade.Runner.2049.2017.2160p.UHD.BluRay.mkv")


def test_futurama_not_anime():
    assert not looks_like_anime("Futurama.S12E03.1080p.x265-ELiTE.mkv")
