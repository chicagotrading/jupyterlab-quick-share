from __future__ import annotations
import functools
import json
import logging
import os
import pathlib
import re
import shlex
import subprocess
import urllib.parse
import time
import typing as t
from dataclasses import asdict
from dataclasses import dataclass
from functools import partial
from string import Template

from jupyter_server.base.handlers import APIHandler
from jupyter_server.serverapp import ServerWebApplication
from jupyter_server.utils import url_path_join as ujoin
import requests
import tornado


EXTENSION_NAME = "jupyterlab-quick-share"
DEFAULT_SETTINGS = {
    "baseDir": f"/tmp/{EXTENSION_NAME}",
    "configByHost": {
        "github.com": {
            "rawUrlTmpl": "https://raw.githubusercontent.com/$org/$repo/$sha/$path",
            "originUrlPat": r"https://github\.com/(?P<org>[^/]+)/(?P<repo>[^/]+)\.git",
            "followRedirects": False,
            "enableGitCredsLookup": False,
        },
    },
}
# TODO: actually look up the configured settings rather than hard-code.
# Ref: https://discourse.jupyter.org/t/accessing-extension-settings-from-server-side-handler/33469
settings: dict[str, t.Any] = {
    "baseDir": f".lsp_symlink/tmp/{EXTENSION_NAME}",
    "configByHost": {
        "bitbucket.chicagotrading.com": {
            "rawUrlTmpl": "https://bitbucket.chicagotrading.com/projects/$org/repos/$repo/raw/$path?at=$sha",
            "originUrlPat": r"https://bitbucket\.chicagotrading\.com/scm/(?P<org>[^/]+)/(?P<repo>[^/]+)\.git",
            "followRedirects": False,
            "enableGitCredsLookup": True,
        },
    },
}


logger = logging.getLogger(EXTENSION_NAME)
logger.setLevel(logging.INFO)


@dataclass(frozen=True)
class UrlData:
    host: str
    org: str
    repo: str
    path: str
    sha: str


def _url_data_from_path(path: str, exc_tp: t.Type[Exception] = Exception) -> UrlData:
    abs_path = pathlib.Path(path).resolve()
    clone_dir = abs_path.parent
    while not clone_dir.joinpath(".git").is_dir():
        if clone_dir == clone_dir.parent:  # reached /
            raise exc_tp(f"No .git directory found above {abs_path}")
        clone_dir = clone_dir.parent
    rel_path = abs_path.relative_to(clone_dir)
    git_cmd = ("git", "-C", shlex.quote(str(clone_dir)))
    # Use abs_path to help git realize when a file is untracked (e.g. git won't traverse symlinks)
    try:
        assert not subprocess.check_output((*git_cmd, "status", "--porcelain", str(abs_path)), text=True).strip()
    except Exception:
        raise exc_tp(f"File {rel_path} has uncommitted changes or is untracked")
    sha = subprocess.check_output((*git_cmd, "rev-parse", "HEAD"), text=True).strip()
    try:
        subprocess.check_call((*git_cmd, "merge-base", "--is-ancestor", sha, r"@{u}"))
    except subprocess.CalledProcessError:
        raise exc_tp(f"Commit {sha[:7]} has not been pushed to the remote")
    repo_url = subprocess.check_output((*git_cmd, "remote", "get-url", "origin"), text=True).strip()
    parsed_url = urllib.parse.urlparse(repo_url)
    assert parsed_url.hostname
    pat = settings["configByHost"].get(parsed_url.hostname, {}).get("originUrlPat")
    if not pat:
        raise exc_tp(f"Unsupported host {parsed_url.hostname}")
    if not (match := re.match(pat, repo_url)):
        raise exc_tp(f"repo_url {repo_url} does not match pattern {pat}")
    return UrlData(host=parsed_url.hostname, org=match["org"], repo=match["repo"], path=str(rel_path), sha=sha)


