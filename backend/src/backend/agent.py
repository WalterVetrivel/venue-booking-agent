"""The chat agent: a small LangGraph graph that turns a conversation into a venue request.

Each turn: understand the message (structured output), then one of
  find_mail search the faculty's own Gmail and show the matches (nothing is read yet),
  use_mail  read the one mail they picked and take the event details from it,
  clarify   ask for the fields still missing,
  lookup    rank suitable free venues and explain the options,
  propose   restate a hold, change or cancellation and wait for a yes,
  execute   carry out the action the faculty just said yes to,
  answer    questions, with a short tool loop over the knowledge and venue tools.

The agent's only hands are the MCP tools (backend.mcp_servers), called in-process as the signed-in
faculty. It can't approve a booking or act for anyone else, and nothing changes until the faculty
says yes to the exact action restated to them. See docs/PLAN.md, "Agent design and booking workflow".
"""

import json
from datetime import date, datetime, time, timedelta
from typing import Any, Literal, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph
from mcp.shared.memory import create_connected_server_and_client_session
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend import booking as bk
from backend import mcp_servers
from backend.config import get_settings
from backend.models import ChatSession, Faculty
from backend.recommend import BookingRequest

HISTORY_TURNS = 10
ANSWER_STEPS = 4
INTENTS = ("book", "mail", "list", "change", "cancel", "question", "yes", "no", "chat")


class Turn(BaseModel):
    """What the latest message means. Only details stated or changed in that message go in `request`."""

    intent: Literal[INTENTS] = Field(description=(
        "book: wants a venue, gives or changes event details, or picks a venue; mail: wants the details taken "
        "from their mail or Gmail, or picks one of the mails shown; list: asks about their bookings; "
        "change: move an existing booking; cancel: cancel an existing booking; question: anything about venues, "
        "rules or past events; yes / no: answers a confirmation; chat: greetings and the rest"))
    request: BookingRequest = Field(default_factory=BookingRequest)
    venue: str | None = Field(None, description="Venue code or name the faculty picked or asked to book")
    booking_id: int | None = Field(None, description="Which existing booking, for change or cancel")
    new_venue: str | None = None
    new_date: date | None = None
    new_start_time: time | None = None
    new_end_time: time | None = None
    reason: str | None = Field(None, description="Reason given for a cancellation")
    question: str | None = Field(None, description="The question as a standalone sentence, for question or chat")
    mail_query: str | None = Field(None, description="For mail: a few Gmail search words, e.g. 'guest lecture AI'")
    mail_pick: int | None = Field(None, description="For mail: the number of the mail shown last that they picked")


class ToolFailed(Exception):
    pass


class Tools:
    """MCP tools, called in-process as one faculty member."""

    def __init__(self, faculty_id: int):
        self.faculty_id = faculty_id

    async def _session(self, server: str, fn):
        token = mcp_servers.acting_faculty_id.set(self.faculty_id)
        try:
            async with create_connected_server_and_client_session(mcp_servers.SERVERS[server]._mcp_server) as client:
                return await fn(client)
        finally:
            mcp_servers.acting_faculty_id.reset(token)

    async def call(self, server: str, tool: str, **args) -> Any:
        res = await self._session(server, lambda c: c.call_tool(tool, args))
        text = res.content[0].text if res.content else ""
        if res.isError:
            raise ToolFailed(text.removeprefix(f"Error executing tool {tool}: "))
        if res.structuredContent is not None:
            return res.structuredContent.get("result", res.structuredContent)
        return json.loads(text) if text else None

    async def specs(self, wanted: dict[str, list[str]]) -> list[dict]:
        """Name, description and JSON schema of the tools the question-answering loop may use."""
        out = []
        for server, names in wanted.items():
            listed = await self._session(server, lambda c: c.list_tools())
            out += [{"server": server, "name": t.name, "description": t.description, "schema": t.inputSchema}
                    for t in listed.tools if t.name in names]
        return out


ANSWER_TOOLS = {"booking": ["list_venues", "check_availability", "list_my_bookings"],
                "knowledge": ["search_knowledge", "get_venue_rules"], "recommender": ["get_usage_patterns"],
                "drive": ["list_documents", "read_document"]}


class AgentLLM(Protocol):
    def understand(self, context: str, history: list[dict], message: str) -> Turn: ...

    def write(self, instructions: str, data: Any, history: list[dict]) -> str: ...

    async def answer(self, question: str, history: list[dict], tools: list[dict], call) -> str: ...


