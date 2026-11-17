"""Read-only import of explicitly selected public GitHub issues."""
import re

import httpx


def github_issue(url: str) -> str:
    match = re.fullmatch(
        r"https://github\.com/([A-Za-z0-9-]{1,39})/([A-Za-z0-9_-][A-Za-z0-9_.-]{0,99})/issues/([1-9][0-9]{0,11})/?",
        url.strip(),
    )
    if not match:
        raise ValueError("Enter a public GitHub issue URL.")
    owner, repo, number = match.groups()
    try:
        response = httpx.get(
            f"https://api.github.com/repos/{owner}/{repo}/issues/{number}",
            headers={"Accept": "application/vnd.github+json"},
            timeout=8,
            follow_redirects=False,
        )
    except httpx.RequestError:
        raise ValueError("GitHub could not be reached. Try again shortly.") from None
    if response.status_code in (403, 429):
        raise ValueError("GitHub denied the request or reached its public API limit.")
    if response.status_code != 200:
        raise ValueError("Public GitHub issue could not be found.")
    try:
        issue = response.json()
        if "pull_request" in issue:
            raise ValueError
        return f"Source: https://github.com/{owner}/{repo}/issues/{number}\n{issue['title']}\n\n{issue.get('body') or ''}"
    except (ValueError, KeyError, TypeError):
        raise ValueError("GitHub did not return an issue.") from None
