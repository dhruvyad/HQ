"""Projects and workstreams: where an item lives, for the human.

A project is the repo its agent works in (from the session's cwd), unless the
item names one. A workstream is an ongoing line of work inside a project. An
agent may name one; otherwise it comes from the item's tags through the
registry below, most specific group first.
"""
import re

PROJECT_TAGS = {"hq": "hq", "mac": "mac"}
PROJECT_NAMES = {"noclick": "NoClick", "hq": "HQ", "ultron": "Ultron", "mac": "Mac", "wahooks": "WAHooks"}
OTHER = "other"
WORKSTREAMS = {
    "noclick": [
        ("appsumo", "AppSumo", "appsumo"),
        ("support", "Support", "support crisp crisp-channel crisp-support privacy abuse trust-safety postmortem customers outreach incident"),
        ("billing", "Billing and usage", "billing usage cost costs media-transcription referrals"),
        ("open-source", "Open source", "open-source oss"),
        ("sites", "Sites and domains", "apps app-sign-in domains apple legal machines hosting"),
        ("resources", "Resources", "resources storage repos browsers kernel databases serverless containers gcp email inbox vendors"),
        ("coordinator", "Coordinator", "coordinator bench compaction connect media chat"),
        ("agents", "Agents and builder", "agents openclaw opencode agent-runtime builder ai-builder canvas"),
        ("growth", "Growth", "growth seo marketing landing analytics mobile launch docs branding"),
        ("platform", "Platform SDK", "platform-sdk sdk platform npm pypi publish users humans phone-accounts primitives mcp"),
        ("integrations", "Integrations", "integrations apify linkedin microsoft instagram whatsapp wahooks credentials connections onboarding phone"),
        ("engineering", "Engineering", "frontend perf safari ux design shell product bug release staging ci tests e2e testing prod-frontend "
                                       "hotpatch vercel modal cloudflare security auth infra area51 relay backend reliability prod prod-db logs "
                                       "config tooling staff-tools staff-debug"),
    ],
    "hq": [("connections", "Connections", "connections"), ("design", "Design", "design ui"), ("release", "Release and CI", "ci")],
    "ultron": [("briefs", "Market briefs", "rwanda-market-briefs"), ("pilot", "Pilot", "pilot-briefings pilot-group"),
               ("family", "Family", "family mamma-health papa-health unitech"), ("setup", "Setup", "setup noclick-queue ultron")],
}


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")


def repo_of(cwd):
    # ~/code/noclick5 and ~/.codex/worktrees/f48e/noclick both belong to "noclick".
    match = re.search(r"/(?:code|worktrees/[^/]+)/([A-Za-z]+?)\d*(?:-[^/]*)?(?:/|$)", cwd or "")
    return match.group(1).lower() if match else None


def project_of(item, cwd):
    if item.get("project"):
        return slug(item["project"])
    for tag in item.get("tags", []):
        if tag in PROJECT_TAGS:
            return PROJECT_TAGS[tag]
    return repo_of(cwd) or OTHER


def project_name(project, given=""):
    return PROJECT_NAMES.get(project) or given or ("Other" if project == OTHER else project.replace("-", " ").title())


def workstream_of(item, project):
    """Return (id, name). An agent's own choice wins; tags decide otherwise."""
    given = item.get("workstream")
    registry = WORKSTREAMS.get(project, [])
    if given:
        wanted = slug(given)
        for id, name, _ in registry:
            if wanted in (id, slug(name)):
                return id, name
        return wanted, given
    tags = set(item.get("tags", []))
    for id, name, aliases in registry:
        if tags & set(aliases.split()):
            return id, name
    return OTHER, "Other"


def place(item, cwd):
    project = project_of(item, cwd)
    workstream, name = workstream_of(item, project)
    return {"project": project, "project_name": project_name(project, item.get("project", "")),
            "workstream": workstream, "workstream_name": name}


def index(items):
    """Every project's workstreams in use, for agents choosing where to report."""
    result = {}
    for item in items:
        project = result.setdefault(item["project"], {"name": item["project_name"], "workstreams": {}})
        project["workstreams"][item["workstream"]] = item["workstream_name"]
    for id, streams in WORKSTREAMS.items():
        project = result.setdefault(id, {"name": project_name(id), "workstreams": {}})
        for stream, name, _ in streams:
            project["workstreams"].setdefault(stream, name)
    return {id: {"name": p["name"], "workstreams": sorted(p["workstreams"].values())} for id, p in sorted(result.items())}
