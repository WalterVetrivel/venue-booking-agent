// The booking UI: plain JavaScript over the JSON API, no build step.

const $ = (sel, el = document) => el.querySelector(sel);
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const state = { me: null, venues: [], calendar: null };

async function api(path, { method = "GET", body, form } = {}) {
  const opts = { method, headers: {} };
  if (form) opts.body = form;
  else if (body !== undefined) { opts.body = JSON.stringify(body); opts.headers["Content-Type"] = "application/json"; }
  const r = await fetch(path, opts);
  const data = r.status === 204 ? null : await r.json().catch(() => null);
  if (r.status === 401 && path !== "/me") { showLogin(); throw new Error("Sign in first."); }
  if (!r.ok) {
    const err = new Error(typeof data?.detail === "string" ? data.detail : "Something went wrong.");
    err.data = data;
    throw err;
  }
  return data;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg; t.hidden = false;
  clearTimeout(toast.timer); toast.timer = setTimeout(() => (t.hidden = true), 4000);
}

const fmtDay = iso => new Date(iso).toLocaleDateString("en-IN", { weekday: "short", day: "numeric", month: "short", year: "numeric" });
const fmtTime = iso => iso.slice(11, 16);
const when = b => `${fmtDay(b.start)}, ${fmtTime(b.start)}-${fmtTime(b.end)}`;
const statusTag = s => `<span class="status ${s}">${{ pending: "waiting for approval" }[s] || s}</span>`;

function describeError(e) {
  const d = e.data?.details || [];
  return esc(e.message) + (d.length ? "<ul>" + d.map(c => `<li>${esc(c.detail)}, ${fmtTime(c.start)}-${fmtTime(c.end)}</li>`).join("") + "</ul>" : "");
}

// --- sign-in ---------------------------------------------------------------------------------------

async function start() {
  try { state.me = await api("/me"); } catch { state.me = null; }
  if (!state.me) return showLogin();
  state.venues = await api("/venues");
  $("#who").innerHTML = `${esc(state.me.name)} <button class="secondary small" id="logout">Sign out</button>`;
  $("#logout").onclick = async () => { await api("/auth/logout", { method: "POST" }); location.hash = ""; location.reload(); };
  for (const a of document.querySelectorAll("nav a[data-role]")) a.hidden = !a.dataset.role.split(" ").includes(state.me.role);
  $("#nav").hidden = false;
  fillVenueSelects();
  route();
}

async function showLogin() {
  $("#nav").hidden = true; $("#who").innerHTML = "";
  showView("login");
  const cfg = await api("/auth/config");
  $("#dev-login").hidden = !cfg.dev_login;
  if (cfg.google_client_id) {
    const init = () => {
      google.accounts.id.initialize({ client_id: cfg.google_client_id, callback: async r => {
        try { await api("/auth/google", { method: "POST", body: { credential: r.credential } }); start(); }
        catch (e) { $("#login-error").textContent = e.message; }
      } });
      google.accounts.id.renderButton($("#google-button"), { theme: "outline", size: "large" });
    };
    window.google?.accounts ? init() : window.addEventListener("load", init);
  } else if (!cfg.dev_login) {
    $("#login-error").textContent = "Sign-in isn't set up yet: add GOOGLE_CLIENT_ID to .env.";
  }
}

$("#dev-login").onsubmit = async e => {
  e.preventDefault();
  try { await api("/auth/dev-login", { method: "POST", body: { email: e.target.elements.email.value } }); start(); }
  catch (err) { $("#login-error").textContent = err.message; }
};

// --- chat ------------------------------------------------------------------------------------------

const CHAT_KEY = "chatSession";
const GREETING = "Hi! Tell me what you're planning: what it is, roughly how many people, when, and whether it's a "
  + "talk or hands-on. I'll check what's free and suggest venues. You can also ask about venues, rules or past events.";

function chatId() { try { return +localStorage.getItem(CHAT_KEY) || null; } catch { return null; } }
function setChatId(id) { try { id ? localStorage.setItem(CHAT_KEY, id) : localStorage.removeItem(CHAT_KEY); } catch {} }