class State(TypedDict, total=False):
    message: str
    action: dict | None  # a button press: {"pick": code}, {"confirm": bool}, {"find_mail": true}, {"use_mail": id}
    history: list[dict]
    draft: dict
    options: list[str]
    mails: list[dict]  # mails shown last: id, subject, sender
    mail_id: str | None
    pending: dict | None
    bookings: list[dict]
    turn: dict
    reply: str
    data: dict | None


# --- wording --------------------------------------------------------------------------------------------

ASK = {"expected_headcount": "how many people you expect", "dates": "which date (or dates) it's on",
       "start_time": "what time it starts and ends", "end_time": "what time it starts and ends",
       "format": "whether it's a talk or a hands-on session"}


def _when(r: BookingRequest) -> str:
    days = ", ".join(f"{d:%a %d %b}" for d in sorted(r.dates))
    return f"{days}, {r.start_time:%H:%M} to {r.end_time:%H:%M}"


def _clarify(r: BookingRequest) -> str:
    asks = list(dict.fromkeys(ASK[f] for f in r.missing()))
    lead = "Happy to help with that. " if not any(r.model_dump(exclude_defaults=True).values()) else ""
    joined = asks[0] if len(asks) == 1 else ", ".join(asks[:-1]) + " and " + asks[-1]
    return f"{lead}Could you tell me {joined}?"


def _summary(r: BookingRequest) -> str:
    """The details of a request in a phrase, to show what was read from a mail."""
    parts = [f'"{r.title}"' if r.title else None,
             f"{r.expected_headcount} people" if r.expected_headcount else None, r.format,
             ", ".join(f"{d:%a %d %b %Y}" for d in sorted(r.dates)) if r.dates else None,
             f"{r.start_time:%H:%M} to {r.end_time:%H:%M}" if r.start_time and r.end_time else None,
             f"{r.guest_profile.replace('_', ' ')} guest" if r.guest_profile else None,
             f"preferring {r.preferred_venue}" if r.preferred_venue else None]
    return ", ".join(p for p in parts if p) or "nothing about a booking"


def options_text(out: dict, r: BookingRequest) -> str:
    """The recommendation in plain words, straight from the tool result. Used when the LLM isn't reachable."""
    if not out["options"]:
        lines = [f"Nothing that suits {r.expected_headcount} people is free on {_when(r)}."]
        if out.get("nearest_free_dates"):
            lines.append("The nearest days with a suitable venue free at the same time are "
                         + "; ".join(f"{date.fromisoformat(d['date']):%a %d %b} ({', '.join(d['venues'])})"
                                     for d in out["nearest_free_dates"]) + ".")
        return " ".join(lines)
    lines = [f"These suit {r.expected_headcount} people on {_when(r)}:"]
    for i, o in enumerate(out["options"], 1):
        line = f"{i}. {o['name']} ({o['code']}): {'; '.join(o['reasons'])}."
        for c in o["soft_conflicts"]:
            line += f" Heads-up: {c['note']}"
        lines.append(line)
    pref = out.get("preferred")
    if pref and pref.get("unavailable"):
        lines.append(f"{pref['code']} can't be used: {pref['unavailable']}.")
    elif pref and pref["code"] not in [o["code"] for o in out["options"]]:
        notes = " ".join(c["note"] for c in pref["soft_conflicts"])
        if notes:
            lines.append(f"{pref['code']}, which you asked for, is free. Heads-up: {notes} "
                         "That's only a warning; you can still request it.")
        else:
            lines.append(f"{pref['code']}, which you asked for, is free too.")
    lines.append("Which one should I request?")
    return "\n".join(lines)


RECOMMEND_PROMPT = """Write the reply to a faculty member who asked for a venue, using only the data below.
Lead with the best option, then the others (at most three in all). For each, give its reasons in one short clause.
If an option has a soft conflict, say it plainly with its evidence (years and what the venue is usually used for),
and point to an option without one. If the faculty preferred a venue that can't be used, say why. If there are no
options, offer the nearest free dates. End by asking which one to request. Plain text, no tables, under 130 words.
Don't claim anything the data doesn't show."""


