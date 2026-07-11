from resources.lib.episode_inventory import build_video_inventory


def test_requested_episode_beats_larger_sibling():
    rows = [
        ("/pack/Spider-Noir.S01E01.Step.Into.My.Office.mkv", 6_636_525_153),
        ("/pack/Spider-Noir.S01E05.Betrayal.mkv", 7_045_751_649),
    ]
    inventory = build_video_inventory(rows, requested=(1, 1))
    assert inventory.selected_path.endswith("S01E01.Step.Into.My.Office.mkv")
    assert inventory.selected_size == 6_636_525_153


def test_named_wrong_episode_fails_closed_but_no_context_keeps_largest():
    rows = [("/pack/Show.S01E05.mkv", 900), ("/pack/Show.S01E04.mkv", 800)]

    assert build_video_inventory(rows, requested=(1, 1)).selected_path is None
    assert build_video_inventory(rows).selected_path.endswith("S01E05.mkv")


def test_no_context_keeps_largest_even_when_it_is_auxiliary():
    rows = [("/pack/movie.mkv", 800), ("/pack/sample.mkv", 900)]

    inventory = build_video_inventory(rows)

    assert inventory.selected_path == "/pack/sample.mkv"
    assert inventory.selected_size == 900


def test_requested_context_excludes_larger_standalone_sample():
    inventory = build_video_inventory(
        [("/pack/video.mkv", 100), ("/pack/sample.mkv", 900)],
        requested=(1, 1),
    )

    assert inventory.selected_path == "/pack/video.mkv"
    assert inventory.selected_size == 100


def test_pack_requires_two_episodes_in_exactly_one_season():
    pack = build_video_inventory(
        [("/p/Show.S01E01.mkv", 100), ("/p/Show.S01E02.mkv", 90)],
        requested=(1, 1),
    )
    mixed = build_video_inventory(
        [("/p/Show.S01E01.mkv", 100), ("/p/Show.S02E01.mkv", 90)],
        requested=(1, 1),
    )

    assert (pack.pack_season, pack.episodes) == (1, (1, 2))
    assert mixed.pack_season is None


def test_sample_does_not_create_phantom_pack_episode():
    inventory = build_video_inventory(
        [("/p/Show.S01E01.mkv", 100), ("/p/Show.S01E02.sample.mkv", 5)],
        requested=(1, 1),
    )

    assert inventory.episodes == (1,)
    assert inventory.pack_season is None


def test_trailer_in_show_title_remains_main_pack_content():
    inventory = build_video_inventory(
        [
            ("/p/Trailer.Park.Boys.S01E01.mkv", 100),
            ("/p/Trailer.Park.Boys.S01E02.mkv", 200),
        ],
        requested=(1, 1),
    )

    assert inventory.selected_path == "/p/Trailer.Park.Boys.S01E01.mkv"
    assert (inventory.pack_season, inventory.episodes) == (1, (1, 2))


def test_trailer_park_boys_single_episode_beats_generic_video():
    inventory = build_video_inventory(
        [
            ("/p/Trailer.Park.Boys.S01E01.mkv", 100),
            ("/p/video.mkv", 900),
        ],
        requested=(1, 1),
    )

    assert inventory.selected_path == "/p/Trailer.Park.Boys.S01E01.mkv"


def test_trailer_park_boys_mixed_seasons_selects_exact_episode():
    inventory = build_video_inventory(
        [
            ("/p/Trailer.Park.Boys.S01E01.mkv", 100),
            ("/p/Trailer.Park.Boys.S02E01.mkv", 200),
        ],
        requested=(1, 1),
    )

    assert inventory.selected_path == "/p/Trailer.Park.Boys.S01E01.mkv"
    assert inventory.pack_season is None


def test_extras_in_show_title_remains_main_pack_content():
    inventory = build_video_inventory(
        [
            ("/downloads/Extras/Extras.S01E01.mkv", 100),
            ("/downloads/Extras/Extras.S01E02.mkv", 200),
        ],
        requested=(1, 1),
    )

    assert inventory.selected_path == "/downloads/Extras/Extras.S01E01.mkv"
    assert (inventory.pack_season, inventory.episodes) == (1, (1, 2))


def test_flat_extras_show_files_remain_main_pack_content():
    inventory = build_video_inventory(
        [
            ("/p/Extras.S01E01.mkv", 100),
            ("/p/Extras.S01E02.mkv", 200),
        ],
        requested=(1, 1),
    )

    assert inventory.selected_path == "/p/Extras.S01E01.mkv"
    assert (inventory.pack_season, inventory.episodes) == (1, (1, 2))


