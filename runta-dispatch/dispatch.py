#!/usr/bin/env python3
"""Dispatch a GitHub issue to Pi in a Runta runtime."""
from __future__ import annotations
import json, os, re, shlex, sys, time, urllib.error, urllib.parse, urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

RUNTA_API = "https://api.runta.com"
MARKER_PREFIX = "<!-- runta-dispatcher:issue="
ALLOWED_PERMISSIONS = {"admin", "maintain", "write"}

class DispatchError(RuntimeError): pass

def log(msg:str)->None:
    print(msg, flush=True)

def parse_follow(raw:str,offset:int)->tuple[str,int,str]:
    lines=raw.splitlines()
    state="MISSING"; nxt=offset; start=0
    for i,line in enumerate(lines[:8]):
        if line.startswith("STATE:"): state=line.split(":",1)[1] or "MISSING"
        elif line.startswith("OFFSET:"):
            try: nxt=int(line.split(":",1)[1])
            except ValueError: nxt=offset
            start=i+1
            break
    chunk="\n".join(lines[start:])
    if chunk: nxt=offset+len(chunk.encode())
    return state, nxt, chunk


@dataclass(frozen=True)
class SecretRule:
    host: str
    path: str
    secret: str
    header: str
    template: str

@dataclass(frozen=True)
class AgentConfig:
    provider: str
    model: str
    base_url: str

@dataclass(frozen=True)
class Config:
    label: str
    command: str
    runtime: str
    agent: AgentConfig
    secrets: tuple[SecretRule, ...]
    cpus: int
    memory_mib: int
    timeout_minutes: int

def parse_scalar(value: str) -> Any:
    value = value.strip()
    if not value: return ""
    if value[:1] in {'"', "'"} and value[-1:] == value[0]: return value[1:-1]
    if re.fullmatch(r"-?\d+", value): return int(value)
    if value in {"true", "false"}: return value == "true"
    return value

def parse_yaml(path: Path) -> dict[str, Any]:
    """Parse this file's small map/list-of-maps YAML subset."""
    root: dict[str, Any] = {}
    stack: list[tuple[int, Any]] = [(-1, root)]
    lines = path.read_text(encoding="utf-8").splitlines()
    for number, raw in enumerate(lines, 1):
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip(): continue
        indent = len(line) - len(line.lstrip())
        if indent % 2: raise DispatchError(f"unsupported indentation at {path}:{number}")
        text = line.strip()
        while stack[-1][0] >= indent: stack.pop()
        parent = stack[-1][1]
        if text.startswith("- "):
            if not isinstance(parent, list): raise DispatchError(f"invalid list at {path}:{number}")
            item: dict[str, Any] = {}
            parent.append(item)
            text = text[2:]
            if text:
                match = re.fullmatch(r"([A-Za-z_][\w-]*):\s*(.*)", text)
                if not match: raise DispatchError(f"invalid config at {path}:{number}")
                item[match[1]] = parse_scalar(match[2])
            stack.append((indent, item))
            continue
        match = re.fullmatch(r"([A-Za-z_][\w-]*):(?:\s*(.*))?", text)
        if not match or not isinstance(parent, dict): raise DispatchError(f"invalid config at {path}:{number}")
        key, value = match[1], match[2] or ""
        if value: parent[key] = parse_scalar(value); continue
        next_text = next((x.strip() for x in lines[number:] if x.strip() and not x.lstrip().startswith("#")), "")
        child: Any = [] if next_text.startswith("- ") else {}
        parent[key] = child
        stack.append((indent, child))
    return root

