"""Compatibility contract for the flat Audio/video Whisper CLI surface."""
# region [00] Contexto del módulo
# Módulo: tests/test_cli_audio_surface.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]

# region [01] Dependencias del módulo
from __future__ import annotations

import argparse
from pathlib import Path

import pytest
from neocortex.platform.policy import (
    default_local_models_only,
    default_whisper_compute_type,
    default_whisper_device,
    default_whisper_model_cache,
)

from neocortex.api.cli.cli_parser import build_parser, decimal_megabytes
from neocortex.api.cli.cli_validation import validate_arguments
# endregion [01]

# region [02] Implementación


AUDIO_GROUP_TITLE = "Audio/video Whisper transcription route"
VIDEO_GROUP_TITLE = "Visual video route"
CODE_GROUP_TITLE = "Code knowledge"
SEMANTIC_GROUP_TITLE = "Multimodal semantic index"
KNOWLEDGE_GROUP_TITLE = "Read-only Knowledge Plane"


def _expected_store(
    option: str,
    destination: str,
    *,
    default: object = None,
    type_name: str | None = None,
    choices: tuple[str, ...] | None = None,
    metavar: str | None = None,
    help_text: str | None = None,
) -> tuple[object, ...]:
    return (
        (option,),
        destination,
        "_StoreAction",
        None,
        None,
        default,
        type_name,
        choices,
        metavar,
        False,
        help_text,
    )


def _expected_flag(
    option: str,
    destination: str,
    help_text: str,
) -> tuple[object, ...]:
    return (
        (option,),
        destination,
        "_StoreTrueAction",
        0,
        True,
        False,
        None,
        None,
        None,
        False,
        help_text,
    )


def _expected_boolean_optional(
    option: str,
    negative_option: str,
    destination: str,
    help_text: str,
    *,
    default: bool = True,
) -> tuple[object, ...]:
    return (
        (option, negative_option),
        destination,
        "BooleanOptionalAction",
        0,
        None,
        default,
        None,
        None,
        None,
        False,
        help_text,
    )


EXPECTED_AUDIO_ACTIONS = (
    _expected_store(
        "--audio-max-mb",
        "audio_max_file_bytes",
        type_name="decimal_megabytes",
        metavar="MB",
        help_text="transcribe only media files at or below this decimal size",
    ),
    _expected_store(
        "--audio-max-count",
        "audio_max_documents",
        type_name="int",
        metavar="N",
    ),
    _expected_store(
        "--audio-max-duration-seconds",
        "audio_max_duration_seconds",
        default=6 * 60 * 60,
        type_name="float",
    ),
    _expected_store(
        "--audio-max-transcript-chars",
        "audio_max_transcript_chars",
        default=5_000_000,
        type_name="int",
    ),
    _expected_store(
        "--audio-max-segments",
        "audio_max_segments",
        default=100_000,
        type_name="int",
    ),
    _expected_boolean_optional(
        "--audio-include-video",
        "--no-audio-include-video",
        "audio_include_video",
        "also transcribe audio streams inside detected video containers",
    ),
    _expected_store("--whisper-model", "whisper_model", default="small"),
    _expected_store(
        "--whisper-device",
        "whisper_device",
        default=default_whisper_device(),
        choices=("auto", "cpu", "cuda"),
    ),
    _expected_store(
        "--whisper-compute-type",
        "whisper_compute_type",
        default=default_whisper_compute_type(),
    ),
    _expected_store(
        "--audio-language",
        "audio_language",
        default="auto",
        help_text="language code such as es or auto for Whisper language detection",
    ),
    _expected_store(
        "--whisper-beam-size",
        "whisper_beam_size",
        default=5,
        type_name="int",
    ),
    _expected_boolean_optional(
        "--audio-vad",
        "--no-audio-vad",
        "audio_vad",
        "use voice activity detection before transcription",
    ),
    _expected_store(
        "--audio-file-timeout",
        "audio_file_timeout",
        default=3600.0,
        type_name="float",
    ),
    _expected_store(
        "--audio-worker-startup-timeout",
        "audio_worker_startup_timeout",
        default=1800.0,
        type_name="float",
    ),
    _expected_store(
        "--audio-worker-memory-mb",
        "audio_worker_memory_mb",
        default=4096,
        type_name="int",
    ),
    _expected_store(
        "--audio-memory-budget-mb",
        "audio_memory_budget_mb",
        default=None,
        type_name="int",
        help_text="aggregate route memory ceiling in MiB; default: automatic",
    ),
    _expected_store(
        "--audio-min-free-memory-mb",
        "audio_min_free_memory_mb",
        default=2048,
        type_name="int",
    ),
    _expected_store(
        "--audio-min-free-commit-mb",
        "audio_min_free_commit_mb",
        default=2048,
        type_name="int",
    ),
    _expected_store(
        "--audio-memory-wait-timeout",
        "audio_memory_wait_timeout",
        default=300.0,
        type_name="float",
    ),
    _expected_store(
        "--audio-model-cache",
        "audio_model_cache",
        default=default_whisper_model_cache(),
        type_name="Path",
        metavar="DIRECTORY",
    ),
    _expected_boolean_optional(
        "--audio-local-models-only",
        "--no-audio-local-models-only",
        "audio_local_models_only",
        "never download model weights; require an existing local model cache",
        default=default_local_models_only(),
    ),
    _expected_flag(
        "--retry-audio-errors",
        "retry_audio_errors",
        "force one new attempt for unchanged cached audio errors",
    ),
    _expected_store("--ffprobe-path", "ffprobe_path"),
    _expected_store("--audio-search", "audio_search", metavar="QUERY"),
    _expected_store(
        "--audio-search-limit",
        "audio_search_limit",
        default=20,
        type_name="int",
    ),
    _expected_flag(
        "--audio-doctor",
        "audio_doctor",
        "check Whisper and FFprobe without loading or downloading a model",
    ),
)

