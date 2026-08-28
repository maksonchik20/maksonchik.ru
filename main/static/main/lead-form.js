(function () {
  "use strict";
  var form = document.getElementById("lead-form");
  var success = document.getElementById("lead-success");
  var section = document.getElementById("request");
  var dialog = document.getElementById("lead-dialog");
  if (!form || !success || !section) return;

  var shell = section.querySelector(".lead-shell");
  var opener = null;
  var opened = false;
  var started = false;
  var sending = false;
  var sent = false;
  var error = form.querySelector(".lead-error");
  var button = form.querySelector("button[type=submit]");
  var buttonText = button.textContent;

  function goal(name) {
    // Analytics must never block the form. Do not send contact details to Metrika.
    try {
      if (typeof window.ym === "function") {
        window.ym(Number(form.dataset.metrikaId), "reachGoal", name, {
          placement: dialog && dialog.open ? "modal" : "inline"
        });
      }
    } catch (_) { /* The lead is still saved if analytics is unavailable. */ }
  }

  function markOpen() {
    if (opened || sent) return;
    opened = true;
    goal("lead_form_open");
  }

  function markStart() {
    if (started || sent) return;
    markOpen();
    started = true;
    goal("lead_form_start");
  }

  document.querySelectorAll("[data-lead-open]").forEach(function (link) {
    link.addEventListener("click", function (event) {
      // Without native dialog support, keep the working #request anchor.
      if (!dialog || typeof dialog.showModal !== "function") return;
      event.preventDefault();
      if (dialog.open) return;
      opener = link;
      dialog.appendChild(shell);
      dialog.showModal();
      document.body.classList.add("lead-modal-open");
      markOpen();
      (sent ? success : form.elements.contact).focus({ preventScroll: true });
    });
  });

  if (dialog) {
    dialog.querySelector(".lead-dialog-close").addEventListener("click", function () {
      dialog.close();
    });
    dialog.addEventListener("click", function (event) {
      if (event.target !== dialog) return;
      var bounds = dialog.getBoundingClientRect();
      if (event.clientX < bounds.left || event.clientX > bounds.right ||
          event.clientY < bounds.top || event.clientY > bounds.bottom) dialog.close();
    });
    dialog.addEventListener("close", function () {
      // A queued close event may arrive after a quick reopen.
      if (dialog.open) return;
      section.appendChild(shell);
      document.body.classList.remove("lead-modal-open");
      if (opener) opener.focus({ preventScroll: true });
    });
  }

  // The same first step covers both opening the popup and viewing the inline form.
  if ("IntersectionObserver" in window) {
    var observer = new IntersectionObserver(function (entries) {
      if (entries.some(function (entry) { return entry.isIntersecting; })) {
        markOpen();
        observer.disconnect();
      }
    }, { threshold: 0.3 });
    observer.observe(form);
  }
  form.addEventListener("input", function (event) {
    if (["contact", "name", "message"].indexOf(event.target.name) !== -1 &&
        event.target.value.trim()) markStart();
  });

  var params = new URLSearchParams(window.location.search);
  var utmKeys = ["utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term"];
  var attribution = {};
  var hasUtm = utmKeys.some(function (key) { return params.has(key); });
  // Keep campaign attribution when a visitor opens another service page before applying.
  try {
    if (!hasUtm) attribution = JSON.parse(sessionStorage.getItem("lead_attribution") || "{}");
  } catch (_) { attribution = {}; }
  if (!attribution || typeof attribution !== "object") attribution = {};
  if (hasUtm) {
    utmKeys.forEach(function (key) { attribution[key] = (params.get(key) || "").slice(0, 255); });
    try { sessionStorage.setItem("lead_attribution", JSON.stringify(attribution)); } catch (_) {}
  }
  form.elements.page_url.value = window.location.href;
  form.elements.page_title.value = document.title;
  utmKeys.forEach(function (key) {
    form.elements[key].value = typeof attribution[key] === "string" ? attribution[key].slice(0, 255) : "";
  });

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    if (sending || sent) return;
    error.textContent = "";
    // Reveal optional fields if an autofilled or previously entered value is invalid.
    var optional = form.querySelector(".lead-optional");
    if (optional && !optional.open &&
        (!form.elements.name.validity.valid || !form.elements.message.validity.valid)) optional.open = true;
    if (!form.reportValidity()) return;
    markStart();
    sending = true;
    button.disabled = true;
    button.textContent = "Отправляю…";
    form.setAttribute("aria-busy", "true");

    fetch(form.action, {
      method: "POST",
      body: new FormData(form),
      headers: { "X-Requested-With": "XMLHttpRequest" },
      credentials: "same-origin"
    }).then(function (response) {
      return response.json().then(function (data) { return { ok: response.ok, data: data }; });
    }).then(function (result) {
      if (!result.ok || !result.data.ok) throw new Error(result.data.error || "Не удалось отправить заявку.");
      sent = true;
      form.classList.add("is-done");
      form.inert = true;
      form.setAttribute("aria-hidden", "true");
      success.removeAttribute("aria-hidden");
      success.classList.add("is-visible");
      success.focus({ preventScroll: true });
      // Honeypot responses intentionally lack lead_id: never count them as leads.
      if (result.data.lead_id) goal("lead_sent");
    }).catch(function (failure) {
      error.textContent = failure.message || "Не удалось отправить. Напишите мне в Telegram.";
      button.disabled = false;
      button.textContent = buttonText;
    }).finally(function () {
      sending = false;
      form.removeAttribute("aria-busy");
    });
  });
})();
