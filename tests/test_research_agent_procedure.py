"""research_agent/DAILY_PROCEDURE.md and DAILY_PROMPT.md stay runnable as written.

Pure and offline: the kit's own argument parser reads every command the procedure gives,
and the app's own identity patterns check every concrete agent identity the procedure and
the prompts give (the intraday prompt's version was first drafted at 37 characters, which
the app refuses: AGENT_VERSION_INVALID, 422 for the whole report).
"""

import re
import shlex
from pathlib import Path

from catalyst_lab.agent_identity import AGENT_ID, AGENT_VERSION
from research_agent import run

KIT = Path(__file__).resolve().parents[1] / "research_agent"
PROCEDURE = (KIT / "DAILY_PROCEDURE.md").read_text(encoding="utf-8")
PROMPTS = (KIT / "DAILY_PROMPT.md").read_text(encoding="utf-8")
SAMPLE = {"DAY": "2026-09-28"}  # Every other shell variable stands in as a plain word.


def section(text, heading):
    """The text under ``heading`` (a ``## `` line), up to the next ``## `` heading."""
    start = text.index(f"\n{heading}\n")
    end = text.find("\n## ", start + 1)
    return text[start:end if end != -1 else None]


def commands(text):
    """Every ``python -m research_agent.run ...`` command in the text's ``sh`` blocks, as the
    argv the kit receives (line continuations joined, shell variables given sample values)."""
    found = []
    for block in re.findall(r"^[ \t]*```sh\n(.*?)^[ \t]*```[ \t]*$", text, re.M | re.S):
        for line in block.replace("\\\n", " ").splitlines():
            if "research_agent.run" in line:
                argv = shlex.split(line.split("research_agent.run", 1)[1])
                found.append([re.sub(r"\$([A-Z_]+)", lambda m: SAMPLE.get(m.group(1), "x"), arg)
                              for arg in argv])
    return found


def test_every_procedure_command_parses_with_the_kits_own_cli():
    found = commands(PROCEDURE)
    assert len(found) >= 25  # The morning, evening, intraday and session-harness commands.
    parser = run.build_parser()
    for argv in found:
        parser.parse_args(argv)  # An unknown flag or a bad value exits with an error.


def test_the_intraday_run_is_the_short_sequence_under_the_intraday_profile():
    text = section(PROCEDURE, "## Intraday run (every 2 hours)")
    parser = run.build_parser()
    parsed = [parser.parse_args(argv) for argv in commands(text)]
    steps = [args.command for args in parsed if args.command != "all"]
    assert steps == ["context", "market", "levels", "build", "build", "validate", "submit"]
    for args in parsed:
        assert args.run_dir == "x"  # Every step names the run's own folder.
        if args.command in {"market", "levels", "build", "all"}:
            assert args.profile == "intraday", args
        if args.command in {"build", "all"}:
            assert args.max_picks == 10, args
        if args.command == "build":
            assert args.lessons is None  # No lessons in an intraday run.
    assert [args.command for args in parsed].count("all") == 1
    for daily_only in ("lessons", "checklist", "outlook", "movers", "postmortem"):
        assert daily_only not in {args.command.split("-")[0] for args in parsed}
    assert "runs/<YYYY-MM-DD>/intraday-<HHMM>" in text and "30 minutes" in text
    # --profile intraday is INTRADAY_V2; the first intraday profile stays selectable.
    assert "**`INTRADAY_V2`** (`--profile intraday`" in text
    assert "`--profile intraday-v1`" in text


def test_every_concrete_agent_identity_is_one_the_app_accepts():
    checked = []
    for text in (PROCEDURE, PROMPTS):
        for flag, pattern in (("--agent-id", AGENT_ID), ("--agent-version", AGENT_VERSION)):
            for value in re.findall(re.escape(flag) + r"[ =]([^\s`]+)", text):
                value = value.strip("\"').,;")
                if value.startswith(("<", "$")):
                    continue  # A placeholder the scheduler fills in.
                assert pattern.fullmatch(value), f"{flag} {value}"
                checked.append(value)
    assert "claude-as-muse-intraday-v1-09.28" in checked and "fable" in checked


def test_the_update_run_is_update_then_submit_in_its_own_folder():
    text = section(PROCEDURE, "## Update run (every 2 hours, RESEARCH_SCHEDULE_V2)")
    parser = run.build_parser()
    parsed = [parser.parse_args(argv) for argv in commands(text)]
    assert [args.command for args in parsed] == ["update", "submit"]
    update_args, submit_args = parsed
    assert update_args.profile == "intraday" and update_args.max_picks == 8
    assert update_args.base_url == "x" and update_args.token_file == "x"
    assert update_args.run_dir == submit_args.run_dir == "x"
    assert "runs/<YYYY-MM-DD>/update-<HHMM>" in text
    for code in ("UPDATE_NEEDS_SCHEDULE_V2", "UPDATE_SLOT_IS_FULL_RUN",
                 "RESEARCH_WITHDRAWAL_RECORDED", "WITHDRAWAL_ID_CONFLICT"):
        assert code in text
    assert "Any withdrawal answer but 200 stops before the report is sent" in text
    # The intraday run says when it does not apply.
    intraday = section(PROCEDURE, "## Intraday run (every 2 hours)")
    assert "Under `RESEARCH_SCHEDULE_V2`" in intraday


def test_the_morning_reads_the_derivatives_context_right_before_build():
    text = section(PROCEDURE, "## Morning")
    parser = run.build_parser()
    parsed = [parser.parse_args(argv) for argv in commands(text)]
    steps = [args.command for args in parsed]
    assert steps.index("derivatives") == steps.index("build") - 1
    [build_args] = [args for args in parsed if args.command == "build"]
    assert build_args.derivatives == "x/derivatives.json"
    assert build_args.lessons == "x/emphasis.json"
    prompt = section(PROMPTS, "## Morning (08:00 New York)")
    assert "--derivatives derivatives.json" in prompt


def test_the_update_prompt_gives_its_placeholders_identity_and_section():
    prompt = section(PROMPTS, "## Update (every 2 hours, RESEARCH_SCHEDULE_V2)")
    for placeholder in ("<RUN_DIR", "<BASE_URL", "<TOKEN_FILE_PATH>"):
        assert placeholder in prompt
    assert "never read this file's contents" in prompt
    assert "--agent-id muse --agent-version claude-as-muse-update-v1-09.29" in prompt
    assert AGENT_VERSION.fullmatch("claude-as-muse-update-v1-09.29")  # 30 of 32 characters.
    assert "update --profile intraday" in prompt and "runs/2026-09-29/update-1000" in prompt
    assert '"Update run (every 2 hours,\nRESEARCH_SCHEDULE_V2)"' in prompt
    intraday = section(PROMPTS, "## Intraday (every 2 hours)")
    assert "RESEARCH_SCHEDULE_V2" in intraday


def test_the_intraday_prompt_gives_its_placeholders_identity_and_profile():
    prompt = section(PROMPTS, "## Intraday (every 2 hours)")
    for placeholder in ("<RUN_DIR", "<BASE_URL", "<TOKEN_FILE_PATH>"):
        assert placeholder in prompt
    assert "never read this file's contents" in prompt
    assert "--agent-id muse --agent-version claude-as-muse-intraday-v1-09.28" in prompt
    assert len("claude-as-muse-intraday-v1-09.28") <= 32  # The app's AGENT_VERSION maximum.
    assert prompt.count("--profile intraday") == 3  # market, levels and build.
    assert "INTRADAY_V2" in prompt and "--profile intraday-v1" not in prompt
    assert "--max-picks 10" in prompt and "Intraday run (every 2" in prompt