EXPECTED_AUDIO_HELP = (
    "Audio/video Whisper transcription route:\n"
    "  --audio-max-mb MB     transcribe only media files at or below this decimal\n"
    "                        size\n"
    "  --audio-max-count N\n"
    "  --audio-max-duration-seconds AUDIO_MAX_DURATION_SECONDS\n"
    "  --audio-max-transcript-chars AUDIO_MAX_TRANSCRIPT_CHARS\n"
    "  --audio-max-segments AUDIO_MAX_SEGMENTS\n"
    "  --audio-include-video, --no-audio-include-video\n"
    "                        also transcribe audio streams inside detected video\n"
    "                        containers\n"
    "  --whisper-model WHISPER_MODEL\n"
    "  --whisper-device {auto,cpu,cuda}\n"
    "  --whisper-compute-type WHISPER_COMPUTE_TYPE\n"
    "  --audio-language AUDIO_LANGUAGE\n"
    "                        language code such as es or auto for Whisper language\n"
    "                        detection\n"
    "  --whisper-beam-size WHISPER_BEAM_SIZE\n"
    "  --audio-vad, --no-audio-vad\n"
    "                        use voice activity detection before transcription\n"
    "  --audio-file-timeout AUDIO_FILE_TIMEOUT\n"
    "  --audio-worker-startup-timeout AUDIO_WORKER_STARTUP_TIMEOUT\n"
    "  --audio-worker-memory-mb AUDIO_WORKER_MEMORY_MB\n"
    "  --audio-memory-budget-mb AUDIO_MEMORY_BUDGET_MB\n"
    "                        aggregate route memory ceiling in MiB; default:\n"
    "                        automatic\n"
    "  --audio-min-free-memory-mb AUDIO_MIN_FREE_MEMORY_MB\n"
    "  --audio-min-free-commit-mb AUDIO_MIN_FREE_COMMIT_MB\n"
    "  --audio-memory-wait-timeout AUDIO_MEMORY_WAIT_TIMEOUT\n"
    "  --audio-model-cache DIRECTORY\n"
    "  --audio-local-models-only, --no-audio-local-models-only\n"
    "                        never download model weights; require an existing\n"
    "                        local model cache\n"
    "  --retry-audio-errors  force one new attempt for unchanged cached audio\n"
    "                        errors\n"
    "  --ffprobe-path FFPROBE_PATH\n"
    "  --audio-search QUERY\n"
    "  --audio-search-limit AUDIO_SEARCH_LIMIT\n"
    "  --audio-doctor        check Whisper and FFprobe without loading or\n"
    "                        downloading a model\n"
    "\n"
)