def test_extras_release_folder_preserves_leading_show_name():
    inventory = build_video_inventory(
        [
            ("/p/Extras.S01.1080p/Extras.S01E01.mkv", 100),
            ("/p/Other.S01E02.mkv", 200),
        ],
        requested=(1, 1),
    )

    assert inventory.selected_path == "/p/Extras.S01.1080p/Extras.S01E01.mkv"
    assert inventory.files[0].auxiliary is False


def test_auxiliary_suffix_exact_match_does_not_override_generic_main_video():
    inventory = build_video_inventory(
        [
            ("/p/video.mkv", 100),
            ("/p/Show.S01E01.FEATURETTE.mkv", 500),
        ],
        requested=(1, 1),
    )

    assert inventory.selected_path == "/p/video.mkv"
    assert inventory.has_tagged_files is False


def test_auxiliary_featurette_directory_excludes_episode_files():
    inventory = build_video_inventory(
        [
            ("/p/FEATURETTE/Show.S01E01.mkv", 100),
            ("/p/Show.S01E02.mkv", 200),
        ],
        requested=(1, 2),
    )

    assert inventory.selected_path == "/p/Show.S01E02.mkv"
    assert inventory.episodes == (2,)
    assert inventory.pack_season is None


def test_auxiliary_extras_directory_does_not_create_pack_episode():
    inventory = build_video_inventory(
        [
            ("/pack/EXTRAS/Show.S01E01.mkv", 100),
            ("/pack/Show.S01E02.mkv", 200),
        ],
        requested=(1, 2),
    )

    assert inventory.selected_path == "/pack/Show.S01E02.mkv"
    assert inventory.episodes == (2,)
    assert inventory.pack_season is None


def test_nested_auxiliary_ancestor_does_not_create_pack_episode():
    inventory = build_video_inventory(
        [
            ("/pack/EXTRAS/1080p/Show.S01E01.mkv", 100),
            ("/pack/Show.S01E02.mkv", 200),
        ],
        requested=(1, 2),
    )

    assert inventory.selected_path == "/pack/Show.S01E02.mkv"
    assert inventory.episodes == (2,)
    assert inventory.pack_season is None


def test_show_folder_exception_does_not_mask_different_auxiliary_ancestor():
    inventory = build_video_inventory(
        [
            ("/pack/FEATURETTE/Extras/Extras.S01E01.mkv", 100),
            ("/pack/Show.S01E02.mkv", 200),
        ],
        requested=(1, 2),
    )

    assert inventory.selected_path == "/pack/Show.S01E02.mkv"
    assert inventory.episodes == (2,)
    assert inventory.pack_season is None


def test_leading_auxiliary_marker_does_not_create_pack_episode():
    inventory = build_video_inventory(
        [
            ("/pack/sample.S01E01.mkv", 100),
            ("/pack/Show.S01E02.mkv", 200),
        ],
        requested=(1, 2),
    )

    assert inventory.selected_path == "/pack/Show.S01E02.mkv"
    assert inventory.episodes == (2,)
    assert inventory.pack_season is None


def test_sample_descriptive_prefix_remains_auxiliary():
    inventory = build_video_inventory(
        [
            ("/pack/sample.Show.Name.S01E01.mkv", 100),
            ("/pack/Show.S01E02.mkv", 200),
        ],
        requested=(1, 2),
    )

    assert inventory.selected_path == "/pack/Show.S01E02.mkv"
    assert inventory.episodes == (2,)
    assert inventory.pack_season is None


def test_featurette_descriptive_prefix_remains_auxiliary():
    inventory = build_video_inventory(
        [
            ("/pack/featurette.Behind.Scenes.S01E01.mkv", 100),
            ("/pack/Show.S01E02.mkv", 200),
        ],
        requested=(1, 2),
    )

    assert inventory.selected_path == "/pack/Show.S01E02.mkv"
    assert inventory.episodes == (2,)
    assert inventory.pack_season is None


def test_tagless_sample_descriptive_prefix_does_not_win_fallback():
    inventory = build_video_inventory(
        [
            ("/pack/video.mkv", 100),
            ("/pack/sample.Show.Name.mkv", 900),
        ],
        requested=(1, 1),
    )

    assert inventory.selected_path == "/pack/video.mkv"
    assert inventory.selected_size == 100