def understand_context(s: State, bookings: list[dict], venues: list[dict]) -> str:
    """What the model needs besides the transcript: dates, venues, the draft and pending action."""
    today = bk._now().date()
    days = ", ".join(f"{today + timedelta(d):%a %d %b %Y}" for d in range(21))
    return "\n".join([
        f"Today is {today:%A %d %B %Y}. The next three weeks: {days}.",
        "Campus time is IST. Write dates as YYYY-MM-DD and times as HH:MM (24-hour).",
        "Venues (give the code): " + "; ".join(f"{v['code']} = {v['name']}, {v['type'].replace('_', ' ')}, "
                                               f"{v['capacity']} seats" for v in venues),
        f"Booking draft so far: {json.dumps(s.get('draft', {}))}",
        f"Venues offered last: {', '.join(s.get('options', [])) or 'none'} (\"the first one\" means the first).",
        "Mails shown last: " + ("; ".join(f"{i}. {m['subject']} (from {m['sender']})"
                                          for i, m in enumerate(s.get("mails", []), 1)) or "none"),
        f"Waiting for a yes or no on: {s['pending']['summary'] if s.get('pending') else 'nothing'}",
        "The faculty's upcoming bookings: " + (json.dumps([{k: b[k] for k in ("booking_id", "title", "venue",
                                                                             "start", "status")}
                                                           for b in bookings]) if bookings else "none"),
    ])


# --- the graph ------------------------------------------------------------------------------------------


