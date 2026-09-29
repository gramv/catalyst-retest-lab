"""Replaceable command provider for external Muse research.

The command receives its JSON request in an explicit prompt and writes one schema-valid JSON
object to the configured output file. It never receives app tokens or broker state.
"""

import json
import os
import subprocess
import tempfile
from pathlib import Path

from catalyst_lab.muse_guidelines import (
    MUSE_GUIDELINES,
    MUSE_GUIDELINES_SHA256,
    MUSE_GUIDELINES_VERSION,
)
from catalyst_lab.muse_reports import MuseContender
from catalyst_lab.review_storage import SourceExcerpt


def _object_schema(model):
    schema = model.model_json_schema()
    definitions = schema.pop("$defs", {})
    technical = definitions.get("TechnicalEvidence")
    if technical:
        technical["properties"]["level_references"] = {
            "type": "object", "additionalProperties": False,
            "properties": {name: {"$ref": "#/$defs/LevelEvidence"}
                           for name in ("entry_trigger", "stop", "target")},
            "required": ["entry_trigger", "stop", "target"],
        }
    def strict_objects(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                node["additionalProperties"] = False
                node["required"] = list(node.get("properties", {}))
            node.pop("default", None)
            for child in node.values():
                strict_objects(child)
        elif isinstance(node, list):
            for child in node:
                strict_objects(child)
    strict_objects(schema)
    strict_objects(definitions)
    return schema, definitions


CONTENDER_SCHEMA, CONTENDER_DEFS = _object_schema(MuseContender)
# MUSE_RESEARCH_GUIDELINES_V2: the provider must always supply the rationale block,
# although the intake model still accepts legacy (unversioned) items without it.
CONTENDER_SCHEMA["properties"]["selection_rationale"] = {"$ref": "#/$defs/SelectionRationale"}
SOURCE_SCHEMA, SOURCE_DEFS = _object_schema(SourceExcerpt)

REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"enum": ["report", "no_op"]},
        "reason": {"type": ["string", "null"]},
        "items": {"type": "array", "maxItems": 30, "items": CONTENDER_SCHEMA},
    },
    "required": ["kind", "reason", "items"],
    "additionalProperties": False,
    "$defs": CONTENDER_DEFS,
}
FOLLOWUP_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"enum": ["followup", "no_op"]},
        "reason": {"type": ["string", "null"]},
        "sources": {"type": "array", "minItems": 0, "maxItems": 8, "items": SOURCE_SCHEMA},
        "thesis": {"type": "string"},
        "disproof": {"type": "string"},
        "economic_relationship": {"type": "string"},
        "technical_facts": {"anyOf": [{"type": "null"}, {
            "type": "object", "additionalProperties": False,
            "properties": {"observed_at": {"type": "string"},
                           "timeframe": {"type": "string"}, "summary": {"type": "string"},
                           "facts": {"type": "array", "items": {"type": "string"}},
                           "source_ids": {"type": "array", "items": {"type": "string"}}},
            "required": ["observed_at", "timeframe", "summary", "facts", "source_ids"],
        }]},
    },
    "required": [
        "kind",
        "reason",
        "sources",
        "thesis",
        "disproof",
        "economic_relationship",
        "technical_facts",
    ],
    "additionalProperties": False,
    "$defs": SOURCE_DEFS,
}
NEWS_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"enum": ["news", "no_op"]},
        "reason": {"type": ["string", "null"]},
        "sources": {"type": "array", "minItems": 0, "maxItems": 8, "items": SOURCE_SCHEMA},
        "material": {"type": "boolean"},
    },
    "required": ["kind", "reason", "sources", "material"],
    "additionalProperties": False,
    "$defs": SOURCE_DEFS,
}


class CommandProvider:
    def __init__(self, argv, *, timeout_seconds=300, max_output_bytes=1_048_576):
        if (
            not isinstance(argv, (list, tuple))
            or not argv
            or any(not isinstance(v, str) or not v for v in argv)
        ):
            raise ValueError("EXPLICIT_COMMAND_ARGV_REQUIRED")
        self.argv = tuple(argv)
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes

    @staticmethod
    def codex_argv(binary="codex"):
        """Official shape; installation and authentication are operator prerequisites."""
        return [binary, "-c", 'web_search="live"', "exec", "--ephemeral",
                "--sandbox", "read-only", "--skip-git-repo-check", "--ignore-user-config"]

    def invoke(self, request, schema=None):
        schema = schema or (
            {"evidence_followup": FOLLOWUP_SCHEMA, "position_news": NEWS_SCHEMA}.get(
                request.get("job"), REPORT_SCHEMA
            )
        )
        prompt = (
            f"MUSE GUIDELINES: {MUSE_GUIDELINES_VERSION}\n"
            f"GUIDELINES SHA256: {MUSE_GUIDELINES_SHA256}\n"
            + MUSE_GUIDELINES
            + "\nThe following request supplies task data under these guidelines.\n"
            + "Do not add fields outside the supplied output schema.\n"
            + "Return only evidence relevant to the requested research, follow-up or news job.\n"
            + "\nREQUEST JSON:\n"
            + json.dumps(request, sort_keys=True)
        )
        with tempfile.TemporaryDirectory(prefix="catalyst-muse-") as directory:
            root = Path(directory)
            schema_path, output_path = root / "schema.json", root / "result.json"
            schema_path.write_text(json.dumps(schema), encoding="utf-8")
            argv = [
                *self.argv,
                prompt,
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(output_path),
            ]
            permitted = {
                "PATH",
                "HOME",
                "USER",
                "LOGNAME",
                "TMPDIR",
                "LANG",
                "LC_ALL",
                "TERM",
                "CODEX_HOME",
            }
            env = {k: v for k, v in os.environ.items() if k in permitted}
            subprocess.run(
                argv,
                text=True,
                env=env,
                cwd=root,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=self.timeout_seconds,
                check=True,
                shell=False,
            )
            if not output_path.is_file() or output_path.stat().st_size > self.max_output_bytes:
                raise ValueError("MUSE_PROVIDER_OUTPUT_INVALID")
            value = json.loads(output_path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("MUSE_PROVIDER_OUTPUT_INVALID")
            return value
