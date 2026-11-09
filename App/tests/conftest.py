"""Shared pytest configuration for the App test suite.

The browser tests serve the public frontend from ThreadingHTTPServer fixtures.
socketserver's default listen backlog is 5: a page that requests its modules,
fonts and data in parallel can overflow it when several browser workers run at
once (pytest-xdist). Windows then refuses the connection outright and Linux
drops the SYN and retries after about a second; either way a test fails or
waits for reasons unrelated to the code under test. A deeper backlog is a
test-only setting and changes no product behaviour.

Some browser tests assert wall-clock milliseconds (typing pace, bubble gaps,
transition length) or wait a few seconds for an animation to start. Under
pytest-xdist the other workers' Chrome processes take CPU from them, so they
fail for reasons unrelated to the code. CI therefore runs the browser suite in
two passes: SNOW_TIMING_TESTS=skip deselects them for the parallel pass and
SNOW_TIMING_TESTS=only keeps just them for a serial pass. Unset (the local
default) runs everything; the assertions themselves are unchanged.
"""
import os
import socketserver

import pytest

socketserver.TCPServer.request_queue_size = 128

TIMING_SENSITIVE = frozenset({
    "test_public_frontend_e2e.py::PublicFrontendE2ETests::test_v092_frontend_contracts_are_non_blocking_and_opt_out_feedback",
    "test_public_frontend_e2e.py::PublicFrontendE2ETests::test_presentation_queue_segments_text_but_uses_one_face_to_face_surface",
    "test_public_frontend_e2e.py::PublicFrontendE2ETests::test_long_face_to_face_reply_is_bounded_and_never_splits_graphemes",
    "test_public_frontend_reliability.py::PublicFrontendReliabilityTests::test_build_update_saves_draft_before_reload",
    "test_stage_presentation_timing.py::StagePresentationTimingTests::test_same_character_expression_crossfades_quickly_and_respects_reduced_motion",
    "test_public_frontend_latency_motion.py::PublicFrontendLatencyMotionTests::test_committed_body_precedes_completion_and_retains_nodes_and_private_free_metrics",
    "test_public_frontend_latency_motion.py::PublicFrontendLatencyMotionTests::test_warm_mobile_transition_has_one_interactive_surface_and_no_long_task",
    "test_public_frontend_latency_motion.py::PublicFrontendLatencyMotionTests::test_complete_reply_uses_elapsed_budget_and_no_stream_v2_header",
    "test_feedback_followup_ui.py::FeedbackFollowupUITests::test_normal_motion_replay_keeps_four_bubble_gaps_and_unlocks_during_pacing",
    "test_feedback_followup_ui.py::FeedbackFollowupUITests::test_slow_reply_deducts_real_wait_and_delays_early_progress",
    "test_feedback_followup_ui.py::FeedbackFollowupUITests::test_reduced_motion_skips_bubble_delays",
    "test_dialogue_v2_frontend.py::DialogueV2FrontendTests::test_v2_scene_actions_do_not_inherit_legacy_wait_with_motion_flag_off",
    "test_dialogue_v2_frontend.py::SnowAdvVeilProductionTests::test_veil_plays_with_location_title_on_entering_in_person",
    "test_dialogue_v2_frontend.py::SnowAdvVeilProductionTests::test_opening_communicator_during_push_cancels_pending_title",
    "test_immersive_ui_followup.py::ImmersiveControlsTests::test_action_popover_stays_visible_in_simulated_soft_keyboard_viewport",
    "test_snow_adv_features_ui.py::SnowAdvFeaturesUITests::test_time_of_day_change_crossfades_the_whole_page",
    "test_snow_adv_ui.py::SnowAdvOpeningTests::test_first_indoor_move_plays_push_then_veil_second_move_no_push",
    "test_communicator_pacing.py::CommunicatorPacingTests::test_ordinary_and_replay_save_before_wait_and_deliver_whole_bubbles_at_chosen_pace",
})


def _timing_key(item: pytest.Item) -> str:
    return item.nodeid.replace("\\", "/").rsplit("/", 1)[-1]


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    mode = os.environ.get("SNOW_TIMING_TESTS", "")
    if not mode:
        return
    if mode not in {"skip", "only"}:
        raise pytest.UsageError(f"SNOW_TIMING_TESTS must be 'skip' or 'only', not {mode!r}")
    collected = {_timing_key(item) for item in items}
    files = {key.split("::", 1)[0] for key in collected}
    # A renamed or deleted test must not silently drop out of both passes.
    stale = sorted(key for key in TIMING_SENSITIVE if key.split("::", 1)[0] in files and key not in collected)
    if stale:
        raise pytest.UsageError("TIMING_SENSITIVE names tests that were not collected: " + ", ".join(stale))
    keep, drop = [], []
    for item in items:
        timing = _timing_key(item) in TIMING_SENSITIVE
        (keep if timing == (mode == "only") else drop).append(item)
    if drop:
        config.hook.pytest_deselected(items=drop)
        items[:] = keep
