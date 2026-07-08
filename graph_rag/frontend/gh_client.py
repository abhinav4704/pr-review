"""Minimal GitHub REST client for the Streamlit frontend: PAT auth, list
repos/branches, download a tarball snapshot of a ref to a local temp dir.

Trimmed copy of the pattern used in primitive-pr/pr_review/github_client.py.

NOTE: TLS certificate verification is disabled below (verify=False), per
explicit request. This means the PAT and downloaded source are NOT protected
against a man-in-the-middle on this connection — only do this on a trusted
network. Re-enable by setting `self.s.verify = True` if that risk isn't
acceptable.
"""
from __future__ import annotations

import io
import logging
import os
import tarfile
import tempfile
import warnings
from dataclasses import dataclass
from typing import List, Optional

import requests
import urllib3

log = logging.getLogger(__name__)

API = "https://api.github.com"


class GitHubError(Exception):
    pass


@dataclass
class PullRequest:
    number: int
    title: str
    head_ref: str
    head_sha: str


class GitHubClient:
    def __init__(self, token: str) -> None:
        if not token:
            raise GitHubError("A GitHub personal access token is required.")
        self.token = token
        self.s = requests.Session()
        self.s.verify = False
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        warnings.filterwarnings("ignore", message="Unverified HTTPS request")
        log.warning(
            "TLS certificate verification is DISABLED for GitHub requests. "
            "The PAT and downloaded source are not protected against MITM."
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
        repos = self._paged(
            f"{API}/user/repos", limit,
            params={"sort": "updated", "affiliation": "owner,collaborator,organization_member"},
        )
        return [r["full_name"] for r in repos]

    def list_branches(self, full_name: str, limit: int = 200) -> List[str]:
        branches = self._paged(f"{API}/repos/{full_name}/branches", limit)
        names = [b["name"] for b in branches]
        if not names:
            raise GitHubError(
                f"No branches returned for {full_name} — check the PAT has 'repo' "
                "scope (private repos) and the repo name is correct."
            )
        return names

    def default_branch(self, full_name: str) -> str:
        return self._get(f"{API}/repos/{full_name}").json().get("default_branch", "main")

    def download_source(self, full_name: str, ref: str, dest_root: Optional[str] = None) -> str:
        """Download+extract a tarball snapshot of `ref`. Returns the extracted
        repo root path. Guards against path-traversal in the archive."""
        r = self._get(f"{API}/repos/{full_name}/tarball/{ref}")
        dest = dest_root or tempfile.mkdtemp(prefix="graphrag_ui_")
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
