"""GitHub REST client — PAT auth, list repos/branches/PRs, fetch diffs + tarballs."""

from __future__ import annotations

import io
import logging
import os
import tarfile
import tempfile
from dataclasses import dataclass
from typing import List, Optional

import requests

log = logging.getLogger(__name__)

API = "https://api.github.com"
DIFF_ACCEPT = "application/vnd.github.diff"


class GitHubError(Exception):
    pass


@dataclass
class PullRequest:
    number: int
    title: str
    state: str
    base_ref: str
    head_ref: str
    head_sha: str
    head_repo: str
    author: str


@dataclass
class Commit:
    sha: str
    message: str   # first line, truncated to 80 chars
    author: str
    date: str      # YYYY-MM-DD


class GitHubClient:
    def __init__(self, token: str) -> None:
        if not token:
            raise GitHubError("A GitHub PAT is required.")
        self.token = token
        self.s = requests.Session()
        if os.environ.get("PR_REVIEW_INSECURE_TLS") == "1":
            self.s.verify = False
            log.warning(
                "TLS certificate verification is DISABLED (PR_REVIEW_INSECURE_TLS=1). "
                "Your GitHub PAT is not protected against MITM attacks."
            )
        self.s.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })

    def _get(self, url: str, **kw) -> requests.Response:
        r = self.s.get(url, timeout=60, **kw)
        if r.status_code == 401:
            raise GitHubError("Unauthorized — check the token and its scopes.")
        if r.status_code == 403 and "rate limit" in r.text.lower():
            raise GitHubError("GitHub rate limit hit. Try again later.")
        if r.status_code >= 400:
            raise GitHubError(f"GitHub {r.status_code}: {r.text[:200]}")
        return r

    def _paged(self, url: str, limit: int, params=None) -> List[dict]:
        out: List[dict] = []
        page = 1
        params = dict(params or {})
        while len(out) < limit:
            params.update({"per_page": 100, "page": page})
            batch = self._get(url, params=params).json()
            if not isinstance(batch, list) or not batch:
                break
            out.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return out[:limit]

    def whoami(self) -> str:
        return self._get(f"{API}/user").json().get("login", "?")

    def list_repos(self, limit: int = 300) -> List[str]:
        repos = self._paged(f"{API}/user/repos", limit,
                            params={"sort": "updated",
                                    "affiliation": "owner,collaborator,organization_member"})
        return [r["full_name"] for r in repos]

    def list_branches(self, full_name: str, limit: int = 200) -> List[str]:
        branches = self._paged(f"{API}/repos/{full_name}/branches", limit)
        return [b["name"] for b in branches]

    def list_pulls(self, full_name: str, state: str = "open",
                   limit: int = 100) -> List[PullRequest]:
        raw = self._paged(f"{API}/repos/{full_name}/pulls", limit,
                          params={"state": state, "sort": "updated", "direction": "desc"})
        out: List[PullRequest] = []
        for p in raw:
            head = p.get("head") or {}
            head_repo = (head.get("repo") or {}).get("full_name") or full_name
            out.append(PullRequest(
                number=p["number"],
                title=p.get("title", ""),
                state=p.get("state", state),
                base_ref=(p.get("base") or {}).get("ref", ""),
                head_ref=head.get("ref", ""),
                head_sha=head.get("sha", ""),
                head_repo=head_repo,
                author=(p.get("user") or {}).get("login", ""),
            ))
        return out

    def get_pr_diff(self, full_name: str, number: int) -> str:
        return self._get(f"{API}/repos/{full_name}/pulls/{number}",
                         headers={"Accept": DIFF_ACCEPT}).text

    def get_commit_diff(self, full_name: str, sha: str) -> str:
        return self._get(f"{API}/repos/{full_name}/commits/{sha}",
                         headers={"Accept": DIFF_ACCEPT}).text

    def list_commits(self, full_name: str, branch: str = "",
                     limit: int = 50) -> List["Commit"]:
        params: dict = {}
        if branch:
            params["sha"] = branch
        raw = self._paged(f"{API}/repos/{full_name}/commits", limit, params=params)
        out: List[Commit] = []
        for c in raw:
            commit_data = c.get("commit") or {}
            author_data = commit_data.get("author") or {}
            out.append(Commit(
                sha=c.get("sha", ""),
                message=(commit_data.get("message") or "").splitlines()[0][:80],
                author=(c.get("author") or {}).get("login") or author_data.get("name") or "?",
                date=(author_data.get("date") or "")[:10],
            ))
        return out

    def compare_diff(self, full_name: str, base: str, head: str) -> str:
        return self._get(f"{API}/repos/{full_name}/compare/{base}...{head}",
                         headers={"Accept": DIFF_ACCEPT}).text

    def download_source(self, full_name: str, ref: str,
                        dest_root: Optional[str] = None) -> str:
        r = self._get(f"{API}/repos/{full_name}/tarball/{ref}")
        dest = dest_root or tempfile.mkdtemp(prefix="prreview_")
        with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz") as tf:
            members = tf.getnames()
            try:
                tf.extractall(dest, filter="data")
            except TypeError:
                # Python < 3.12 fallback: validate paths to prevent traversal.
                safe = []
                dest_real = os.path.realpath(dest)
                for m in tf.getmembers():
                    target = os.path.realpath(os.path.join(dest, m.name))
                    if target.startswith(dest_real + os.sep) or target == dest_real:
                        safe.append(m)
                tf.extractall(dest, members=safe)
        top = members[0].split("/", 1)[0] if members else ""
        return os.path.join(dest, top) if top else dest