async function loadChat() {
  const log = $("#chat-log");
  if (log.dataset.loaded === String(chatId())) return;
  log.innerHTML = "";
  log.dataset.loaded = String(chatId());
  let convo = null;
  if (chatId()) { try { convo = await api(`/chat/sessions/${chatId()}`); } catch { setChatId(null); } }
  addBubble("assistant", GREETING);
  const msgs = convo?.messages || [];
  msgs.forEach((m, i) => addBubble(m.role, m.content, m.data, convo.pending && i === msgs.length - 1));
  $("#chat-form").elements.message.focus();
}

function addBubble(role, text, data, live = false) {
  const div = document.createElement("div");
  div.className = `bubble ${role}`;
  div.innerHTML = `<div class="text">${esc(text).replace(/\n/g, "<br>")}</div>`;
  for (const o of data?.options || []) {
    const card = document.createElement("div");
    card.className = "option";
    card.innerHTML = `<div><b>${esc(o.name)}</b> <span class="muted">${esc(o.code)} · ${o.capacity} seats</span></div>
      <ul>${o.reasons.map(r => `<li>${esc(r)}</li>`).join("")}</ul>
      ${o.soft_conflicts.map(c => `<div class="warn">${esc(c.note)}</div>`).join("")}
      <button type="button" class="small">Request this</button>`;
    card.querySelector("button").onclick = () => sendChat(`Request ${o.code}`, { pick: o.code });
    div.append(card);
  }
  if (data?.nearest_free_dates?.length) {
    div.insertAdjacentHTML("beforeend", `<div class="muted">Nearest free days: ${data.nearest_free_dates.map(d =>
      `${esc(fmtDay(d.date))} (${d.venues.map(esc).join(", ")})`).join("; ")}</div>`);
  }
  if (data?.bookings?.length) div.insertAdjacentHTML("beforeend", `<div class="table-wrap"><table>${data.bookings.map(b => `
    <tr><td>${esc(b.title)}</td><td>${esc(b.venue)}</td><td>${when(b)}</td><td>${statusTag(b.status)}</td>
      <td><button class="secondary small" data-open="${b.booking_id}">Open</button></td></tr>`).join("")}</table></div>`);
  if (data?.confirm && live) {
    const row = document.createElement("div");
    row.className = "actions confirm";
    row.innerHTML = `<button type="button">Yes, go ahead</button><button type="button" class="secondary">No</button>`;
    const [yes, no] = row.querySelectorAll("button");
    yes.onclick = () => sendChat("Yes, go ahead", { confirm: true });
    no.onclick = () => sendChat("No", { confirm: false });
    div.append(row);
  }
  wireOpen(div);
  $("#chat-log").append(div);
  div.scrollIntoView({ block: "end", behavior: "smooth" });
}

async function sendChat(message, action) {
  if (sendChat.busy) return;
  sendChat.busy = true;
  for (const b of document.querySelectorAll("#chat-log .confirm, #chat-log .option button")) b.remove();
  addBubble("user", message);
  const typing = document.createElement("div");
  typing.className = "bubble assistant typing"; typing.textContent = "Checking…";
  $("#chat-log").append(typing);
  try {
    const r = await api("/chat", { method: "POST", body: { message, session_id: chatId(), action } });
    setChatId(r.session_id);
    $("#chat-log").dataset.loaded = String(r.session_id);
    typing.remove();
    addBubble("assistant", r.reply, r.data, r.pending);
  } catch (e) {
    typing.remove();
    addBubble("assistant", e.message);
  } finally {
    sendChat.busy = false;
  }
}

$("#chat-form").onsubmit = e => {
  e.preventDefault();
  const box = e.target.elements.message;
  const text = box.value.trim();
  if (!text) return;
  box.value = "";
  sendChat(text);
};
$("#chat-form").elements.message.onkeydown = e => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#chat-form").requestSubmit(); }
};
$("#chat-new").onclick = () => { setChatId(null); loadChat(); };

// --- routing ---------------------------------------------------------------------------------------

function showView(name) {
  for (const v of document.querySelectorAll(".view")) v.hidden = v.id !== `view-${name}`;
  for (const a of document.querySelectorAll("nav a")) a.classList.toggle("active", a.getAttribute("href") === `#${name}`);
}

const VIEWS = { chat: loadChat, calendar: showCalendar, book: () => {}, mine: loadMine, approvals: loadApprovals,
                timetables: loadTimetables, knowledge: loadKnowledge, faculty: loadFaculty, notices: loadNotices };