def input_config() -> Config:
    try:
        base_url = os.environ["INPUT_BASE_URL"].rstrip("/")
        host = urllib.parse.urlparse(base_url).hostname or ""
        config = Config(
            os.environ.get("INPUT_LABEL", "runta"),
            os.environ.get("INPUT_COMMAND", "/runta"),
            os.environ["INPUT_RUNTIME"],
            AgentConfig(
                os.environ.get("INPUT_PROVIDER", "runta"),
                os.environ["INPUT_MODEL"],
                base_url,
            ),
            (SecretRule(
                host,
                (urllib.parse.urlparse(base_url).path.rstrip("/") or "") + "/*",
                os.environ["INPUT_PROVIDER_SECRET"],
                "Authorization",
                "Bearer ${credential}",
            ),),
            int(os.environ.get("INPUT_CPUS", "2")),
            int(os.environ.get("INPUT_MEMORY_MIB", "4096")),
            int(os.environ.get("INPUT_TIMEOUT_MINUTES", "90")),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise DispatchError("missing or malformed action input") from error
    if not config.command.startswith("/") or not host:
        raise DispatchError("invalid action input")
    return config

class JsonClient:
    def __init__(self, base_url: str, token: str): self.base_url, self.token = base_url.rstrip("/"), token
    def request(self, method: str, path: str, body: Any = None, timeout: int = 60) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Accept":"application/json", "Authorization":f"Bearer {self.token}", "User-Agent":"runta-dispatcher/0.1"}
        if data: headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw=response.read(); return json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            raw=error.read().decode("utf-8","replace")
            try:
                detail=json.loads(raw).get("error",{}); message=detail.get("message") or detail.get("code") or raw
            except json.JSONDecodeError: message=raw
            raise DispatchError(f"{method} {path} failed ({error.code}): {message}") from error
        except urllib.error.URLError as error: raise DispatchError(f"{method} {path} failed: {error.reason}") from error

class GitHub:
    def __init__(self, api_url:str, repository:str, token:str): self.client, self.repository=JsonClient(api_url,token),repository
    def request(self, method:str,path:str,body:Any=None)->Any:return self.client.request(method,f"/repos/{self.repository}{path}",body)
    def permission(self,username:str)->str:
        data=self.request("GET",f"/collaborators/{urllib.parse.quote(username,safe='')}/permission")
        return str(data.get("user",{}).get("permissions",{}).get("role_name") or data.get("permission",""))
    def issue_comments(self,n:int)->list[dict[str,Any]]:
        out=[]; page=1
        while True:
            batch=self.request("GET",f"/issues/{n}/comments?per_page=100&page={page}"); out.extend(batch)
            if len(batch)<100:return out
            page+=1
    def comment(self,n:int,body:str)->None:self.request("POST",f"/issues/{n}/comments",{"body":body})
    def issue_context(self,n:int,issue:dict[str,Any])->dict[str,Any]:
        return {"number":n,"title":issue.get("title",""),"body":issue.get("body") or "","url":issue.get("html_url",""),"comments":[{"author":x.get("user",{}).get("login","unknown"),"body":x.get("body") or ""} for x in self.issue_comments(n) if MARKER_PREFIX not in (x.get("body") or "")]}

class Runta:
    def __init__(self,token:str):self.client=JsonClient(RUNTA_API,token)
    def find_runtime(self,name:str)->dict[str,Any]|None:
        after=None
        while True:
            query="?limit=100"+("&after="+urllib.parse.quote(after) if after else ""); response=self.client.request("GET","/v1/runtimes"+query)
            for runtime in response.get("data",[]):
                if runtime.get("display_name")==name:return runtime
            p=response.get("pagination") or {}; after=p.get("next") or p.get("next_cursor")
            if not after:return None
    def create_runtime(self,config:Config)->dict[str,Any]:
        response=self.client.request("POST","/v1/runtimes",{"name":config.runtime,"resources":{"requests":{"vcpus":config.cpus,"memory_mib":config.memory_mib},"limits":{"memory_mib":config.memory_mib}},"egress_policy":{"mode":"denylist","allowed_hosts":[],"denied_hosts":[]},"idle_policy":{"mode":"disabled"}},timeout=300)
        return response["data"]
    def get_runtime(self,runtime_id:str)->dict[str,Any]:return self.client.request("GET",f"/v1/runtimes/{urllib.parse.quote(runtime_id,safe='')}")["data"]
    def ensure_ready(self,runtime:dict[str,Any],timeout_seconds:int=600)->dict[str,Any]:
        runtime_id=str(runtime.get("id") or runtime.get("display_name")); status=runtime.get("status")
        if status in {"paused","suspended"}:self.client.request("POST",f"/v1/runtimes/{runtime_id}/resume")
        elif status=="shutdown":self.client.request("POST",f"/v1/runtimes/{runtime_id}/start")
        elif status in {"error","crashed","deleting"}:raise DispatchError(f"runtime {runtime_id} is unusable ({status})")
        deadline=time.monotonic()+timeout_seconds
        while time.monotonic()<deadline:
            runtime=self.get_runtime(runtime_id)
            if runtime.get("status")=="running":return runtime
            if runtime.get("status") in {"error","crashed","deleting"}:raise DispatchError(f"runtime {runtime_id} became {runtime.get('status')}")
            time.sleep(5)
        raise DispatchError(f"runtime {runtime_id} was not ready after {timeout_seconds}s")
    def upsert_secret(self,name:str,value:str)->dict[str,Any]:
        response=self.client.request("PUT",f"/v1/secrets/{urllib.parse.quote(name,safe='')}",{"value":value})
        return response.get("data",response)
    def delete_secret(self,name:str)->None:
        self.client.request("DELETE",f"/v1/secrets/{urllib.parse.quote(name,safe='')}")
    def list_secret_rules(self,runtime_id:str)->list[dict[str,Any]]:
        response=self.client.request("GET",f"/v1/runtimes/{urllib.parse.quote(runtime_id,safe='')}/secret-stubs")
        return response.get("data",response) if isinstance(response,dict) else response
    def create_secret_rule(self,runtime_id:str,rule:SecretRule)->dict[str,Any]:
        response=self.client.request("POST",f"/v1/runtimes/{urllib.parse.quote(runtime_id,safe='')}/secret-stubs",{"host":rule.host,"path":rule.path,"stub_value":rule.secret,"config":{"headerName":rule.header,"valueFormat":rule.template}})
        return response.get("data",response)
    def delete_secret_rule(self,stub_id:str)->None:
        self.client.request("DELETE",f"/v1/secret-stubs/{urllib.parse.quote(stub_id,safe='')}")
    def ensure_secret_rules(self,runtime_id:str,rules:tuple[SecretRule,...])->list[str]:
        existing=self.list_secret_rules(runtime_id); created=[]
        for rule in rules:
            match=None
            for x in existing:
                try: stub_config=json.loads(x.get("stub_config_json") or "{}")
                except json.JSONDecodeError: stub_config={}
                same_target=(x.get("host_pattern"), x.get("path_pattern") or "/*")==(rule.host, rule.path)
                same_value=(x.get("stub_value_display_name"), stub_config.get("headerName"), stub_config.get("valueFormat"))==(rule.secret, rule.header, rule.template)
                if same_target and same_value:
                    match=x; break
                if same_target and x.get("id"):
                    try:self.delete_secret_rule(str(x["id"]))
                    except DispatchError as error: log(f"cleanup warning: {error}")
            if match is None:
                created_rule=self.create_secret_rule(runtime_id,rule)
                if created_rule.get("id"): created.append(str(created_rule["id"]))
        return created
    def exec(self,runtime_id:str,script:str,timeout_seconds:int)->dict[str,Any]:
        last=None
        for attempt in range(4):
            try:
                response=self.client.request("POST",f"/v1/runtimes/{urllib.parse.quote(runtime_id,safe='')}/exec",{"command":"sh","args":["-lc",script],"timeout_secs":timeout_seconds,"max_output_bytes":4_194_304},timeout=min(timeout_seconds+15,90))
                return response["data"]
            except DispatchError as error:
                last=error
                if "504" not in str(error) or attempt==3: raise
                time.sleep(2 ** attempt)
        raise last
    def follow_agent(self,runtime_id:str,timeout_seconds:int)->str:
        deadline=time.monotonic()+timeout_seconds; offset=0
        while time.monotonic()<deadline:
            probe="\n".join([
                "set +e",
                "if [ -f /tmp/runta-agent.exit ]; then echo STATE:EXIT:$(tr -d '[:space:]' < /tmp/runta-agent.exit);",
                "elif [ -f /tmp/runta-agent.pid ] && kill -0 $(tr -d '[:space:]' < /tmp/runta-agent.pid) 2>/dev/null; then echo STATE:RUNNING;",
                "else echo STATE:DEAD; fi",
                f"echo OFFSET:{offset}",
                f"if [ -f /tmp/runta-agent.out ]; then tail -c +{offset+1} /tmp/runta-agent.out | head -c 65536; fi",
            ])
            status=self.exec(runtime_id,probe,15)
            raw=(status.get("stdout") or "")+(status.get("stderr") or "")
            state,offset,chunk=parse_follow(raw,offset)
            if chunk: log(chunk.rstrip("\n"))
            else: log(f"agent {state.lower()} ({offset} bytes)")
            if state.startswith("EXIT"):
                code=int(state.split(":")[-1] or "1")
                if code!=0: raise DispatchError(f"agent exited with status {code}")
                return raw
            if state=="DEAD": raise DispatchError("agent process disappeared before writing an exit status")
            time.sleep(2)
        raise DispatchError(f"agent did not finish after {timeout_seconds}s")


def marker(n:int)->str:return f"{MARKER_PREFIX}{n} -->"
def command_runtime(body:str,config:Config)->str|None:
    first=next((x.strip() for x in body.splitlines() if x.strip()),""); m=re.fullmatch(re.escape(config.command)+r"(?:\s+([A-Za-z0-9][A-Za-z0-9._-]{0,62}))?",first)
    return (m.group(1) or config.runtime) if m else None
def trigger(event:dict[str,Any],config:Config)->tuple[str,str]|None:
    if "comment" in event and event.get("action")=="created":
        runtime=command_runtime(event["comment"].get("body") or "",config); return (event["comment"]["user"]["login"],runtime) if runtime else None
    if "label" in event and event.get("action")=="labeled" and event["label"].get("name")==config.label:return event.get("sender",{}).get("login",""),config.runtime
    return None

def pi_config(agent:AgentConfig)->str:
    return json.dumps({"providers":{agent.provider:{"baseUrl":agent.base_url,"api":"openai-responses","apiKey":"OPENAI_API_KEY","models":[{"id":agent.model,"name":agent.model}]}},"defaultProvider":agent.provider,"defaultModel":agent.model},indent=2)+"\n"
def job_script(config:Config,repository:str,issue:dict[str,Any])->str:
    number=int(issue["number"]); branch=f"runta/issue-{number}"
    prompt=f"""You are implementing GitHub issue #{number} in {repository}.\n\nIssue context:\n{json.dumps(issue,ensure_ascii=False,indent=2)}\n\nWork autonomously. Inspect repository guidance before changing code. Implement the issue, commit, push branch {branch}, and open a pull request against the default branch. The PR body must include `Closes #{number}`. Finally comment on issue #{number} with the PR URL, or with a concise failure explanation if you cannot finish. Do not expose credentials. GitHub credentials are egress-injected. GH_TOKEN/GITHUB_TOKEN are placeholders; do not run gh auth status. Use gh pr create and gh issue comment anyway."""
    rendered=" ".join(shlex.quote(x) for x in ["pi","--verbose","--mode","text","-p",prompt])
    return "\n".join([
        "set -eu",
        "export GH_TOKEN=runta-secret-stub",
        "export GITHUB_TOKEN=runta-secret-stub",
        "export OPENAI_API_KEY=runta-secret-stub",
        "export GIT_TERMINAL_PROMPT=0",
        "export PYTHONUNBUFFERED=1",
        "command -v pi >/dev/null 2>&1 || { command -v npm >/dev/null 2>&1 || { curl -fsSL https://deb.nodesource.com/setup_22.x | bash -; apt-get install -y nodejs; }; npm install -g @mariozechner/pi-coding-agent; }",
        "command -v gh >/dev/null 2>&1 || { curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg && echo \"deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main\" > /etc/apt/sources.list.d/github-cli.list && apt-get update && apt-get install -y gh; }",
        "mkdir -p ~/.pi/agent",
        f"if [ ! -e ~/.pi/agent/models.json ]; then printf %s {shlex.quote(pi_config(config.agent))} > ~/.pi/agent/models.json; fi",
        "rm -rf /workspace/runta-dispatch",
        f"gh repo clone {shlex.quote(repository)} /workspace/runta-dispatch",
        "cd /workspace/runta-dispatch",
        f"git checkout -b {shlex.quote(branch)}",
        "git config user.name 'runta-dispatcher'",
        "git config user.email 'runta-dispatcher@users.noreply.github.com'",
        rendered,
    ])
def start_script(job:str)->str:
    quoted=shlex.quote(job+"\nprintf %s $? > /tmp/runta-agent.exit")
    return "\n".join([
        "set -eu",
        "rm -f /tmp/runta-agent.out /tmp/runta-agent.exit /tmp/runta-agent.pid /tmp/runta-job.sh",
        ": > /tmp/runta-agent.out",
        f"printf %s {quoted} > /tmp/runta-job.sh",
        "chmod +x /tmp/runta-job.sh",
        "nohup script -q -f -c 'stdbuf -oL -eL sh /tmp/runta-job.sh' /tmp/runta-agent.out >/dev/null 2>&1 &",
        "echo $! > /tmp/runta-agent.pid",
        "echo started",
    ])
def github_secret_name() -> str:
    override=os.environ.get("INPUT_RUNTA_GH_TOKEN_NAME","").strip()
    if override:return override
    repository_id=os.environ.get("GITHUB_REPOSITORY_ID","repo")
    run_id=os.environ.get("GITHUB_RUN_ID","run")
    attempt=os.environ.get("GITHUB_RUN_ATTEMPT","1")
    return f"github-{repository_id}-{run_id}-{attempt}"

def github_rules(repository:str,api_secret:str,git_secret:str)->tuple[SecretRule,...]:
    owner,repo=repository.split("/",1)
    return (
        SecretRule("api.github.com","/*",api_secret,"Authorization","Bearer ${credential}"),
        SecretRule("github.com",f"/{owner}/{repo}*",git_secret,"Authorization","Basic ${credential}"),
    )

def main()->int:
    config=input_config(); event=json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    if event.get("issue",{}).get("pull_request"):return 0
    selected=trigger(event,config)
    if not selected:return 0
    actor,runtime_name=selected; config=replace(config,runtime=runtime_name)
    github_token,runta_token=os.environ.get("GITHUB_TOKEN",""),os.environ.get("RUNTA_TOKEN","")
    if not github_token or not runta_token:raise DispatchError("GITHUB_TOKEN and RUNTA_TOKEN are required")
    repository=os.environ["GITHUB_REPOSITORY"]; issue=event["issue"]; n=int(issue["number"]); github=GitHub(os.environ.get("GITHUB_API_URL","https://api.github.com"),repository,github_token)
    if github.permission(actor) not in ALLOWED_PERMISSIONS:github.comment(n,f"Runta dispatch denied: @{actor} does not have write permission.");return 0
    issue_marker=marker(n)
    if any(issue_marker in (x.get("body") or "") for x in github.issue_comments(n)):print(f"issue #{n} was already dispatched");return 0
    runta=Runta(runta_token); secret_name=github_secret_name(); git_secret_name=secret_name+"-git"; uploaded=[]; created_stubs=[]
    try:
        log(f"uploading ephemeral GitHub credentials as Runta secrets {secret_name} and {git_secret_name}")
        runta.upsert_secret(secret_name,github_token); uploaded.append(secret_name)
        git_basic=__import__("base64").b64encode(("x-access-token:"+github_token).encode()).decode()
        runta.upsert_secret(git_secret_name,git_basic); uploaded.append(git_secret_name)
        runtime=runta.find_runtime(config.runtime); created=runtime is None
        if created:
            log(f"creating runtime {config.runtime}")
            runtime=runta.create_runtime(config)
        else:
            log(f"reusing runtime {config.runtime}")
        runtime_id=str(runtime.get("id") or runtime.get("display_name") or config.runtime)
        github.comment(n,f"{issue_marker}\n🚀 Dispatched to Runta runtime `{config.runtime}` (`{runtime_id}`).")
        log("waiting for runtime")
        runtime=runta.ensure_ready(runtime); runtime_id=str(runtime.get("id") or runtime_id)
        log("ensuring secret stubs")
        created_stubs=runta.ensure_secret_rules(runtime_id,config.secrets+github_rules(repository,secret_name,git_secret_name))
        job=job_script(config,repository,github.issue_context(n,issue))
        starter=start_script(job)
        if github_token in job or github_token in starter:raise DispatchError("GitHub credential reached runtime script")
        log("starting detached job")
        start=runta.exec(runtime_id,starter,20)
        log(((start.get("stdout") or "")+(start.get("stderr") or ""))[-500:])
        if int(start.get("exit_code",1))!=0:raise DispatchError("failed to start detached job")
        log("following job output")
        runta.follow_agent(runtime_id,config.timeout_minutes*60)
        github.comment(n,f"✅ Runta agent finished in runtime `{config.runtime}`. It should have opened or updated the pull request.");return 0
    except Exception as error:
        github.comment(n,f"❌ Runta dispatch failed in runtime `{config.runtime}`: {str(error)[:1000]}");raise
    finally:
        log("cleaning ephemeral GitHub stubs and secrets")
        for stub_id in created_stubs:
            try:runta.delete_secret_rule(stub_id)
            except DispatchError as cleanup_error:print(f"runta-dispatcher: cleanup warning: {cleanup_error}",file=sys.stderr)
        for name in uploaded:
            try:runta.delete_secret(name)
            except DispatchError as cleanup_error:print(f"runta-dispatcher: cleanup warning: {cleanup_error}",file=sys.stderr)

if __name__=="__main__":
    try:raise SystemExit(main())
    except DispatchError as error:print(f"runta-dispatcher: {error}",file=sys.stderr);raise SystemExit(1)