class ShareHandler(APIHandler):
    @tornado.web.authenticated
    async def get(self):
        path = self.get_query_argument("path")
        data = _url_data_from_path(path, exc_tp=partial(tornado.web.HTTPError, 400))
        rawUrlTmpl = settings["configByHost"].get(data.host, {}).get("rawUrlTmpl")
        if not rawUrlTmpl:
            raise tornado.web.HTTPError(400, reason=f"Unsupported host {data.host}")
        raw_url = Template(rawUrlTmpl).substitute(asdict(data))
        open_url = f"{EXTENSION_NAME}/open?url={urllib.parse.quote(raw_url)}"
        # TODO: Is there an API we can call from here like
        # https://github.com/jupyterlab/jupyterlab/blob/431405/packages/coreutils/src/pageconfig.ts#L120
        # to get page_config["shareUrl"], as set here:
        # https://github.com/jupyterlab/jupyterlab/blob/431405e2/jupyterlab/labapp.py#L905
        # rather than resorting to this code:
        if os.environ.get("JUPYTERHUB_BASE_URL", ""):
            open_url = ujoin("hub/user-redirect", open_url)
        open_url = ujoin(f"{self.request.protocol}://{self.request.host}", open_url)
        self.finish(json.dumps({"url": open_url}))


class OpenHandler(APIHandler):
    @tornado.web.authenticated
    async def get(self):
        url = self.get_query_argument("url")
        parsed = urllib.parse.urlparse(url)
        # Avoid users being lured into opening and running random .py files from who knows where.
        if parsed.hostname not in settings["configByHost"]:
            raise tornado.web.HTTPError(400, reason=f"Unsupported host {parsed.host}")
        config = settings["configByHost"][parsed.hostname]
        content = _get_content(parsed, config)
        if content is None:
            raise tornado.web.HTTPError(400, reason=f"Download failed: {url}")
        filename = parsed.path.rpartition('/')[-1]
        nonce = int(time.time())
        path = pathlib.Path(f"{settings["baseDir"]}/{nonce}-{filename}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        logger.info("Downloaded %s to %s", url, path)
        self.redirect(f"/lab/tree/{str(path).lstrip("/")}")


def _get_content(parsed_url: urllib.parse.ParseResult, config: dict) -> bytes | None:
    url = parsed_url.geturl()
    allow_redirects = config.get("followRedirects", False)
    resp = requests.get(url, allow_redirects=allow_redirects)
    if resp.ok and not resp.is_redirect:
        return resp.content
    if not config.get("enableGitCredsLookup", False):
        logger.info("GET %s -> %s and git creds lookup disabled", url, resp.status_code)
        return None
    logger.info("GET %s -> %s, trying git creds lookup...", url, resp.status_code)
    port = {None: 443, "https": 443, "http": 80}[parsed_url.scheme] 
    username, password = _creds_from_git_store(parsed_url.scheme, parsed_url.netloc)
    quoted_username = urllib.parse.quote(username, safe="")
    quoted_password = urllib.parse.quote(password, safe="")
    netloc = f"{quoted_username}:{quoted_password}@{parsed_url.hostname}:{port}"
    url_with_creds = parsed_url._replace(netloc=netloc).geturl()
    url_with_creds_san = url_with_creds.replace(quoted_password, "****", 1)
    logger.info("git creds lookup succeeded, trying %r...", url_with_creds_san)
    resp = requests.get(url_with_creds, allow_redirects=allow_redirects)
    logger.info("GET %s -> %s", url_with_creds_san, resp.status_code)
    if resp.ok and not resp.is_redirect:
        return resp.content
    return None


# Cache the result of the git credential lookup indefinitely. Credentials are unlikely to change
# in between requests and the cache is cleared when the server is restarted.
@functools.cache
def _creds_from_git_store(protocol: str, host: str) -> tuple[str, str]:
    cmd = ("git", "credential", "fill")
    input_ = f"protocol={protocol}\nhost={host}\n"
    result = subprocess.run(cmd, input=input_, text=True, capture_output=True, check=True)
    credentials = {}
    for line in result.stdout.strip().split('\n'):
        if line and "=" in line:
            key, _, value = line.partition("=")
            credentials[key] = value
    username, password = credentials.get("username", ""), credentials.get("password", "")
    return username, password


def setup_handlers(web_app: ServerWebApplication) -> None:
    host_pattern = ".*$"
    base_url = web_app.settings["base_url"]
    handlers = [
        (ujoin(base_url, EXTENSION_NAME, "share"), ShareHandler),
        (ujoin(base_url, EXTENSION_NAME, "open"), OpenHandler),
    ]
    web_app.add_handlers(host_pattern, handlers)