function route() {
  if (!state.me) return;
  const name = VIEWS[location.hash.slice(1)] ? location.hash.slice(1) : "chat";
  showView(name);
  VIEWS[name]();
}
window.addEventListener("hashchange", route);

function fillVenueSelects() {
  const opts = state.venues.map(v => `<option value="${v.id}">${esc(v.name)} (${v.capacity} seats${v.n_computers ? `, ${v.n_computers} PCs` : v.byod ? ", BYOD" : ""})</option>`).join("");
  $("#cal-venue").innerHTML = `<option value="">All venues</option>` + opts;
  $("#book-form").elements.venue_id.innerHTML = opts;
}

// --- calendar --------------------------------------------------------------------------------------

function showCalendar() {
  if (state.calendar) { state.calendar.refetchEvents(); state.calendar.updateSize(); return; }
  state.calendar = new FullCalendar.Calendar($("#calendar"), {
    initialView: window.innerWidth < 700 ? "timeGridDay" : "timeGridWeek",
    headerToolbar: { left: "prev,next today", center: "title", right: "timeGridWeek,timeGridDay,listWeek" },
    // Times are shown as campus wall-clock time whatever the viewer's timezone: offsets are dropped and the
    // calendar treats every time as UTC.
    timeZone: "UTC",
    slotMinTime: "08:00", slotMaxTime: "19:00", firstDay: 1, allDaySlot: false, height: "auto", nowIndicator: true,
    events: async (info, ok, fail) => {
      try {
        const venue = $("#cal-venue").value;
        const q = new URLSearchParams({ start: info.startStr.slice(0, 10), end: info.endStr.slice(0, 10) });
        if (venue) q.set("venue_id", venue);
        const data = await api(`/calendar?${q}`);
        const vname = id => state.venues.find(v => v.id === id)?.code || "";
        const wall = iso => iso.slice(0, 19);
        ok([
          ...data.bookings.map(b => ({ title: `${b.title} · ${b.venue.code}`, start: wall(b.start), end: wall(b.end),
            color: b.status === "confirmed" ? "var(--accent)" : "var(--pending)", extendedProps: { booking: b } })),
          ...data.classes.map(c => ({ title: `${c.course} · ${c.class_label}${c.batch ? " " + c.batch : ""} · ${vname(c.venue_id)}`,
            start: wall(c.start), end: wall(c.end), color: "var(--class)" })),
        ]);
      } catch (e) { fail(e); }
    },
    eventClick: info => info.event.extendedProps.booking && openBooking(info.event.extendedProps.booking.id),
    dateClick: info => {
      const f = $("#book-form").elements;
      f.date.value = info.dateStr.slice(0, 10); f.start.value = info.dateStr.slice(11, 16);
      if ($("#cal-venue").value) f.venue_id.value = $("#cal-venue").value;
      location.hash = "book";
    },
  });
  state.calendar.render();
}
$("#cal-venue").onchange = () => state.calendar?.refetchEvents();

// --- booking form ----------------------------------------------------------------------------------

function slotFromForm(f) {
  return { venue_id: +f.venue_id.value, start: `${f.date.value}T${f.start.value}:00+05:30`, end: `${f.date.value}T${f.end.value}:00+05:30` };
}

async function checkAvailability() {
  const f = $("#book-form").elements;
  if (!f.date.value || !f.start.value || !f.end.value) return toast("Pick a date and times first.");
  const s = slotFromForm(f);
  const r = await api(`/availability?${new URLSearchParams({ venue_id: s.venue_id, start: s.start, end: s.end })}`);
  const venue = state.venues.find(v => v.id === s.venue_id);
  const head = +f.expected_headcount.value;
  let html = r.free ? `<p class="ok">${esc(venue.name)} is free then.</p>`
    : `<p class="error">${esc(venue.name)} is not free then:</p><ul>${r.clashes.map(c => `<li>${esc(c.detail)}, ${fmtTime(c.start)}-${fmtTime(c.end)}</li>`).join("")}</ul>`;
  if (head && head > venue.capacity) html += `<p class="error">It seats ${venue.capacity}, fewer than ${head}.</p>`;
  html += r.soft_conflicts.map(c => `<div class="warn">${esc(c.note)}</div>`).join("");
  $("#book-check").innerHTML = html;
}
$("#check-btn").onclick = () => checkAvailability().catch(e => toast(e.message));

