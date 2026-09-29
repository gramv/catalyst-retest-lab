"""Launch an explicitly selected local component from an owner-only JSON config (v2).

No shell, sourcing, credential discovery or service installation is performed. The launcher
verifies the release hash, applies the crash-loop breaker, waits for the ledger before the app
and Muse, and sends child output to rotating mode-0600 logs. A tripped breaker exits 0 so that
launchd stops restarting; a child's own exit status is passed through.
"""

import argparse

from catalyst_lab.managed_ops import (
    COMPONENTS,
    REFUSAL_CODE,
    launch_component,
    private_standard_streams,
)


def main():
    private_standard_streams()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--component", choices=COMPONENTS, required=True)
    args = parser.parse_args()
    try:
        result = launch_component(args.config, args.component)
    except Exception as exc:
        # Only this project's own refusal codes (e.g. LEDGER_RETIRED) are shown; never paths,
        # DSNs, values or driver text.
        code = str(exc)
        raise SystemExit("PRIVATE_COMPONENT_FAILED" + (
            ": " + code if REFUSAL_CODE.fullmatch(code) else "")) from None
    if type(result) is int:
        raise SystemExit(result)


if __name__ == "__main__":
    main()
