"use strict";
const $ = (s) => document.querySelector(s);
const esc = (v) =>
  String(v ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const money = (v) =>
  new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency: "INR",
    maximumFractionDigits: 0,
  }).format(v || 0);
const stamp = (v) => (v ? new Date(v * 1000).toLocaleString("en-IN") : "");
const state = {
  user: null,
  clinics: [],
  clinic: null,
  view: "overview",
  threads: [],
  appts: [],
  jobs: [],
  leads: [],
  report: {},
  status: {},
  selectedThread: null,
};
let refreshTimer, toastTimer;
function toast(text, error = false) {
  $("#toast").textContent = text;
  $("#toast").className = error ? "error" : "";
  $("#toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => ($("#toast").hidden = true), 6000);
}
async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    ...options,
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": state.user?.csrf || "",
      ...(options.headers || {}),
    },
  });
  let data;
  try {
    data = await response.json();
  } catch {
    data = { detail: "Server response could not be read" };
  }
  if (!response.ok) {
    if (response.status === 401 && state.user) showLogin();
    const detail = Array.isArray(data.detail)
      ? data.detail
          .map((d) => `${d.loc?.slice(1).join(".")}: ${d.msg}`)
          .join("; ")
      : data.detail;
    throw Error(detail || "Request failed");
  }
  return data;
}
function post(path, data = {}) {
  return api(path, { method: "POST", body: JSON.stringify(data) });
}
function showLogin() {
  state.user = null;
  $("#workspace").hidden = true;
  $("#login").hidden = false;
  clearInterval(refreshTimer);
}
function pill(text, type = "") {
  return `<span class="pill ${type}">${esc(text)}</span>`;
}
function empty(title, body = "") {
  return `<div class="empty"><h3>${esc(title)}</h3><p>${esc(body)}</p></div>`;
}
const titles = {
  overview: "Overview",
  appointments: "Appointments",
  inbox: "Conversations",
  delivery: "Message delivery",
  settings: "Clinic settings",
  demo: "Try the demo",
  leads: "Sales enquiries",
};
async function launch() {
  state.clinics = await api("/api/clinics");
  state.clinic =
    state.clinics.find((c) => c.id === state.clinic?.id) ||
    state.clinics[0] ||
    null;
  $("#clinic-select").innerHTML = state.clinics
    .map((c) => `<option value="${c.id}">${esc(c.name)}</option>`)
    .join("");
  if (state.clinic) $("#clinic-select").value = state.clinic.id;
  $("#login").hidden = true;
  $("#workspace").hidden = false;
  $("#account").textContent = `${state.user.username} · ${state.user.role}`;
  $("#environment").textContent =
    state.user.mode === "production"
      ? "CLINIC WORKSPACE"
      : "DEMO WORKSPACE · NO REAL DEMO SENDS";
  $("#demo-nav").hidden = state.user.mode === "production";
  $("#leads-nav").hidden = state.user.role !== "admin";
  await load();
  clearInterval(refreshTimer);
  refreshTimer = setInterval(() => {
    if (
      !document.hidden &&
      !$("#modal").open &&
      state.view !== "settings" &&
      state.view !== "demo"
    )
      load(false).catch((e) => toast(e.message, true));
  }, 15000);
}
async function load(render = true) {
  if (!state.clinic) {
    renderView();
    return;
  }
  const id = state.clinic.id;
  const results = await Promise.all([
    api(`/api/threads?clinic_id=${id}`),
    api(`/api/appointments?clinic_id=${id}`),
    api(`/api/jobs?clinic_id=${id}`),
    api(`/api/reports?clinic_id=${id}`),
    api("/api/status"),
  ]);
  if (state.clinic.id !== id) return;
  [state.threads, state.appts, state.jobs, state.report, state.status] =
    results;
  $("#attention-count").textContent =
    state.threads.filter((t) => t.attention).length || "";
  const missing = [];
  if (!state.status.worker_healthy)
    missing.push("Message worker is offline. Queued messages will wait.");
  if (!state.status.kapso_configured)
    missing.push("WhatsApp credentials are not configured.");
  if (!state.clinic.number_id)
    missing.push("Connect this clinic’s WhatsApp number before activation.");
  if (!state.clinic.enabled)
    missing.push("Automation is paused for this clinic.");
  $("#connection-banner").hidden = !missing.length;
  $("#connection-banner").textContent = missing.join(" ");
  if (render || !["settings", "demo"].includes(state.view)) {
    if (state.view === "inbox" && !render) {
      renderThreadList();
      await renderMessages();
    } else renderView();
  }
}
function renderView() {
  document
    .querySelectorAll("nav button")
    .forEach((b) =>
      b.classList.toggle("active", b.dataset.view === state.view),
    );
  $("#page-title").textContent = titles[state.view];
  if (state.view === "leads" && state.user.role === "admin") {
    renderLeads();
    return;
  }
  if (!state.clinic) {
    $("#content").innerHTML =
      `<div class="panel">${empty("Add your first clinic", "Create a clinic profile, approve its information, then connect WhatsApp.")}<button class="primary" data-action="new-clinic">Add clinic</button></div>`;
    return;
  }
  ({
    overview: renderOverview,
    appointments: renderAppointments,
    inbox: renderInbox,
    delivery: renderDelivery,
    settings: renderSettings,
    demo: renderDemo,
  })[state.view]();
}
function renderOverview() {
  const c = state.clinic,
    r = state.report;
  const today = state.appts.filter(
    (a) =>
      a.source !== "demo" &&
      a.status === "confirmed" &&
      new Date(a.starts * 1000).toLocaleDateString("en-CA", {
        timeZone: c.timezone,
      }) === new Date().toLocaleDateString("en-CA", { timeZone: c.timezone }),
  );
  const attention = state.threads.filter((t) => t.attention).slice(0, 5);
  $("#content").innerHTML =
    `<section class="hero"><div><div class="eyebrow">RECEPTION, WITH ROOM TO BREATHE</div><h2>Welcome to ${esc(c.name)}.</h2><p>Keep appointments moving and give the conversations that need a person a clear next step.</p><div class="status-line">${pill(c.enabled ? "Automation enabled" : "Automation paused", c.enabled ? "good" : "warn")}${pill(c.number_id ? "Number configured" : "WhatsApp setup needed", c.number_id ? "" : "warn")}${pill(c.timezone)}</div></div><div class="hero-icon">◌</div></section><div class="metrics">${[
      ["Today’s appointments", r.today, "Confirmed · live records"],
      ["Needs attention", r.needs_attention, "Patient conversations"],
      ["Attended visits", r.attended, "Staff marked completed"],
      [
        "Payments recorded",
        money(r.recorded_payments),
        "Entered by clinic staff",
      ],
    ]
      .map(
        ([l, v, s]) =>
          `<div class="metric"><p>${l}</p><strong>${esc(v ?? 0)}</strong><small>${s}</small></div>`,
      )
      .join(
        "",
      )}</div><div class="two-col"><section class="panel"><div class="panel-heading"><div><h3>On the schedule</h3><p>Today · ${esc(c.doctor)}</p></div><button class="quiet" data-view="appointments">View all →</button></div>${
      today.length
        ? today
            .slice(0, 5)
            .map(
              (a) =>
                `<div class="list-row"><div><strong>${esc(a.name)}</strong><p>${esc(a.label)}</p></div>${pill(a.status, "good")}</div>`,
            )
            .join("")
        : empty(
            "A clear schedule",
            "Live appointments appear here. Demo records stay separate.",
          )
    }</section><section class="panel"><div class="panel-heading"><div><h3>Needs a human touch</h3><p>Staff requests and delivery issues</p></div><button class="quiet" data-view="inbox">Open inbox →</button></div>${attention.length ? attention.map((t) => `<div class="list-row"><div><strong>${esc(t.name)}</strong><p>${esc(t.attention)}</p></div><button class="secondary" data-action="open-thread" data-id="${t.id}">Open</button></div>`).join("") : empty("Nothing waiting", "New staff requests will appear here.")}</section></div><section class="panel"><div class="panel-heading"><div><h3>Your clinic’s activity</h3><p>Live records only · totals across retained data</p></div></div><div class="metrics"><div><p>Patient enquiries</p><h2>${r.enquiries || 0}</h2></div><div><p>Bookings created</p><h2>${r.booked || 0}</h2></div><div><p>No-shows recorded</p><h2>${r.no_shows || 0}</h2></div><div><p>Messages this month</p><h2>${r.monthly_messages || 0} <small>/ ${r.monthly_limit || 0}</small></h2></div></div><p class="hint">${esc(r.definition || "")}</p></section>`;
}
function renderAppointments() {
  const rows = state.appts;
  $("#content").innerHTML =
    `<section class="panel"><div class="panel-heading"><div><h3>A shared schedule</h3><p>Add phone and walk-in bookings here to keep WhatsApp availability accurate.</p></div><button class="primary" data-action="new-appointment">+ Book appointment</button></div><div class="toolbar"><input id="appointment-search" placeholder="Search patient or contact" aria-label="Search appointments"><select id="appointment-filter" class="status-filter" aria-label="Filter appointments"><option value="">All statuses</option>${["confirmed", "completed", "no_show", "cancelled"].map((s) => `<option>${s}</option>`).join("")}</select><button class="secondary" data-action="export">Export CSV</button></div><div class="table-wrap"><table><thead><tr><th>Patient</th><th>Appointment</th><th>Status</th><th>Source</th><th>Actions</th></tr></thead><tbody id="appointment-rows">${rows.map((a) => appointmentRow(a)).join("")}</tbody></table></div>${!rows.length ? empty("No appointments yet", "Book manually, or let patients choose and confirm a WhatsApp slot.") : ""}</section>`;
}
function appointmentRow(a) {
  return `<tr data-search="${esc((a.name + " " + a.contact).toLowerCase())}" data-status="${a.status}"><td><strong>${esc(a.name)}</strong><small>${esc(a.contact)}</small></td><td>${esc(a.label)}<small>#${a.id}${a.paid_amount ? " · " + money(a.paid_amount) : ""}</small></td><td>${pill(a.status, a.status === "confirmed" || a.status === "completed" ? "good" : "")}</td><td>${pill(a.source, a.source === "demo" ? "warn" : "")}</td><td><div class="actions">${a.status === "confirmed" ? `<button class="secondary" data-action="complete" data-id="${a.id}">Complete</button><button class="secondary" data-action="reschedule" data-id="${a.id}">Move</button><button class="quiet" data-action="no-show" data-id="${a.id}">No-show</button><button class="danger" data-action="cancel" data-id="${a.id}">Cancel</button>` : ""}<button class="quiet" data-action="payment" data-id="${a.id}">Record payment</button></div></td></tr>`;
}
function renderInbox() {
  if (!state.threads.find((t) => t.id === state.selectedThread))
    state.selectedThread = state.threads[0]?.id || null;
  $("#content").innerHTML =
    `<div class="inbox-layout"><div id="thread-list" class="thread-list"></div><section class="panel chat-panel" id="conversation"></section></div>`;
  renderThreadList();
  renderMessages().catch((e) => toast(e.message, true));
}
function renderThreadList() {
  if (!$("#thread-list")) return;
  $("#thread-list").innerHTML = state.threads.length
    ? state.threads
        .map(
          (t) =>
            `<button class="thread-item ${t.id === state.selectedThread ? "selected" : ""}" data-action="open-thread" data-id="${t.id}"><div><strong>${esc(t.name)}</strong>${pill(t.channel === "demo" ? "Demo" : t.mode, t.mode === "human" ? "warn" : "")}</div><p>${esc(t.attention || t.last)}</p><small>${esc(t.contact)}</small></button>`,
        )
        .join("")
    : empty("No conversations", "Incoming patient messages appear here.");
}
async function renderMessages() {
  const t = state.threads.find((t) => t.id === state.selectedThread);
  if (!$("#conversation")) return;
  if (!t) {
    $("#conversation").innerHTML = empty(
      "A conversation starts here",
      "Select a patient to see the history and reply.",
    );
    return;
  }
  const id = t.id;
  const messages = await api(`/api/threads/${id}/messages`);
  if (state.selectedThread !== id || !$("#conversation")) return;
  const draft = $("#reply-text")?.value || "";
  $("#conversation").innerHTML =
    `<div class="chat-header"><div class="panel-heading"><div><h3>${esc(t.name)}</h3><p>${esc(t.contact)} · ${esc(t.channel)}</p></div><div class="actions"><button class="secondary" data-action="${t.mode === "human" ? "resume" : "takeover"}" data-id="${id}">${t.mode === "human" ? "Resume automation" : "Take over"}</button>${state.user.role !== "receptionist" ? `<button class="danger" data-action="erase" data-id="${id}">Delete patient data</button>` : ""}</div></div>${t.attention ? `<p class="hint">${esc(t.attention)}</p>` : ""}<div class="status-line">${pill(t.mode === "human" ? "Staff in control" : "Automated replies", t.mode === "human" ? "warn" : "good")}${pill(t.opted_out ? "Messages stopped" : t.consent ? "Reminders permitted" : "No reminder permission")}</div></div><div class="chat-stream" id="chat-stream">${messages.map((m) => `<div class="bubble ${m.direction}">${esc(m.text)}<small>${esc(stamp(m.created))} · ${esc(m.status)}</small></div>`).join("")}</div><div class="chat-reply"><form id="reply-form"><input id="reply-text" name="text" placeholder="Write a reply…" required maxlength="4000" aria-label="Reply" ${t.opted_out ? "disabled" : ""}><button class="primary" ${t.opted_out ? "disabled" : ""}>Send reply</button></form><p class="hint">Sending a reply takes over the conversation.${t.channel === "demo" ? " This is a local demo; nothing is sent." : ""}</p></div>`;
  $("#reply-text").value = draft;
  $("#chat-stream").scrollTop = $("#chat-stream").scrollHeight;
}
function renderDelivery() {
  $("#content").innerHTML =
    `<section class="panel"><div class="panel-heading"><div><h3>Every message, accounted for</h3><p>Accepted means the provider accepted the send; delivered/read receipts arrive separately.</p></div>${pill(state.status.worker_healthy ? "Worker online" : "Worker offline", state.status.worker_healthy ? "good" : "bad")}</div><div class="table-wrap"><table><thead><tr><th>Job</th><th>State</th><th>Next attempt / due</th><th>Details</th><th></th></tr></thead><tbody>${state.jobs.map((j) => `<tr><td>#${j.id}<small>${esc(j.kind)}</small></td><td>${pill(j.status, ["failed", "uncertain"].includes(j.status) ? "bad" : "")}</td><td>${esc(stamp(j.due))}<small>${j.attempts} attempt(s)</small></td><td>${esc(j.error || "—")}</td><td>${["failed", "uncertain"].includes(j.status) ? `<button class="secondary" data-action="retry" data-id="${j.id}">Review & retry</button>` : ""}</td></tr>`).join("")}</tbody></table></div>${!state.jobs.length ? empty("No queued messages", "WhatsApp replies and reminders are tracked here.") : ""}<p class="hint">An uncertain send is never automatically retried. Check Kapso logs before sending again to avoid duplicates.</p></section>`;
}
function clinicFields(c = {}) {
  const s = c.schedule || {
    open: "09:00",
    close: "19:00",
    duration: 30,
    closed_days: [6],
    breaks: [{ start: "13:00", end: "14:00" }],
    leave_dates: [],
  };
  return `<div class="form-grid"><label>Clinic name<input name="name" value="${esc(c.name || "")}" required maxlength="120"></label><label>Doctor name<input name="doctor" value="${esc(c.doctor || "")}" required maxlength="120"></label><label>Clinic phone<input name="phone" value="${esc(c.phone || "")}" placeholder="+919…" required></label><label>Consultation fee (₹)<input name="fee" type="number" min="0" value="${c.fee ?? 500}" required></label><label class="full">Address<input name="address" value="${esc(c.address || "")}" required maxlength="600"></label><label>Timezone<select name="timezone">${["Asia/Kolkata", "UTC"].map((z) => `<option ${c.timezone === z ? "selected" : ""}>${z}</option>`).join("")}</select></label><label>Appointment duration (minutes)<input name="duration" type="number" min="5" max="120" value="${s.duration}" required></label><label>Opens<input name="open" type="time" value="${s.open}" required></label><label>Closes<input name="close" type="time" value="${s.close}" required></label><label>Break starts (optional)<input name="break_start" type="time" value="${s.breaks[0]?.start || ""}"></label><label>Break ends<input name="break_end" type="time" value="${s.breaks[0]?.end || ""}"></label><div class="full"><label>Closed days</label><div class="status-line">${["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map((d, i) => `<label class="check-label"><input type="checkbox" name="closed_days" value="${i}" ${s.closed_days.includes(i) ? "checked" : ""}>${d}</label>`).join("")}</div></div><label class="full">Leave dates (one YYYY-MM-DD per line)<textarea name="leave_dates">${esc(s.leave_dates.join("\n"))}</textarea></label><label class="full">Approved services (one per line)<textarea name="services">${esc((c.services || ["Dental consultation"]).join("\n"))}</textarea></label><label>WhatsApp phone number ID<input name="number_id" value="${esc(c.number_id || "")}" placeholder="From Kapso" ${state.user.role !== "admin" ? "disabled" : ""}></label><label>Monthly outgoing message allowance<input name="monthly_limit" type="number" min="1" value="${c.monthly_limit ?? 2000}" ${state.user.role !== "admin" ? "disabled" : ""}></label><label>Plan<input name="plan" value="${esc(c.plan || "Pilot")}" ${state.user.role !== "admin" ? "disabled" : ""}></label><label>Monthly subscription (₹)<input name="monthly_fee" type="number" min="0" value="${c.monthly_fee ?? 4999}" ${state.user.role !== "admin" ? "disabled" : ""}></label><label>Plan expiry (optional)<input name="pilot_end" type="date" value="${c.pilot_end ? new Date(c.pilot_end * 1000).toISOString().slice(0, 10) : ""}" ${state.user.role !== "admin" ? "disabled" : ""}></label><div class="status-line"><label class="check-label"><input name="enabled" type="checkbox" ${c.enabled ? "checked" : ""}>Enable automated replies</label><label class="check-label"><input name="template_approved" type="checkbox" ${c.template_approved ? "checked" : ""}>Reminder template approved</label></div><div class="full"><label>Approved questions & answers</label><p class="hint">Only add administrative answers approved by the clinic.</p><div id="faq-rows">${(c.faqs || []).map((f) => faqRow(f)).join("")}</div><button type="button" class="secondary" data-action="add-faq">+ Add answer</button></div></div>`;
}
function faqRow(f = {}) {
  return `<div class="faq-row"><input class="faq-question" value="${esc(f.question || "")}" placeholder="Patient question" aria-label="FAQ question" required maxlength="300"><textarea class="faq-answer" placeholder="Approved answer" aria-label="FAQ answer" required maxlength="1200">${esc(f.answer || "")}</textarea><button type="button" class="quiet" data-action="remove-faq" aria-label="Remove answer">✕</button></div>`;
}
function readClinic(form) {
  const d = new FormData(form),
    old = state.clinic || {};
  return {
    name: d.get("name"),
    doctor: d.get("doctor"),
    phone: d.get("phone"),
    address: d.get("address"),
    timezone: d.get("timezone"),
    fee: Number(d.get("fee")),
    schedule: {
      open: d.get("open"),
      close: d.get("close"),
      duration: Number(d.get("duration")),
      closed_days: d.getAll("closed_days").map(Number),
      breaks: d.get("break_start")
        ? [{ start: d.get("break_start"), end: d.get("break_end") }]
        : [],
      leave_dates: String(d.get("leave_dates"))
        .split("\n")
        .map((s) => s.trim())
        .filter(Boolean),
    },
    services: String(d.get("services"))
      .split("\n")
      .map((s) => s.trim())
      .filter(Boolean),
    faqs: [...form.querySelectorAll(".faq-row")].map((row) => ({
      question: row.querySelector("input").value,
      answer: row.querySelector("textarea").value,
    })),
    number_id:
      state.user.role === "admin" ? d.get("number_id") || null : old.number_id,
    enabled: d.has("enabled"),
    template_approved: d.has("template_approved"),
    plan: state.user.role === "admin" ? d.get("plan") : old.plan,
    monthly_fee:
      state.user.role === "admin"
        ? Number(d.get("monthly_fee"))
        : old.monthly_fee,
    monthly_limit:
      state.user.role === "admin"
        ? Number(d.get("monthly_limit"))
        : old.monthly_limit,
    pilot_end:
      state.user.role === "admin"
        ? d.get("pilot_end")
          ? Math.floor(
              new Date(d.get("pilot_end") + "T23:59:59Z").getTime() / 1000,
            )
          : null
        : old.pilot_end,
  };
}
function renderSettings() {
  const c = state.clinic;
  $("#content").innerHTML =
    `<section class="panel"><div class="panel-heading"><div><h3>Approved clinic information</h3><p>This information guides patient replies and appointment availability.</p></div>${state.user.role === "admin" ? '<button class="secondary" data-action="new-clinic">+ Add clinic</button>' : ""}</div>${state.user.role === "receptionist" ? '<p class="hint">Ask the clinic owner to update settings.</p>' : `<form id="settings-form">${clinicFields(c)}<div class="form-actions"><button class="primary">Save clinic settings</button><p class="hint">Schedule changes affect new bookings. Review existing appointments separately.</p></div></form>`}</section><section class="panel"><h3>WhatsApp activation</h3><div class="setup-step"><span>1</span><div><strong>Connect the clinic’s number in Kapso</strong><p>Use the clinic-owned account. Save its phone number ID above.</p></div></div><div class="setup-step"><span>2</span><div><strong>Register the clinic webhook</strong><p>Use the Meta payload option and the secret configured on the server. Your deployment guide contains the setup command.</p></div></div><div class="setup-step"><span>3</span><div><strong>Approve the reminder template and test</strong><p>Check booking, cancellation, staff takeover and message delivery from a real patient number before activation.</p></div></div><div class="status-line">${pill(state.status.kapso_configured ? "Credentials configured" : "Credentials missing", state.status.kapso_configured ? "good" : "warn")}${pill(state.status.ai_mode)}${pill("Live acceptance test required", "warn")}</div></section>${state.user.role === "admin" ? `<section class="panel"><div class="panel-heading"><h3>Clinic staff access</h3><button class="secondary" data-action="new-user">+ Add staff account</button></div><div id="users-list"></div></section>` : ""}<section class="panel password-panel"><h3>Change your password</h3><form id="password-form"><div class="form-grid"><label>Current password<input name="current" type="password" required autocomplete="current-password"></label><label>New password (16+ characters)<input name="new" type="password" required minlength="16" maxlength="128" autocomplete="new-password"></label></div><div class="form-actions"><button class="secondary">Change password & sign out</button></div></form></section>`;
  if (state.user.role === "admin")
    api("/api/users")
      .then((users) => {
        if (!$("#users-list")) return;
        $("#users-list").innerHTML = users
          .map(
            (u) =>
              `<div class="list-row"><div><strong>${esc(u.username)}</strong><p>${esc(u.role)} · clinic ${u.clinic_id ?? "all"} · ${u.active ? "active" : "disabled"}</p></div>${u.id !== state.user.id && u.active ? `<button class="danger" data-action="disable-user" data-id="${u.id}">Disable</button>` : ""}</div>`,
          )
          .join("");
      })
      .catch((e) => toast(e.message, true));
}
function renderLeads() {
  $("#content").innerHTML = `<section class="panel"><div class="panel-heading"><div><h3>Website demo requests</h3><p>Keep sales follow-up in one place. Only administrator accounts can access these enquiries.</p></div></div><div id="leads-list">Loading enquiries…</div><p class="hint">Contact people only about their demo request. Delete records when no longer needed or when deletion is requested.</p></section>`;
  api("/api/leads")
    .then((leads) => {
      state.leads = leads;
      const target = $("#leads-list");
      if (!target || state.view !== "leads") return;
      if (!leads.length) {
        target.innerHTML = empty("No demo requests yet", "Requests submitted on the public Jyutrix site will appear here.");
        return;
      }
      target.innerHTML = `<div class="table-wrap"><table><thead><tr><th>Contact</th><th>Clinic</th><th>Request</th><th>Received</th><th>Stage</th><th></th></tr></thead><tbody>${leads.map((lead) => `<tr><td><strong>${esc(lead.name)}</strong><small><a href="tel:${esc(lead.phone)}">${esc(lead.phone)}</a>${lead.email ? `<br><a href="mailto:${esc(lead.email)}">${esc(lead.email)}</a>` : ""}</small></td><td>${esc(lead.clinic_name)}<small>${esc(lead.city)}</small></td><td>${esc(lead.note || "—")}</td><td>${esc(stamp(lead.created))}</td><td><select data-lead-id="${lead.id}" aria-label="Follow-up stage for ${esc(lead.name)}">${["new", "contacted", "qualified", "won", "closed"].map((stage) => `<option value="${stage}" ${stage === lead.status ? "selected" : ""}>${stage}</option>`).join("")}</select></td><td><button class="danger" data-action="delete-lead" data-id="${lead.id}">Delete</button></td></tr>`).join("")}</tbody></table></div>`;
    })
    .catch((error) => toast(error.message, true));
}