$("#book-form").onsubmit = async e => {
  e.preventDefault();
  const f = e.target.elements;
  const body = {
    title: f.title.value, category: f.category.value, format: f.format.value,
    expected_headcount: +f.expected_headcount.value, significance: +f.significance.value,
    description: f.description.value || null, slots: [slotFromForm(f)],
    co_organisers: f.co_organisers.value.split(",").map(s => s.trim()).filter(Boolean).map(email => ({ email })),
  };
  try {
    const r = await api("/events", { method: "POST", body });
    const b = r.bookings[0];
    e.target.reset(); $("#book-check").innerHTML = "";
    toast(b.soft_conflict_note ? "Request sent, with a warning for the in-charge." : "Request sent to the venue's in-charge.");
    location.hash = "mine";
  } catch (err) { $("#book-check").innerHTML = `<p class="error">${describeError(err)}</p>`; }
};

// --- lists -----------------------------------------------------------------------------------------

function bookingRows(list, empty) {
  if (!list.length) return `<p class="muted">${empty}</p>`;
  return `<div class="table-wrap"><table><tr><th>Event</th><th>Venue</th><th>When</th><th>Status</th><th></th></tr>${list.map(b => `
    <tr><td>${esc(b.title)}<br><span class="muted">${esc(b.organiser)}, ${b.expected_headcount} people${b.replaces_booking_id ? ", change request" : ""}</span>
      ${b.soft_conflict_note ? `<div class="warn">${esc(b.soft_conflict_note)}</div>` : ""}</td>
      <td>${esc(b.venue.name)}</td><td>${when(b)}</td><td>${statusTag(b.status)}</td>
      <td><button class="secondary small" data-open="${b.id}">Open</button></td></tr>`).join("")}</table></div>`;
}

function wireOpen(el) { for (const btn of el.querySelectorAll("[data-open]")) btn.onclick = () => openBooking(+btn.dataset.open); }

async function loadMine() {
  const el = $("#mine-list");
  el.innerHTML = bookingRows(await api("/bookings/mine"), "No bookings yet.");
  wireOpen(el);
}

async function loadApprovals() {
  const el = $("#approvals-list");
  el.innerHTML = bookingRows(await api("/approvals"), "Nothing is waiting for you.");
  wireOpen(el);
}

const NOTICE_TEXT = {
  request_received: "New request", request_submitted: "Request sent", approved: "Approved", rejected: "Rejected",
  change_requested: "Change requested", change_approved: "Change approved", change_rejected: "Change rejected",
  request_changed: "Request changed", cancelled: "Cancelled", event_edited: "Event details edited",
  expired: "Request expired", approval_reminder: "Reminder: waiting for your approval", timetable_clash: "Timetable clash",
};

async function loadNotices() {
  const list = await api("/notifications");
  const el = $("#notices-list");
  el.innerHTML = list.length ? `<div class="table-wrap"><table>${list.map(n => `<tr>
    <td>${esc(NOTICE_TEXT[n.kind] || n.kind)}</td><td>${esc(n.payload.title)}<br><span class="muted">${n.payload.booking ? esc(n.payload.booking.venue) + ", " + when(n.payload.booking) : ""}</span>
    ${n.payload.reason ? `<br><span class="muted">Reason: ${esc(n.payload.reason)}</span>` : ""}</td>
    <td class="muted">${new Date(n.at).toLocaleString("en-IN")}</td>
    <td>${n.booking_id ? `<button class="secondary small" data-open="${n.booking_id}">Open</button>` : ""}</td></tr>`).join("")}</table></div>`
    : `<p class="muted">No notices.</p>`;
  wireOpen(el);
}

// --- booking dialog --------------------------------------------------------------------------------