class Agent:
    def __init__(self, llm: AgentLLM, tools: Tools):
        self.llm, self.tools = llm, tools
        g = StateGraph(State)
        for name in ("understand", "find_mail", "use_mail", "clarify", "lookup", "propose_hold", "propose_manage",
                     "execute", "decline", "list_bookings", "answer"):
            g.add_node(name, getattr(self, name))
        g.add_edge(START, "understand")
        g.add_conditional_edges("understand", self.route)
        for name in ("find_mail", "use_mail", "clarify", "lookup", "propose_hold", "propose_manage", "execute",
                     "decline", "list_bookings", "answer"):
            g.add_edge(name, END)
        self.graph = g.compile()

    async def run(self, state: State) -> State:
        return await self.graph.ainvoke(state)

    # Understand -------------------------------------------------------------------------------------------

    async def understand(self, s: State) -> dict:
        bookings = await self.tools.call("booking", "list_my_bookings")
        action = s.get("action") or {}
        if "confirm" in action:
            turn = Turn(intent="yes" if action["confirm"] else "no")
        elif "pick" in action:
            turn = Turn(intent="book", venue=action["pick"])
        elif "find_mail" in action or "use_mail" in action:
            turn = Turn(intent="mail")
        else:
            venues = await self.tools.call("booking", "list_venues")
            turn = self.llm.understand(understand_context(s, bookings, venues),
                                       s.get("history", [])[-HISTORY_TURNS:], s["message"])
        out: dict = {"turn": turn.model_dump(mode="json"), "bookings": bookings, "data": None, "mail_id": None}
        mails = s.get("mails", [])
        if "use_mail" in action:
            out["mail_id"] = action["use_mail"]
        elif turn.intent == "mail" and turn.mail_pick and 0 < turn.mail_pick <= len(mails):
            out["mail_id"] = mails[turn.mail_pick - 1]["id"]
        if turn.intent == "book":
            stated = {k: v for k, v in turn.request.model_dump(mode="json").items() if v not in (None, [], "")}
            if stated:
                out["draft"] = {**s.get("draft", {}), **stated}
        return out

    def route(self, s: State) -> str:
        t = Turn(**s["turn"])
        if s.get("pending") and t.intent in ("yes", "no"):
            return "execute" if t.intent == "yes" else "decline"
        if t.intent == "book":
            r = BookingRequest(**s.get("draft", {}))
            if r.missing():
                return "clarify"
            return "propose_hold" if t.venue else "lookup"
        if t.intent in ("change", "cancel"):
            return "propose_manage"
        if t.intent == "list":
            return "list_bookings"
        if t.intent == "mail":
            return "use_mail" if s.get("mail_id") else "find_mail"
        return "answer"

    # Mail -------------------------------------------------------------------------------------------------

    async def find_mail(self, s: State) -> dict:
        t, r = Turn(**s["turn"]), BookingRequest(**s.get("draft", {}))
        query = t.mail_query or r.title or (r.category or "").replace("_", " ")
        try:
            mails = await self.tools.call("gmail", "search_my_mail", query=query)
        except ToolFailed as e:
            return {"reply": f"I can't look in your mail yet: {e}", "pending": None, "data": {"link_gmail": True}}
        if not mails:
            return {"reply": f"I didn't find a recent mail matching \"{query}\". Tell me a few words from its subject "
                             "and I'll look again.", "pending": None, "mails": []}
        return {"reply": "These mails look relevant. Pick the one with the event details; I'll read only that one.",
                "pending": None, "mails": [{k: m[k] for k in ("id", "subject", "sender")} for m in mails],
                "data": {"mails": mails}}

    async def use_mail(self, s: State) -> dict:
        try:
            mail = await self.tools.call("gmail", "read_message", message_id=s["mail_id"])
        except ToolFailed as e:
            return {"reply": f"I couldn't read that mail: {e}", "pending": None, "data": {"link_gmail": True}}
        venues = await self.tools.call("booking", "list_venues")
        turn = self.llm.understand(understand_context(s, [], venues), [],
                                   f"Use the details in this mail for my booking:\n\n{mail['text']}")
        stated = {k: v for k, v in turn.request.model_dump(mode="json").items() if v not in (None, [], "")}
        draft = {**s.get("draft", {}), **stated}
        r = BookingRequest(**draft)
        after = {**s, "draft": draft, "turn": turn.model_dump(mode="json")}
        nxt = await (self.clarify(after) if r.missing() else self.lookup(after))
        return {**nxt, "draft": draft, "mails": [],
                "reply": f"From \"{mail['subject']}\" I read: {_summary(BookingRequest(**stated))}. Tell me if "
                         f"anything is wrong.\n\n{nxt['reply']}"}

    # Booking ----------------------------------------------------------------------------------------------

    async def clarify(self, s: State) -> dict:
        return {"reply": _clarify(BookingRequest(**s.get("draft", {}))), "pending": None}

    async def lookup(self, s: State) -> dict:
        r = BookingRequest(**s["draft"])
        out = await self.tools.call("recommender", "rank_venues", request=r.model_dump(mode="json"))
        try:
            reply = self.llm.write(RECOMMEND_PROMPT, {"request": r.model_dump(mode="json"), **out},
                                   s.get("history", [])[-4:])
        except Exception:  # the plain version says the same thing from the same data
            reply = options_text(out, r)
        cards = list(out["options"])
        pref = out.get("preferred")
        if pref and not pref.get("unavailable") and pref["code"] not in [o["code"] for o in cards]:
            cards.append(pref)  # free but ranked lower, usually for a seasonal warning: still the faculty's call
        return {"reply": reply, "options": [o["code"] for o in cards], "pending": None,
                "data": {"options": cards, "nearest_free_dates": out.get("nearest_free_dates", [])}}

    async def propose_hold(self, s: State) -> dict:
        t, r = Turn(**s["turn"]), BookingRequest(**s["draft"])
        try:
            rules = await self.tools.call("knowledge", "get_venue_rules", venue_code=t.venue)
        except ToolFailed as e:
            return {"reply": f"{e} Which venue did you mean?", "pending": None}
        free = await self.tools.call("booking", "find_free_venues", request=r.model_dump(mode="json"))
        if rules["code"] not in free["free"]:
            why = free["excluded"].get(rules["code"], "it isn't available")
            lookup = await self.lookup(s)
            return {**lookup, "reply": f"{rules['code']} won't work: {why}.\n\n{lookup['reply']}"}
        warnings = []
        for d in r.dates:
            checks = await self.tools.call("booking", "check_availability", venue_codes=[rules["code"]],
                                           day=d.isoformat(), start_time=r.start_time.isoformat(),
                                           end_time=r.end_time.isoformat())
            warnings += [w["note"] for c in checks for w in c["warnings"]]
        approver = rules["approver"]["name"] if rules.get("approver") else "the venue in-charge"
        title = r.title or (r.category or "your event").replace("_", " ")
        summary = f"request {rules['name']} ({rules['code']}) for \"{title}\" on {_when(r)}"
        reply = f"Shall I {summary}? {approver} approves it, and it's held for you for " \
                f"{get_settings().hold_hours} hours meanwhile."
        if warnings:
            reply += f" Heads-up: {' '.join(dict.fromkeys(warnings))} You can still go ahead; it's only a warning."
        pending = {"action": "create_hold", "summary": summary,
                   "args": {"request": r.model_dump(mode="json"), "venue_code": rules["code"]}}
        return {"reply": reply, "pending": pending, "data": {"confirm": summary}}

    async def propose_manage(self, s: State) -> dict:
        t, bookings = Turn(**s["turn"]), s.get("bookings", [])
        ids = {b["booking_id"]: b for b in bookings}
        b = ids.get(t.booking_id) or (bookings[0] if len(bookings) == 1 else None)
        if b is None:
            if not bookings:
                return {"reply": "You don't have any upcoming bookings.", "pending": None}
            return {"reply": "Which booking do you mean?\n" + _bookings_text(bookings), "pending": None,
                    "data": {"bookings": bookings}}
        start = datetime.fromisoformat(b["start"])
        what = f"\"{b['title']}\" in {b['venue']} on {start:%a %d %b, %H:%M}"
        if t.intent == "cancel":
            summary = f"cancel {what}"
            args = {"booking_id": b["booking_id"], "reason": t.reason}
            pending = {"action": "cancel_booking", "summary": summary, "args": args}
            reply = f"Shall I {summary}? Everyone involved will be told."
        else:
            args = {k: v for k, v in {"booking_id": b["booking_id"], "venue_code": t.new_venue,
                                      "day": t.new_date and t.new_date.isoformat(),
                                      "start_time": t.new_start_time and t.new_start_time.isoformat(),
                                      "end_time": t.new_end_time and t.new_end_time.isoformat()}.items() if v}
            if len(args) == 1:
                return {"reply": f"What should change for {what}: the venue, the day or the time?", "pending": None}
            to = ", ".join(x for x in (t.new_venue, t.new_date and f"{t.new_date:%a %d %b}",
                                       t.new_start_time and f"from {t.new_start_time:%H:%M}",
                                       t.new_end_time and f"to {t.new_end_time:%H:%M}") if x)
            summary = f"move {what} to {to}"
            pending = {"action": "request_change", "summary": summary, "args": args}
            reply = f"Shall I {summary}?" + (" The current booking stays until the in-charge approves the change."
                                              if b["status"] == "confirmed" else "")
        return {"reply": reply, "pending": pending, "data": {"confirm": summary}}

    async def execute(self, s: State) -> dict:
        p = s["pending"]
        try:
            result = await self.tools.call("booking", p["action"], **p["args"])
        except ToolFailed as e:
            return {"reply": f"That didn't go through: {e}", "pending": None}
        if p["action"] == "create_hold":
            first = result["bookings"][0]
            warn = " The in-charge will see the seasonal warning." if first.get("soft_conflict") else ""
            return {"reply": f"Done. I've sent the request to {p['summary'].removeprefix('request ')}. It's waiting "
                             f"for the in-charge's approval, and you'll be told either way.{warn}",
                    "pending": None, "draft": {}, "options": [], "data": {"bookings": result["bookings"]}}
        if p["action"] == "cancel_booking":
            return {"reply": f"Done. I've cancelled {p['summary'].removeprefix('cancel ')}, and everyone involved "
                             "has been told.", "pending": None}
        extra = (" Your current booking stays until the in-charge approves it." if result.get("replaces_booking_id")
                 else " It's waiting for the in-charge's approval.")
        return {"reply": f"Done. I've asked to {p['summary']}.{extra}", "pending": None,
                "data": {"bookings": [result]}}

    async def decline(self, s: State) -> dict:
        return {"reply": "Okay, I haven't changed anything.", "pending": None}

    async def list_bookings(self, s: State) -> dict:
        bookings = s.get("bookings", [])
        if not bookings:
            return {"reply": "You don't have any upcoming bookings.", "pending": None}
        return {"reply": "Your upcoming bookings:\n" + _bookings_text(bookings), "pending": None,
                "data": {"bookings": bookings}}

    # Questions --------------------------------------------------------------------------------------------

    async def answer(self, s: State) -> dict:
        t = Turn(**s["turn"])
        specs = await self.tools.specs(ANSWER_TOOLS)
        servers = {sp["name"]: sp["server"] for sp in specs}

        async def call(name: str, args: dict) -> Any:
            try:
                return await self.tools.call(servers[name], name, **args)
            except (ToolFailed, KeyError) as e:
                return {"error": str(e)}

        reply = await self.llm.answer(t.question or s["message"], s.get("history", [])[-HISTORY_TURNS:], specs, call)
        return {"reply": reply, "pending": None}


