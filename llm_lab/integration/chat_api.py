"""Glue for the opt-in loopback preview. A corporate router supplies trusted owners."""

import os
import re
from collections import OrderedDict
from hashlib import sha256
from hmac import compare_digest
from http.cookies import CookieError, SimpleCookie
from secrets import token_urlsafe
from threading import Lock
from uuid import NAMESPACE_URL, uuid5

from pm import ai_store, brief, chat, live_runs
from pm.memory import redact

from . import chat_store

COOKIE = "paradiigma_qwen"
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
PROXY_USER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,95}")
PROXY_USER_HEADER = "X-Paradiigma-User"
PROXY_KEY_HEADER = "X-Paradiigma-Proxy-Key"
JOBS = OrderedDict()
JOBS_LOCK = Lock()
EDIT_FIELDS = {"title": 200, "hypothesis": 800, "verification": 1200}


def install():
    validate_owner_config()
    chat_store.install()


def validate_owner_config():
    mode = os.getenv("PM_QWEN_OWNER_MODE", "local")
    if mode not in {"local", "proxy"}:
        raise ValueError("Проверьте PM_QWEN_OWNER_MODE.")
    if mode == "proxy" and not TOKEN.fullmatch(os.getenv("PM_QWEN_PROXY_KEY", "")):
        raise ValueError("Для режима proxy настройте PM_QWEN_PROXY_KEY.")
    return mode


def request_owner(handler, *, create=False):
    if validate_owner_config() == "local":
        return local_owner(handler, create=create)
    users = handler.headers.get_all(PROXY_USER_HEADER, [])
    keys = handler.headers.get_all(PROXY_KEY_HEADER, [])
    if (len(users) != 1 or len(keys) != 1 or not PROXY_USER.fullmatch(users[0])
            or not TOKEN.fullmatch(keys[0]) or not compare_digest(keys[0], os.environ["PM_QWEN_PROXY_KEY"])):
        raise PermissionError("Для чата требуется аутентификация на прокси.")
    owner = sha256(f"proxy:{users[0]}".encode("ascii")).hexdigest()
    return owner, None


def local_owner(handler, *, create=False):
    jar = SimpleCookie()
    try:
        jar.load(handler.headers.get("Cookie", ""))
    except CookieError:
        jar = SimpleCookie()
    previous = jar.get(COOKIE)
    token = previous.value if previous and TOKEN.fullmatch(previous.value) else None
    response_cookie = None
    if token is None and create:
        token = token_urlsafe(32)
        response_cookie = f"{COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/api/qwen; Max-Age=2592000"
        if os.getenv("PM_QWEN_COOKIE_SECURE") == "true":
            response_cookie += "; Secure"
    return (sha256(token.encode()).hexdigest() if token else None), response_cookie


def route(path, owner, body):
    if path == "dialogs":
        return {"dialogs": chat_store.dialogs(owner) if owner else []}
    if path == "brief":
        return brief.release_brief(body["run_id"])
    if path == "brief-latest":
        for item in ai_store.recent():
            report = ai_store.run(item["run_id"])
            if report.get("request_context") and any(source["id"] == "release:current" and source["ref"] == body.get("release_id")
                   for source in report["evidence"]):
                return {"brief": brief.report_view(report)}
        return {"brief": None}
    if not owner:
        raise ValueError("Сначала откройте диалог.")
    if path == "new":
        return {"dialog_id": chat_store.create(owner)}
    identifier = body.get("dialog_id")
    if not isinstance(identifier, str) or not re.fullmatch(r"[a-f0-9]{32}", identifier):
        raise ValueError(chat_store.NOT_FOUND)
    if path == "history":
        return {"turns": chat_store.history(owner, identifier)}
    if path in {"turn", "approve"}:
        position = body.get("position")
        if type(position) is not int or position < 1:
            raise ValueError("Укажите сообщение диалога.")
        turn = chat_store.get_turn(owner, identifier, position)
        return turn["reply"] if path == "turn" else approve(turn, body)
    if path == "start":
        return start(owner, identifier, body)
    if path == "status":
        return status(owner, identifier, body.get("job_id"))
    raise KeyError("route")


def start(owner, identifier, body):
    chat.normalize_turn(body)
    version, prior = chat_store.state(owner, identifier)

    def analyze(request, *, progress):
        turn = chat.consult(request, previous=prior, progress=progress)
        if turn["previous_run_id"]:
            turn["reply"]["previous_position"] = version
        position = chat_store.append(owner, identifier, version, turn)
        return {"run_id": position}

    with JOBS_LOCK:
        for key, pair in list(JOBS.items()):
            try:
                phase = live_runs.status(key)["status"]
            except ValueError:
                del JOBS[key]
                continue
            if phase == "running" and pair == (owner, identifier):
                raise ValueError("Дождитесь текущего ответа в этом диалоге.")
        job = live_runs.start(body, analyze)["job_id"]
        JOBS[job] = owner, identifier
    return {"job_id": job}


def status(owner, identifier, job):
    if not isinstance(job, str):
        raise ValueError("Статус запуска недоступен.")
    with JOBS_LOCK:
        if JOBS.get(job) != (owner, identifier):
            raise ValueError("Статус запуска недоступен.")
        state = live_runs.status(job)
    position = state.pop("run_id", None)
    return state | ({"position": position, "reply": chat_store.get_turn(owner, identifier, position)["reply"]}
                    if state["status"] == "ready" else {})


def approve(turn, body):
    report = turn["report"]
    proposal = next((item for item in (report or {}).get("proposed_tasks", [])
                     if item["id"] == body.get("proposal_id")), None)
    if not proposal or body.get("confirm") is not True:
        raise ValueError("Проверьте черновик и подтвердите его сохранение.")
    draft = body.get("draft", {})
    if not isinstance(draft, dict) or draft.keys() - EDIT_FIELDS.keys():
        raise ValueError("Допустимо править название, гипотезу и способ проверки.")
    edited = {}
    for key, value in draft.items():
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= EDIT_FIELDS[key]:
            raise ValueError("Проверьте длину полей черновика.")
        edited[key] = redact(value.strip())

    # Publish only the approved task's evidence and Core plan to the team journal.
    run_id = uuid5(NAMESPACE_URL, f"paradiigma:qwen:{report['run_id']}:{proposal['id']}").hex
    refs = {*proposal["evidence_ids"], *proposal["contradictions"]}
    shared = {key: report[key] for key in ("llm_status", "data_mode", "sources_status", "effect_plan", "summary")}
    shared.update(run_id=run_id, evidence=[item for item in report["evidence"] if item["id"] in refs],
                  proposed_tasks=[proposal | edited], hypotheses=[], trace=[], usage={}, findings=[], seconds=0,
                  termination="approved_task", recommended_actions=[],
                  limitations=[], backlog_matches=proposal["existing_tasks"])
    request = {
        "run_id": run_id, "proposal_id": proposal["id"], "action": "accept",
        "target": body.get("target"), "control_min": body.get("control_min"),
        "idempotency_key": f"qwen-{run_id}",
    }
    return ai_store.decide(request, source_report=shared, edited=edited)