async function openBooking(id) {
  const b = await api(`/bookings/${id}`);
  const d = $("#dialog"), body = $("#dialog-body");
  body.innerHTML = `<h2>${esc(b.title)}</h2>
    <p>${statusTag(b.status)} ${esc(b.venue.name)}, ${when(b)}</p>
    <p class="muted">${esc(b.organiser)} · ${b.expected_headcount} people · ${esc(b.format)}${b.hold_expires_at ? ` · approve by ${new Date(b.hold_expires_at).toLocaleString("en-IN")}` : ""}</p>
    ${b.soft_conflict_note ? `<div class="warn">${esc(b.soft_conflict_note)}</div>` : ""}
    <p class="muted">Notified: ${b.parties.map(p => esc(p.name || p.email)).join(", ")}</p>
    <details><summary>History</summary><ul>${b.history.map(h => `<li>${esc(h.action)} · ${new Date(h.at).toLocaleString("en-IN")}${h.reason ? ` · ${esc(h.reason)}` : ""}</li>`).join("")}</ul></details>
    <p id="dlg-error" class="error"></p>
    <div class="actions">
      ${b.can_approve ? `<button type="button" data-act="approve">Approve</button><button type="button" class="danger" data-act="reject">Reject</button>` : ""}
      ${b.can_change ? `<button type="button" class="secondary" data-act="change">Change venue or time</button><button type="button" class="secondary" data-act="edit">Edit details</button>` : ""}
      ${b.can_cancel ? `<button type="button" class="danger" data-act="cancel">Cancel booking</button>` : ""}
      <button value="close" class="secondary">Close</button>
    </div>`;
  const act = (fn, done) => async () => {
    try { await fn(); d.close(); toast(done); refresh(); }
    catch (e) { $("#dlg-error").innerHTML = describeError(e); }
  };
  const on = (name, fn) => { const btn = body.querySelector(`[data-act="${name}"]`); if (btn) btn.onclick = fn; };
  on("approve", act(() => api(`/bookings/${id}/approve`, { method: "POST" }), "Approved. Everyone involved is notified."));
  on("reject", () => { const reason = prompt("Why? The organiser will see this."); if (reason) act(() => api(`/bookings/${id}/reject`, { method: "POST", body: { reason } }), "Rejected.")(); });
  on("cancel", () => {
    const own = b.is_owner;
    const reason = own ? (confirm("Cancel this booking? Everyone involved will be told.") ? "" : null) : prompt("Why? The organiser will see this.");
    if (reason !== null) act(() => api(`/bookings/${id}/cancel`, { method: "POST", body: { reason: reason || null } }), "Cancelled.")();
  });
  on("change", () => changeForm(b));
  on("edit", () => editForm(b));
  if (!d.open) d.showModal();
}

function changeForm(b) {
  const body = $("#dialog-body");
  body.innerHTML = `<h2>Change ${esc(b.title)}</h2>
    <p class="muted">${b.status === "confirmed" ? "Your booking stays confirmed until the in-charge approves the change." : "The request will wait for approval again."}</p>
    <div class="grid">
      <label class="wide">Venue <select name="venue">${state.venues.map(v => `<option value="${v.id}" ${v.id === b.venue.id ? "selected" : ""}>${esc(v.name)} (${v.capacity})</option>`).join("")}</select></label>
      <label>Date <input name="date" type="date" value="${b.start.slice(0, 10)}"></label>
      <label>Start <input name="start" type="time" value="${fmtTime(b.start)}"></label>
      <label>End <input name="end" type="time" value="${fmtTime(b.end)}"></label>
    </div>
    <p id="dlg-error" class="error"></p>
    <div class="actions"><button value="close" class="secondary">Close</button><button type="button" id="send-change">Request change</button></div>`;
  $("#send-change").onclick = async () => {
    const f = n => body.querySelector(`[name=${n}]`).value;
    try {
      await api(`/bookings/${b.id}/change`, { method: "POST", body: { venue_id: +f("venue"), start: `${f("date")}T${f("start")}:00+05:30`, end: `${f("date")}T${f("end")}:00+05:30` } });
      $("#dialog").close(); toast("Change sent for approval."); refresh();
    } catch (e) { $("#dlg-error").innerHTML = describeError(e); }
  };
}

function editForm(b) {
  const body = $("#dialog-body");
  body.innerHTML = `<h2>Edit details</h2>
    <div class="grid">
      <label class="wide">Title <input name="title" value="${esc(b.title)}"></label>
      <label>Expected headcount <input name="expected_headcount" type="number" min="1" value="${b.expected_headcount}"></label>
    </div>
    <p class="muted">These changes don't need approval. To move venue or time, use "Change venue or time".</p>
    <p id="dlg-error" class="error"></p>
    <div class="actions"><button value="close" class="secondary">Close</button><button type="button" id="save-edit">Save</button></div>`;
  $("#save-edit").onclick = async () => {
    try {
      await api(`/events/${b.event_id}`, { method: "PATCH", body: { title: body.querySelector("[name=title]").value,
        expected_headcount: +body.querySelector("[name=expected_headcount]").value } });
      $("#dialog").close(); toast("Saved. Everyone involved is notified."); refresh();
    } catch (e) { $("#dlg-error").innerHTML = describeError(e); }
  };
}

