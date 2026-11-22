"""Garde-fou PreToolUse (Bash) : refuse une commande de vérification dont le code de sortie serait masqué.

`pytest … | tail` renvoie le code de `tail` (0) même quand les tests échouent : une vérification ne doit
jamais passer par un pipe sans `pipefail`. Autorisé : `.claude/bin/journal …`, `set -o pipefail`,
`bash -o pipefail -c …`, ou l'usage explicite de PIPESTATUS.
"""

from __future__ import annotations

import json
import re
import sys

VERIFICATION = re.compile(
    r"\b(pytest|unittest|ruff|mypy|tox|nox|npm (test|run)|pnpm (test|run)|yarn (test|run)|node\s+\S*test"
    r"|playwright test|cargo test|go test|make (test|check|lint)|test-signature)\b"
)
PIPE_FILTRANT = re.compile(r"(?<!\|)\|(?!\|)\s*(tail|head|grep|sed|awk|cut|less|more|tee|wc|sort|uniq)\b")
PROTEGE = re.compile(r"pipefail|PIPESTATUS|\.claude/bin/journal")


def verdict(commande: str) -> str | None:
    if PROTEGE.search(commande) or not VERIFICATION.search(commande) or not PIPE_FILTRANT.search(commande):
        return None
    return (
        "Commande de vérification passée dans un pipe sans pipefail : son code de sortie serait masqué "
        "(un échec paraîtrait réussi). Relancer avec `.claude/bin/journal <étiquette> <commande>` "
        "(journal complet sur disque, code exact, résumé jamais tronqué) ou `bash -o pipefail -c '…'`."
    )


def main() -> int:
    try:
        commande = json.load(sys.stdin).get("tool_input", {}).get("command", "")
    except (ValueError, AttributeError):
        return 0
    raison = verdict(commande)
    if raison:
        json.dump(
            {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                    "permissionDecisionReason": raison}},
            sys.stdout,
            ensure_ascii=False,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
