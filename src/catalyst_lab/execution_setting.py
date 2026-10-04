"""``MANAGED_CRYPTO_EXECUTION_JSON``: the switch for the phase-D execution versions (package
exec-d). Optional; absent (or both null) means neither version is admitted, exactly as before.

The value is a JSON object with exactly two keys::

    {"stop_execution": null | "CRYPTO_STOP_EXECUTION_V1",
     "maker_entry": null | "CRYPTO_MAKER_ENTRY_V1"}

Each switch only decides what admission records for NEW setups; a setup keeps the versions it
was admitted under whatever the setting later says. Nothing here sends an order: every order of
both versions goes through the existing exact one-use risk authorization.
"""

import json
from dataclasses import dataclass

from catalyst_lab import maker_entry, stop_execution

ENV = "MANAGED_CRYPTO_EXECUTION_JSON"
INVALID = "CRYPTO_EXECUTION_SETTING_INVALID"


@dataclass(frozen=True)
class CryptoExecutionSetting:
    stop_execution: bool = False
    maker_entry: bool = False

    def __post_init__(self):
        if type(self.stop_execution) is not bool or type(self.maker_entry) is not bool:
            raise ValueError(INVALID)

    def record(self):
        return {"stop_execution": stop_execution.VERSION if self.stop_execution else None,
                "maker_entry": maker_entry.VERSION if self.maker_entry else None}


OFF = CryptoExecutionSetting()


def parse(text):
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        raise ValueError(INVALID) from None
    if not isinstance(value, dict) or set(value) != {"stop_execution", "maker_entry"}:
        raise ValueError(INVALID)
    if value["stop_execution"] not in (None, stop_execution.VERSION) or (
            value["maker_entry"] not in (None, maker_entry.VERSION)):
        raise ValueError(INVALID)
    return CryptoExecutionSetting(value["stop_execution"] is not None,
                                  value["maker_entry"] is not None)


def from_env(env):
    """The setting, or ``OFF`` when the variable is absent."""
    text = env.get(ENV)
    return OFF if text is None else parse(text)