function refresh() { route(); }

// --- admin: timetables -----------------------------------------------------------------------------

async function loadTimetables() {
  const docs = await api("/admin/timetables");
  $("#tt-list").innerHTML = docs.length ? `<div class="table-wrap"><table><tr><th>File</th><th>Class</th><th>Slots</th><th>To review</th><th>Problems</th></tr>${docs.map(d => `
    <tr><td>${esc(d.name)}</td><td>${esc(d.class_label)}</td><td>${d.active}</td><td>${d.pending_review}</td><td class="muted">${esc(d.problems || "")}</td></tr>`).join("")}</table></div>`
    : `<p class="muted">No timetables read yet. The lab slots in use come from the mock data.</p>`;
  const slots = await api("/admin/timetable-slots?status=pending_review");
  const venueOpts = sel => state.venues.map(v => `<option value="${v.id}" ${v.id === sel ? "selected" : ""}>${esc(v.code)}</option>`).join("");
  $("#tt-review").innerHTML = slots.length ? `<div class="table-wrap"><table><tr><th>Class</th><th>Course</th><th>Day</th><th>Time</th><th>Venue</th><th></th></tr>${slots.map(s => `
    <tr data-id="${s.id}"><td>${esc(s.class_label)} ${esc(s.batch || "")}</td><td>${esc(s.course)}</td><td>${DAYS[s.weekday]}</td>
      <td><input name="start_time" type="time" value="${s.start_time}"> <input name="end_time" type="time" value="${s.end_time}"></td>
      <td><select name="venue_id">${venueOpts(s.venue_id)}</select></td>
      <td><button class="small" data-st="active">Approve</button> <button class="small danger" data-st="rejected">Reject</button></td></tr>`).join("")}</table></div>`
    : `<p class="muted">Nothing to review.</p>`;
  for (const btn of $("#tt-review").querySelectorAll("[data-st]")) btn.onclick = async () => {
    const row = btn.closest("tr"), v = n => row.querySelector(`[name=${n}]`).value;
    try {
      const r = await api(`/admin/timetable-slots/${row.dataset.id}`, { method: "PATCH", body: {
        status: btn.dataset.st, venue_id: +v("venue_id"), start_time: v("start_time"), end_time: v("end_time") } });
      toast(r.flagged_bookings ? `Saved. ${r.flagged_bookings} booking(s) now clash and were flagged.` : "Saved.");
      loadTimetables();
    } catch (e) { toast(e.message); }
  };
}

$("#tt-upload").onsubmit = async e => {
  e.preventDefault();
  const form = new FormData(e.target);
  try {
    const r = await api("/admin/timetables", { method: "POST", form });
    $("#tt-result").textContent = `${r.file}: ${r.class}, ${r.active} slots, ${r.pending_review} to review` +
      (r.flagged_bookings ? `, ${r.flagged_bookings} booking(s) flagged` : "") + (r.problems.length ? `. ${r.problems.join(" ")}` : ".");
    e.target.reset(); loadTimetables();
  } catch (err) { $("#tt-result").textContent = err.message; }
};

// --- admin: knowledge -----------------------------------------------------------------------------

function weekLabel(w, plusDays = 0) {
  // The Monday (plus some days) of ISO week w in the current year, as "9 Mar".
  const jan4 = new Date(Date.UTC(new Date().getUTCFullYear(), 0, 4));
  const monday = new Date(jan4.getTime() - ((jan4.getUTCDay() + 6) % 7) * 864e5 + ((w - 1) * 7 + plusDays) * 864e5);
  return monday.toLocaleDateString("en-IN", { day: "numeric", month: "short", timeZone: "UTC" });
}