def test_tagless_featurette_descriptive_prefix_does_not_win_fallback():
    inventory = build_video_inventory(
        [
            ("/pack/video.mkv", 100),
            ("/pack/featurette.Behind.Scenes.mkv", 900),
        ],
        requested=(1, 1),
    )

    assert inventory.selected_path == "/pack/video.mkv"
    assert inventory.selected_size == 100


def _tagless_nonleading_auxiliary_inventory(marker):
    return build_video_inventory(
        [
            ("/pack/video.mkv", 100),
            ("/pack/Show.{}.mkv".format(marker), 900),
        ],
        requested=(1, 1),
    )


def test_tagless_nonleading_sample_does_not_win_fallback():
    inventory = _tagless_nonleading_auxiliary_inventory("Sample")

    assert inventory.selected_path == "/pack/video.mkv"


def test_tagless_nonleading_trailer_does_not_win_fallback():
    inventory = _tagless_nonleading_auxiliary_inventory("Trailer")

    assert inventory.selected_path == "/pack/video.mkv"


def test_tagless_nonleading_featurette_does_not_win_fallback():
    inventory = _tagless_nonleading_auxiliary_inventory("Featurette")

    assert inventory.selected_path == "/pack/video.mkv"


def test_tagless_nonleading_extras_does_not_win_fallback():
    inventory = _tagless_nonleading_auxiliary_inventory("Extras")

    assert inventory.selected_path == "/pack/video.mkv"


def test_single_leading_trailer_marker_remains_auxiliary():
    inventory = build_video_inventory(
        [
            ("/pack/trailer.S01E01.mkv", 100),
            ("/pack/Show.S01E02.mkv", 200),
        ],
        requested=(1, 2),
    )

    assert inventory.selected_path == "/pack/Show.S01E02.mkv"
    assert inventory.episodes == (2,)
    assert inventory.pack_season is None


def test_single_leading_extras_marker_remains_auxiliary():
    inventory = build_video_inventory(
        [
            ("/pack/extras.S01E01.mkv", 100),
            ("/pack/Show.S01E02.mkv", 200),
        ],
        requested=(1, 2),
    )

    assert inventory.selected_path == "/pack/Show.S01E02.mkv"
    assert inventory.episodes == (2,)
    assert inventory.pack_season is None


def test_leading_auxiliary_marker_strips_full_separator_run():
    inventory = build_video_inventory(
        [
            ("/pack/sample - S01E01.mkv", 100),
            ("/pack/Show.S01E02.mkv", 200),
        ],
        requested=(1, 2),
    )

    assert inventory.selected_path == "/pack/Show.S01E02.mkv"
    assert inventory.episodes == (2,)
    assert inventory.pack_season is None


def test_leading_auxiliary_marker_supports_multi_episode_notation():
    inventory = build_video_inventory(
        [
            ("/pack/sample.S01E01E02.mkv", 100),
            ("/pack/Show.S01E03.mkv", 200),
        ],
        requested=(1, 3),
    )

    assert inventory.selected_path == "/pack/Show.S01E03.mkv"
    assert inventory.episodes == (3,)
    assert inventory.pack_season is None


def test_separator_bounded_auxiliary_names_do_not_match_inside_words():
    inventory = build_video_inventory(
        [("/p/Show.S01E01.Extraspecial.mkv", 100)], requested=(1, 1)
    )

    assert inventory.selected_path == "/p/Show.S01E01.Extraspecial.mkv"
    assert inventory.files[0].auxiliary is False


def test_multi_episode_tag_contributes_each_pack_episode():
    inventory = build_video_inventory(
        [("/p/Show.S01E01E02.mkv", 100)], requested=(1, 2)
    )

    assert inventory.selected_path == "/p/Show.S01E01E02.mkv"
    assert (inventory.pack_season, inventory.episodes) == (1, (1, 2))


def test_invalid_and_negative_sizes_are_coerced_to_zero():
    inventory = build_video_inventory(
        [
            ("/p/Show.S01E01.mkv", -10),
            ("/p/Show.S01E02.mkv", "not-a-size"),
            ("/p/Show.S01E03.mkv", None),
        ],
        requested=(1, 1),
    )

    assert inventory.selected_size == 0
    assert tuple(item.size for item in inventory.files) == (0, 0, 0)