def _normalized_action(action: argparse.Action) -> tuple[object, ...]:
    choices = None if action.choices is None else tuple(action.choices)
    type_name = None if action.type is None else action.type.__name__
    return (
        tuple(action.option_strings),
        action.dest,
        type(action).__name__,
        action.nargs,
        action.const,
        action.default,
        type_name,
        choices,
        action.metavar,
        action.required,
        action.help,
    )


def test_audio_actions_aliases_and_help_preserve_the_normalized_contract() -> None:
    parser = build_parser()
    group = next(item for item in parser._action_groups if item.title == AUDIO_GROUP_TITLE)

    assert group is parser._action_groups[-5]
    assert parser._action_groups[-4].title == VIDEO_GROUP_TITLE
    assert parser._action_groups[-3].title == CODE_GROUP_TITLE
    assert parser._action_groups[-2].title == SEMANTIC_GROUP_TITLE
    assert parser._action_groups[-1].title == KNOWLEDGE_GROUP_TITLE
    assert tuple(_normalized_action(action) for action in group._group_actions) == (
        EXPECTED_AUDIO_ACTIONS
    )
    max_file_action = next(
        action for action in group._group_actions if action.dest == "audio_max_file_bytes"
    )
    assert max_file_action.type is decimal_megabytes
    help_text = parser.format_help()
    help_start = help_text.index(f"{AUDIO_GROUP_TITLE}:\n")
    help_end = help_text.index(f"{VIDEO_GROUP_TITLE}:\n", help_start)
    assert help_text[help_start:help_end] == EXPECTED_AUDIO_HELP


def test_audio_explicit_aliases_and_abbreviation_policy_remain_stable() -> None:
    parser = build_parser()
    args = parser.parse_args(
        (
            "--audio-doctor",
            "--audio-include-video",
            "--no-audio-vad",
            "--audio-model-cache=models",
        )
    )

    assert parser.allow_abbrev is False
    assert args.audio_include_video is True
    assert args.audio_vad is False
    assert args.audio_model_cache == Path("models")
    assert args._explicit_options == frozenset(
        {
            "audio_doctor",
            "audio_include_video",
            "audio_vad",
            "audio_model_cache",
        }
    )


@pytest.mark.parametrize(
    ("arguments", "message"),
    (
        (
            ("--audio-max-count", "0", "--audio-max-duration-seconds", "0"),
            "--audio-max-count must be positive",
        ),
        (
            ("--audio-max-duration-seconds", "0", "--whisper-model", " "),
            "--audio-max-duration-seconds must be positive",
        ),
        (
            (
                "--audio-min-free-memory-mb",
                "-1",
                "--audio-memory-wait-timeout",
                "-1",
            ),
            "audio memory headroom cannot be negative",
        ),
        (
            ("--whisper-model", " ", "--whisper-compute-type", ""),
            "--whisper-model must be non-empty",
        ),
        (
            ("--audio-search", " ", "--audio-search-limit", "0"),
            "--audio-search-limit must be between 1 and 1000",
        ),
        (
            ("--audio-search", " "),
            "--audio-search must be non-empty",
        ),
    ),
)
def test_audio_validation_error_precedence_remains_stable(
    arguments: tuple[str, ...],
    message: str,
) -> None:
    args = build_parser().parse_args(arguments)

    with pytest.raises(SystemExit) as raised:
        validate_arguments(args)

    assert str(raised.value) == message


# endregion [02]
