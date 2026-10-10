"""Un seul geste vers GitHub : lancer un workflow (utilisé par la commande /podcast du bot).

Dans GitHub Actions, le jeton (GITHUB_TOKEN), le dépôt (GITHUB_REPOSITORY) et la branche (GITHUB_REF_NAME) sont
fournis par le workflow ; il faut juste lui donner la permission « actions: write » (c'est fait dans journal.yml).
"""
from __future__ import annotations

import os

import requests

API_ROOT = "https://api.github.com"


class GitHubError(RuntimeError):
    def __init__(self, message: str, *, missing: bool = False):
        super().__init__(message)
        self.missing = missing   # True = rien n'est configuré (pas un vrai échec)


def dispatch(workflow: str, inputs: dict | None = None, environ=None) -> None:
    env = os.environ if environ is None else environ
    token = (env.get("GITHUB_TOKEN") or "").strip()
    repo = (env.get("GITHUB_REPOSITORY") or "").strip()
    ref = (env.get("GITHUB_REF_NAME") or "main").strip()
    if not token or "/" not in repo:
        raise GitHubError("jeton ou dépôt GitHub absents", missing=True)
    try:
        resp = requests.post(
            f"{API_ROOT}/repos/{repo}/actions/workflows/{workflow}/dispatches",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"},
            json={"ref": ref, "inputs": {k: str(v) for k, v in (inputs or {}).items()}},
            timeout=30,
        )
    except requests.RequestException as exc:  # le texte de l'exception pourrait contenir l'adresse : on ne garde que son nom
        raise GitHubError(f"réseau ({exc.__class__.__name__})") from exc
    # GitHub répond « 204 » (sans corps) ; depuis 2026 il peut répondre « 200 » avec le détail du lancement quand on le
    # demande (option return_run_details). Dans les deux cas le workflow est lancé : tout code 2xx veut dire « c'est parti ».
    if 200 <= resp.status_code < 300:
        return
    try:
        detail = str(resp.json().get("message", ""))[:160]
    except ValueError:
        detail = ""
    hint = ""
    if resp.status_code in (401, 403):
        hint = " (il manque sans doute la permission « actions: write » dans .github/workflows/journal.yml)"
    elif resp.status_code == 404:
        hint = " (workflow ou branche introuvable)"
    elif resp.status_code == 422:
        hint = " (le workflow n'accepte pas ces réglages : mets à jour podcast.yml)"
    raise GitHubError(f"HTTP {resp.status_code} {detail}{hint}")