STATUS_WORDS = {"pending": "waiting for approval", "confirmed": "confirmed", "hold": "held"}


def _bookings_text(bookings: list[dict]) -> str:
    return "\n".join(f"- {b['title']}, {b['venue']}, {datetime.fromisoformat(b['start']):%a %d %b %H:%M} "
                     f"({STATUS_WORDS.get(b['status'], b['status'])})" for b in bookings)


# --- Groq -----------------------------------------------------------------------------------------------

UNDERSTAND_PROMPT = """You help faculty at KIOT book venues and labs. Read the latest message in the light of the
conversation and fill in the schema. Put in `request` only details the latest message states or changes; leave the
rest empty. Dates: every day of the event. A talk, lecture, seminar or address is format "talk"; anything where
participants use computers is "hands-on". Significance 3 means a chief guest, VIP or institution-level event.
A venue named when the faculty asks to book or picks one goes in `venue`, even with no other details.
When the faculty refers to a mail ("it's in my mail", "check my Gmail", "use the second mail"), the intent is mail.
When the message is the text of a mail, read the event it is about: who is coming, when, how many, what kind.

{context}"""

ANSWER_PROMPT = """You help faculty at KIOT with questions about venues, labs, bookings and past events. Use the
tools; never state availability, capacities or past usage that a tool didn't return in this conversation. Name the
documents you rely on. If the faculty wants to book something, say you can find a venue if they tell you the
date, time, headcount and whether it's a talk or hands-on. Be brief and plain, without tables. Today is {today}."""