function renderDemo() {
  $("#content").innerHTML =
    `<section class="panel"><div class="panel-heading"><div><h3>Try a patient conversation</h3><p>Same booking engine, separate demo records. No WhatsApp messages are sent.</p></div>${pill("Demo only", "warn")}</div><form id="demo-form"><div class="form-grid"><label>Patient name<input name="name" value="Demo patient" required></label><label>Demo contact<input name="contact" value="919999000001" required></label></div><div class="demo-chat" id="demo-chat">Send BOOK → choose 1 → send YES. No booking is created until confirmation.</div><div class="demo-quick">${["Hello", "BOOK", "1", "YES", "FEES", "RESCHEDULE", "CANCEL", "STAFF", "STOP", "START"].map((t) => `<button type="button" class="secondary" data-action="demo-quick" data-text="${t}">${t}</button>`).join("")}</div><div class="form-actions"><input name="text" placeholder="Type a patient message…" required maxlength="4000" aria-label="Demo patient message"><button class="primary">Send</button></div></form><p class="hint">The prototype recognizes selected Telugu/Hindi phrases; replies currently use English. Full translated conversation quality requires separate acceptance testing.</p></section>`;
}
function modal(title, html) {
  $("#modal-title").textContent = title;
  $("#modal-body").innerHTML = html;
  $("#modal").showModal();
}
async function newAppointment(moveId = null) {
  const slots = await api(`/api/availability?clinic_id=${state.clinic.id}`);
  const a = state.appts.find((x) => x.id === moveId);
  modal(
    moveId ? "Reschedule appointment" : "Book an appointment",
    `<form id="appointment-form" data-move="${moveId || ""}"><div class="form-grid"><label>Patient name<input name="name" value="${esc(a?.name || "")}" required></label><label>Patient phone<input name="contact" value="${esc(a?.contact || "")}" placeholder="+919…" required></label><label class="full">Find a date<input id="booking-day" type="date"></label><label class="full">Available slot<select name="scheduled_at" id="slot-select" required>${slots.map((s) => `<option value="${esc(s.iso)}">${esc(s.label)}</option>`).join("")}</select></label><label class="full check-label"><input name="consent" type="checkbox">Patient agreed to appointment messages on WhatsApp</label></div><div class="form-actions"><button class="primary">${moveId ? "Confirm new time" : "Confirm booking"}</button></div><p class="hint">Availability is checked again when you confirm.</p></form>`,
  );
}
function filterAppointments() {
  const q = $("#appointment-search").value.toLowerCase(),
    status = $("#appointment-filter").value;
  document
    .querySelectorAll("#appointment-rows tr")
    .forEach(
      (r) =>
        (r.hidden =
          !r.dataset.search.includes(q) ||
          (status && r.dataset.status !== status)),
    );
}
function exportCSV() {
  const safe = (v) => {
    let s = String(v ?? "");
    if (/^[=+@\-\t\r]/.test(s)) s = "'" + s;
    return '"' + s.replaceAll('"', '""') + '"';
  };
  const rows = [
    ["id", "name", "contact", "date", "status", "source", "paid_amount"],
    ...state.appts.map((a) => [
      a.id,
      a.name,
      a.contact,
      a.label,
      a.status,
      a.source,
      a.paid_amount,
    ]),
  ];
  const blob = new Blob(
    ["\ufeff" + rows.map((r) => r.map(safe).join(",")).join("\r\n")],
    { type: "text/csv;charset=utf-8" },
  );
  const url = URL.createObjectURL(blob),
    a = document.createElement("a");
  a.href = url;
  a.download = "clinic-appointments.csv";
  a.click();
  URL.revokeObjectURL(url);
}
document.addEventListener("click", async (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  try {
    if (button.dataset.view) {
      state.view = button.dataset.view;
      renderView();
      return;
    }
    const action = button.dataset.action,
      id = Number(button.dataset.id);
    if (!action) return;
    if (action === "delete-lead") {
      if (!confirm("Delete this demo request and its contact details? This cannot be undone.")) return;
      await api(`/api/leads/${id}`, { method: "DELETE" });
      renderLeads();
      toast("Enquiry deleted");
    } else if (action === "open-thread") {
      state.selectedThread = id;
      if (state.view !== "inbox") {
        state.view = "inbox";
        renderView();
      } else {
        renderThreadList();
        await renderMessages();
      }
    } else if (action === "new-clinic")
      modal(
        "Add a clinic",
        `<form id="new-clinic-form">${clinicFields()}<div class="form-actions"><button class="primary">Create clinic</button></div></form>`,
      );
    else if (action === "new-appointment") await newAppointment();
    else if (action === "reschedule") await newAppointment(id);
    else if (["complete", "no-show", "cancel"].includes(action)) {
      if (
        !confirm(
          `Mark appointment #${id} ${action === "complete" ? "completed" : action === "no-show" ? "no-show" : "cancelled"}?`,
        )
      )
        return;
      await api(`/api/appointments/${id}`, {
        method: "PATCH",
        body: JSON.stringify({
          status:
            action === "complete"
              ? "completed"
              : action === "no-show"
                ? "no_show"
                : "cancelled",
        }),
      });
      await load();
      toast("Appointment updated");
    } else if (action === "payment") {
      modal(
        "Record payment",
        `<form id="payment-form" data-id="${id}"><label>Amount paid (₹)<input name="amount" type="number" min="0" max="10000000" required></label><div class="form-actions"><button class="primary">Save recorded payment</button></div><p class="hint">This records a payment reported by staff; it does not collect money.</p></form>`,
      );
    } else if (action === "takeover" || action === "resume") {
      await post(`/api/threads/${id}/${action}`);
      await load();
      toast(action === "resume" ? "Automation resumed" : "You are in control");
    } else if (action === "erase") {
      if (
        !confirm(
          "Delete this patient’s conversation data and anonymize appointment records? This cannot be undone.",
        )
      )
        return;
      await api(`/api/threads/${id}/patient`, { method: "DELETE" });
      state.selectedThread = null;
      await load();
      toast("Patient data deleted");
    } else if (action === "retry") {
      const j = state.jobs.find((j) => j.id === id);
      if (
        !confirm(
          j.status === "uncertain"
            ? "Have you checked Kapso logs and confirmed this message was NOT sent? Only continue after that check."
            : "Have you fixed the reported issue and checked this retry will not duplicate a delivered message?",
        )
      )
        return;
      await post(`/api/jobs/${id}/retry`, { reviewed_provider_logs: true });
      await load();
      toast("Retry queued");
    } else if (action === "add-faq")
      $("#faq-rows").insertAdjacentHTML("beforeend", faqRow());
    else if (action === "remove-faq") button.closest(".faq-row").remove();
    else if (action === "new-user")
      modal(
        "Add clinic staff",
        `<form id="user-form"><div class="form-grid"><label>Username<input name="username" required minlength="3"></label><label>Temporary password<input name="password" type="password" required minlength="16" autocomplete="new-password"></label><label>Role<select name="role"><option value="receptionist">Receptionist</option><option value="owner">Owner</option></select></label></div><div class="form-actions"><button class="primary">Create account</button></div><p class="hint">Account is restricted to ${esc(state.clinic.name)}. Share credentials securely; staff can change their password after login.</p></form>`,
      );
    else if (action === "disable-user") {
      if (!confirm("Disable this staff account and revoke its sessions?"))
        return;
      await post(`/api/users/${id}/disable`);
      renderSettings();
    } else if (action === "demo-quick") {
      $("#demo-form [name=text]").value = button.dataset.text;
      $("#demo-form").requestSubmit();
    } else if (action === "export") exportCSV();
  } catch (e) {
    toast(e.message, true);
  }
});
document.addEventListener("submit", async (event) => {
  const form = event.target;
  event.preventDefault();
  const button = form.querySelector("[type=submit],button:not([type=button])");
  if (button) button.disabled = true;
  try {
    if (form.id === "login-form") {
      const d = Object.fromEntries(new FormData(form));
      state.user = await post("/api/auth/login", d);
      await launch();
      $("#login-error").textContent = "";
    } else if (form.id === "settings-form" || form.id === "new-clinic-form") {
      const data = readClinic(form);
      const c = await api(
        form.id === "settings-form"
          ? `/api/clinics/${state.clinic.id}`
          : "/api/clinics",
        {
          method: form.id === "settings-form" ? "PUT" : "POST",
          body: JSON.stringify(data),
        },
      );
      state.clinic = c;
      if ($("#modal").open) $("#modal").close();
      await launch();
      toast("Clinic saved");
    } else if (form.id === "appointment-form") {
      const d = new FormData(form),
        id = Number(form.dataset.move);
      if (id)
        await api(`/api/appointments/${id}`, {
          method: "PATCH",
          body: JSON.stringify({ scheduled_at: d.get("scheduled_at") }),
        });
      else
        await post("/api/appointments", {
          clinic_id: state.clinic.id,
          name: d.get("name"),
          contact: d.get("contact"),
          scheduled_at: d.get("scheduled_at"),
          consent: d.has("consent"),
          request_key: crypto.randomUUID(),
        });
      $("#modal").close();
      await load();
      toast("Appointment confirmed");
    } else if (form.id === "reply-form") {
      await post(`/api/threads/${state.selectedThread}/reply`, {
        text: $("#reply-text").value,
      });
      $("#reply-text").value = "";
      await load();
      toast("Reply queued");
    } else if (form.id === "demo-form") {
      const d = Object.fromEntries(new FormData(form)),
        r = await post("/api/demo/message", {
          ...d,
          clinic_id: state.clinic.id,
        });
      $("#demo-chat").textContent =
        `You: ${d.text}\n\nReception: ${r.reply || "Staff has control; automated replies are paused."}\n\nConversation mode: ${r.mode}`;
      form.querySelector("[name=text]").value = "";
    } else if (form.id === "user-form") {
      await post("/api/users", {
        ...Object.fromEntries(new FormData(form)),
        clinic_id: state.clinic.id,
      });
      $("#modal").close();
      renderSettings();
      toast("Staff account created");
    } else if (form.id === "password-form") {
      await post("/api/auth/password", Object.fromEntries(new FormData(form)));
      showLogin();
      toast("Password changed. Sign in again.");
    } else if (form.id === "payment-form") {
      await api(`/api/appointments/${form.dataset.id}`, {
        method: "PATCH",
        body: JSON.stringify({
          paid_amount: Number(new FormData(form).get("amount")),
        }),
      });
      $("#modal").close();
      await load();
      toast("Payment recorded");
    }
  } catch (e) {
    if (form.id === "login-form") $("#login-error").textContent = e.message;
    else toast(e.message, true);
  } finally {
    if (button) button.disabled = false;
  }
});
document.addEventListener("input", (event) => {
  if (["appointment-search", "appointment-filter"].includes(event.target.id))
    filterAppointments();
});
document.addEventListener("change", async (event) => {
  try {
    if (event.target.id === "clinic-select") {
      state.clinic = state.clinics.find(
        (c) => c.id === Number(event.target.value),
      );
      state.selectedThread = null;
      await load();
    }
    if (event.target.id === "booking-day") {
      const slots = await api(
        `/api/availability?clinic_id=${state.clinic.id}&day=${encodeURIComponent(event.target.value)}`,
      );
      $("#slot-select").innerHTML = slots
        .map((s) => `<option value="${esc(s.iso)}">${esc(s.label)}</option>`)
        .join("");
      if (!slots.length) toast("No slots available for this date");
    }
    if (event.target.id === "appointment-filter") filterAppointments();
    if (event.target.matches("[data-lead-id]")) {
      await api(`/api/leads/${event.target.dataset.leadId}`, {
        method: "PATCH",
        body: JSON.stringify({ status: event.target.value }),
      });
      toast("Enquiry stage updated");
    }
  } catch (e) {
    toast(e.message, true);
  }
});
$("#close-modal").addEventListener("click", () => $("#modal").close());
$("#logout").addEventListener("click", async () => {
  try {
    await post("/api/auth/logout");
    showLogin();
  } catch (e) {
    toast(e.message, true);
  }
});
$("#refresh").addEventListener("click", () =>
  load().catch((e) => toast(e.message, true)),
);
(async () => {
  try {
    const h = await fetch("/health").then((r) => r.json());
    if (h.mode === "demo") {
      $("#demo-login").hidden = false;
      $("#login-form [name=username]").value = "admin";
    }
    state.user = await api("/api/auth/me");
    await launch();
  } catch {
    showLogin();
  }
})();
