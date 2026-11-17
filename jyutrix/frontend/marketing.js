"use strict";
const form = document.querySelector("#lead-form");
const status = document.querySelector("#form-status");
form?.addEventListener("submit", async (event) => {
  event.preventDefault();
  status.textContent = "Sending your request…";
  status.classList.remove("error");
  const submit = form.querySelector("button[type=submit]");
  submit.disabled = true;
  const fields = Object.fromEntries(new FormData(form));
  fields.consent = form.elements.consent.checked;
  try {
    const response = await fetch("/api/leads", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(fields),
    });
    const result = await response.json();
    if (!response.ok) {
      const errors = Array.isArray(result.detail)
        ? result.detail.map((item) => item.msg).join(" ")
        : result.detail;
      throw new Error(errors || "We could not send your request. Please try again.");
    }
    form.reset();
    status.textContent = result.message || "Thanks — your request has been received.";
  } catch (error) {
    status.textContent = error.message || "Connection issue. Please try again.";
    status.classList.add("error");
  } finally {
    submit.disabled = false;
  }
});