class GroqLLM:
    def __init__(self):
        from langchain_groq import ChatGroq  # imported here so tests and other commands don't need it

        s = get_settings()
        if not (s.groq_api_key and s.groq_agent_model):
            raise RuntimeError("Set GROQ_API_KEY and GROQ_AGENT_MODEL in .env to use the chat.")
        self.chat = ChatGroq(model=s.groq_agent_model, api_key=s.groq_api_key, temperature=0, max_retries=3)

    @staticmethod
    def _messages(system: str, history: list[dict], message: str | None = None):
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

        msgs = [SystemMessage(system)]
        msgs += [HumanMessage(h["content"]) if h["role"] == "user" else AIMessage(h["content"]) for h in history]
        if message:
            msgs.append(HumanMessage(message))
        return msgs

    def understand(self, context: str, history: list[dict], message: str) -> Turn:
        chain = self.chat.with_structured_output(Turn)
        return chain.invoke(self._messages(UNDERSTAND_PROMPT.format(context=context), history, message))

    def write(self, instructions: str, data: Any, history: list[dict]) -> str:
        return self.chat.invoke(self._messages(f"{instructions}\n\nData:\n{json.dumps(data, default=str)}",
                                               history)).content

    async def answer(self, question: str, history: list[dict], tools: list[dict], call) -> str:
        from langchain_core.messages import ToolMessage

        bound = self.chat.bind_tools([{"type": "function", "function": {
            "name": t["name"], "description": t["description"], "parameters": t["schema"]}} for t in tools])
        msgs = self._messages(ANSWER_PROMPT.format(today=f"{bk._now():%A %d %B %Y}"), history, question)
        for _ in range(ANSWER_STEPS):
            ai = await bound.ainvoke(msgs)
            msgs.append(ai)
            if not ai.tool_calls:
                return ai.content
            for tc in ai.tool_calls:
                result = await call(tc["name"], tc["args"])
                msgs.append(ToolMessage(json.dumps(result, default=str)[:6000], tool_call_id=tc["id"]))
        return (await self.chat.ainvoke(msgs)).content


# --- sessions -------------------------------------------------------------------------------------------


async def chat(db: Session, faculty: Faculty, llm: AgentLLM, message: str, session_id: int | None = None,
               action: dict | None = None) -> dict:
    """One turn of a conversation, saved to chat_session."""
    cs = db.get(ChatSession, session_id) if session_id else None
    if cs is None or cs.faculty_id != faculty.id:
        cs = ChatSession(faculty_id=faculty.id, messages=[], state={})
        db.add(cs)
        db.flush()
    state: State = {**cs.state, "message": message, "action": action,
                    "history": [{"role": m["role"], "content": m["content"]} for m in cs.messages]}
    try:
        out = await Agent(llm, Tools(faculty.id)).run(state)
        reply, data = out["reply"], out.get("data")
    except Exception as e:  # keep the conversation usable when Groq or a tool is down
        out, reply, data = state, f"Sorry, something went wrong on my side ({type(e).__name__}). Please try again.", None
    cs.messages = [*cs.messages, {"role": "user", "content": message},
                   {"role": "assistant", "content": reply, "data": data}]
    cs.state = {k: out.get(k) for k in ("draft", "options", "mails", "pending") if out.get(k) is not None}
    db.flush()
    return {"session_id": cs.id, "reply": reply, "data": data, "pending": bool(out.get("pending"))}