async function loadKnowledge() {
  const drive = await api("/admin/drive");
  $("#drive-status").textContent = !drive.folder_configured ? "Set DRIVE_ROOT_FOLDER_ID in .env first."
    : drive.linked ? "Linked. New and changed files are read every night at 2 am." : "Not linked yet.";
  $("#drive-link").hidden = !drive.folder_configured;
  $("#drive-link button").textContent = drive.linked ? "Link again" : "Link Google Drive";

  const pats = await api("/admin/patterns");
  $("#patterns-list").innerHTML = pats.length ? `<div class="table-wrap"><table><tr><th>Venue</th><th>Used for</th><th>Around</th><th>Seen</th><th>Evidence</th></tr>${pats.map(p => `
    <tr><td>${esc(p.venue)}</td><td>${esc(p.category.replace(/_/g, " "))}</td>
      <td>${weekLabel(p.week_start)} to ${weekLabel(p.week_end, 6)}${p.weekdays ? ` (${p.weekdays.map(d => DAYS[d]).join(", ")} only)` : ""}</td>
      <td>${p.years_seen} of ${p.years_observed} years</td>
      <td class="muted">${p.evidence.slice(-3).map(e => esc(`${e.title} (${e.date})`)).join("<br>")}</td></tr>`).join("")}</table></div>`
    : `<p class="muted">No patterns yet. They appear once past events are loaded.</p>`;

  const past = await api("/admin/past-events?status=pending_review");
  const venueOpts = sel => `<option value="">Pick venue</option>` + state.venues.map(v => `<option value="${v.id}" ${v.id === sel ? "selected" : ""}>${esc(v.code)}</option>`).join("");
  $("#past-review").innerHTML = past.length ? `<div class="table-wrap"><table><tr><th>Event</th><th>Dates</th><th>Venue</th><th>Source</th><th></th></tr>${past.map(e => `
    <tr data-id="${e.id}"><td>${esc(e.title)}<br><span class="muted">${esc(e.category || "")}</span></td>
      <td>${e.start_date}${e.end_date !== e.start_date ? ` to ${e.end_date}` : ""}</td>
      <td><select name="venue_id">${venueOpts(e.venue_id)}</select></td><td class="muted">${esc(e.source || "")}</td>
      <td><button class="small" data-st="active">Approve</button> <button class="small danger" data-st="rejected">Reject</button></td></tr>`).join("")}</table></div>`
    : `<p class="muted">Nothing to review.</p>`;
  for (const btn of $("#past-review").querySelectorAll("[data-st]")) btn.onclick = async () => {
    const row = btn.closest("tr"), venue = row.querySelector("[name=venue_id]").value;
    try {
      await api(`/admin/past-events/${row.dataset.id}`, { method: "PATCH", body: { status: btn.dataset.st, ...(venue ? { venue_id: +venue } : {}) } });
      toast("Saved."); loadKnowledge();
    } catch (e) { toast(e.message); }
  };
}

$("#patterns-refresh").onclick = async () => {
  try { const r = await api("/admin/patterns/refresh", { method: "POST" }); toast(`${r.patterns} patterns found.`); loadKnowledge(); }
  catch (e) { toast(e.message); }
};

// --- admin: faculty --------------------------------------------------------------------------------

async function loadFaculty() {
  const list = await api("/admin/faculty");
  const roles = ["faculty", "in_charge", "admin"];
  $("#fac-list").innerHTML = `<div class="table-wrap"><table><tr><th>Name</th><th>Email</th><th>Dept</th><th>Role</th><th>Can sign in</th></tr>${list.map(f => `
    <tr data-id="${f.id}"><td>${esc(f.name)}</td><td>${esc(f.email)}</td><td>${esc(f.department || "")}</td>
      <td><select name="role">${roles.map(r => `<option ${r === f.role ? "selected" : ""} value="${r}">${r.replace("_", "-")}</option>`).join("")}</select></td>
      <td><input type="checkbox" name="active" ${f.active ? "checked" : ""}></td></tr>`).join("")}</table></div>`;
  for (const el of $("#fac-list").querySelectorAll("select, input")) el.onchange = async () => {
    const id = el.closest("tr").dataset.id;
    try { await api(`/admin/faculty/${id}`, { method: "PATCH", body: { [el.name]: el.type === "checkbox" ? el.checked : el.value } }); toast("Saved."); }
    catch (e) { toast(e.message); loadFaculty(); }
  };
}

$("#fac-add").onsubmit = async e => {
  e.preventDefault();
  const f = e.target.elements;
  try {
    await api("/admin/faculty", { method: "POST", body: { name: f.name.value, email: f.email.value, department: f.department.value || null, role: f.role.value } });
    e.target.reset(); toast("Added. They can sign in now."); loadFaculty();
  } catch (err) { toast(err.message); }
};

start();
