const STATUS_LABELS = {
  scheduled: "مجدولة",
  boarding: "صعود الركاب",
  delayed: "متأخرة",
  cancelled: "ملغاة",
  completed: "مكتملة",
};
const BOOKING_STATUS_LABELS = { confirmed: "مؤكد", cancelled: "ملغى" };
const VIEW_COPY = {
  dashboard: ["مرحباً بك في رِحال", "نظرة عامة على الرحلات والحجوزات."],
  flights: ["الرحلات الجوية", "إدارة ومتابعة الرحلات المسجلة."],
  bookings: ["حجوزات الركاب", "إدارة بيانات الحجوزات وعدد المقاعد."],
  admin: ["المستخدمون والتدقيق", "إدارة حسابات الموظفين ومراجعة التغييرات."],
};
const state = {
  flightPage: 1,
  flightPages: 0,
  bookingPage: 1,
  bookingPages: 0,
  auditPage: 1,
  auditPages: 0,
  role: "staff",
  toastTimer: null,
  searchTimer: null,
  notificationTimer: null,
};
const elements = {
  loginScreen: document.querySelector("#login-screen"),
  appShell: document.querySelector("#app-shell"),
  loginForm: document.querySelector("#login-form"),
  loginError: document.querySelector("#login-error"),
  loginSubmit: document.querySelector("#login-submit"),
  flightTable: document.querySelector("#flights-table-body"),
  flightEmpty: document.querySelector("#empty-state"),
  search: document.querySelector("#search-input"),
  statusFilter: document.querySelector("#status-filter"),
  flightStart: document.querySelector("#flight-start-date"),
  flightEnd: document.querySelector("#flight-end-date"),
  flightDialog: document.querySelector("#flight-dialog"),
  flightForm: document.querySelector("#flight-form"),
  flightError: document.querySelector("#form-error"),
  flightSubmit: document.querySelector("#submit-flight-button"),
  bookingTable: document.querySelector("#bookings-table-body"),
  bookingEmpty: document.querySelector("#booking-empty-state"),
  bookingDialog: document.querySelector("#booking-dialog"),
  bookingForm: document.querySelector("#booking-form"),
  bookingError: document.querySelector("#booking-form-error"),
  bookingSubmit: document.querySelector("#submit-booking-button"),
  toast: document.querySelector("#toast"),
};
const numberFormatter = new Intl.NumberFormat("ar-SA");
const dateFormatter = new Intl.DateTimeFormat("ar-SA-u-ca-gregory", {
  day: "numeric",
  month: "short",
  year: "numeric",
});
const timeFormatter = new Intl.DateTimeFormat("ar-SA", {
  hour: "2-digit",
  minute: "2-digit",
  hour12: true,
});

function showToast(message) {
  elements.toast.textContent = message;
  elements.toast.classList.add("visible");
  window.clearTimeout(state.toastTimer);
  state.toastTimer = window.setTimeout(
    () => elements.toast.classList.remove("visible"),
    2800,
  );
}

function runRequest(action) {
  action().catch((error) => showToast(error.message));
}

async function apiRequest(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: { "Content-Type": "application/json", ...options.headers },
  });
  if (response.status === 401 && !url.startsWith("/api/auth/")) {
    showLogin("انتهت جلستك. سجّل الدخول مرة أخرى.");
  }
  if (!response.ok) {
    let message = "حدث خطأ أثناء تنفيذ الطلب.";
    try {
      const body = await response.json();
      if (typeof body.detail === "string") message = body.detail;
      else if (Array.isArray(body.detail) && body.detail[0]?.msg) {
        message = body.detail[0].msg;
      }
    } catch (error) {
      if (!(error instanceof SyntaxError)) throw error;
    }
    throw new Error(message);
  }
  if (response.status === 204) return null;
  return response.json();
}

function showLogin(message = "") {
  window.clearInterval(state.notificationTimer);
  elements.appShell.hidden = true;
  elements.loginScreen.hidden = false;
  elements.loginError.textContent = message;
  elements.loginError.hidden = !message;
}

function showDashboard(username, role = "staff") {
  state.role = role;
  elements.loginScreen.hidden = true;
  elements.appShell.hidden = false;
  document.querySelector("#profile-username").textContent = username;
  document.querySelectorAll(".admin-only").forEach((item) => {
    item.hidden = role !== "admin";
  });
  setView("dashboard");
  window.clearInterval(state.notificationTimer);
  state.notificationTimer = window.setInterval(loadNotifications, 60_000);
}

function setView(view) {
  if (view === "admin" && state.role !== "admin") view = "dashboard";
  const visible = {
    dashboard: ["dashboard-view", "flights-view"],
    flights: ["flights-view"],
    bookings: ["bookings-view"],
    admin: ["admin-view"],
  }[view] ?? ["dashboard-view", "flights-view"];
  document.querySelectorAll(".view-panel").forEach((panel) => {
    panel.hidden = !visible.includes(panel.id);
  });
  document.querySelectorAll(".nav-link[data-view]").forEach((link) => {
    link.classList.toggle("active", link.dataset.view === view);
  });
  document.querySelector("#page-title").textContent = VIEW_COPY[view][0];
  document.querySelector("#page-description").textContent = VIEW_COPY[view][1];
  document.querySelector("#add-flight-button").hidden =
    view !== "dashboard" && view !== "flights";
  if (view === "dashboard" || view === "flights") runRequest(loadFlights);
  if (view === "bookings") runRequest(loadBookings);
  if (view === "admin") runRequest(loadAdminData);
}

function applyTheme(theme) {
  document.body.dataset.theme = theme;
  const isDark = theme === "dark";
  document.querySelectorAll(".theme-toggle").forEach((button) => {
    button.setAttribute("aria-label", isDark ? "تفعيل الوضع الفاتح" : "تفعيل الوضع الداكن");
    button.title = isDark ? "تفعيل الوضع الفاتح" : "تفعيل الوضع الداكن";
  });
  document.querySelector('meta[name="theme-color"]').content =
    isDark ? "#111214" : "#f6f7fb";
  localStorage.setItem("rihal-theme", theme);
}

function formatTime(value) {
  const date = new Date(value);
  return { date: dateFormatter.format(date), time: timeFormatter.format(date) };
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (character) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[character]);
}

function flightFilters() {
  const params = new URLSearchParams({ page: String(state.flightPage), page_size: "20" });
  params.set("sort_by", document.querySelector("#flight-sort").value);
  params.set("sort_order", document.querySelector("#flight-sort-order").value);
  const search = elements.search.value.trim();
  if (search) params.set("search", search);
  if (elements.statusFilter.value) params.set("status", elements.statusFilter.value);
  if (elements.flightStart.value) params.set("start_date", elements.flightStart.value);
  if (elements.flightEnd.value) params.set("end_date", elements.flightEnd.value);
  return params;
}

function updateFlightExport() {
  const params = flightFilters();
  params.delete("page");
  params.delete("page_size");
  document.querySelector("#export-flights").href =
    `/api/flights/export.csv?${params.toString()}`;
}

function createFlightRow(flight) {
  const departure = formatTime(flight.departure_time);
  const arrival = formatTime(flight.arrival_time);
  return `
    <tr>
      <td><span class="flight-number">${escapeHtml(flight.flight_number)}</span></td>
      <td><div class="route-cell"><span>${escapeHtml(flight.origin)}</span><span class="route-arrow" aria-hidden="true">←</span><span>${escapeHtml(flight.destination)}</span></div></td>
      <td><div class="time-cell"><strong>${departure.time}</strong><span>${departure.date}</span></div></td>
      <td><div class="time-cell"><strong>${arrival.time}</strong><span>${arrival.date}</span></div></td>
      <td>${numberFormatter.format(flight.capacity)} مقعد</td>
      <td>${numberFormatter.format(flight.price)} ر.س</td>
      <td><span class="status-badge status-${flight.status}">${STATUS_LABELS[flight.status]}</span></td>
      <td><div class="row-actions">
        <button class="icon-button edit-button" type="button" data-action="edit-flight" data-id="${flight.id}" aria-label="تعديل الرحلة ${escapeHtml(flight.flight_number)}"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m15 5 4 4M4 20l4.3-.9L19 8.4a2.1 2.1 0 0 0-3-3L5.3 16.1 4 20Z" /></svg></button>
        <button class="icon-button delete-button" type="button" data-action="delete-flight" data-id="${flight.id}" aria-label="حذف الرحلة ${escapeHtml(flight.flight_number)}"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M10 11v6m4-6v6M6 7l1 13h10l1-13M9 7V4h6v3" /></svg></button>
      </div></td>
    </tr>`;
}

async function loadFlights() {
  updateFlightExport();
  const page = await apiRequest(`/api/flights/page?${flightFilters().toString()}`);
  if (page.total_pages && state.flightPage > page.total_pages) {
    state.flightPage = page.total_pages;
    return loadFlights();
  }
  if (!page.total_pages && state.flightPage > 1) {
    state.flightPage = 1;
    return loadFlights();
  }
  elements.flightTable.innerHTML = page.items.map(createFlightRow).join("");
  elements.flightEmpty.hidden = page.items.length > 0;
  elements.flightTable.closest("table").hidden = page.items.length === 0;
  document.querySelector("#results-count").textContent =
    `${numberFormatter.format(page.total)} رحلة`;
  state.flightPages = page.total_pages;
  document.querySelector("#flight-page-label").textContent =
    `${numberFormatter.format(page.page)} / ${numberFormatter.format(page.total_pages || 1)}`;
  document.querySelector("#flight-prev").disabled = page.page <= 1;
  document.querySelector("#flight-next").disabled = page.page >= page.total_pages;
}

async function refreshDashboard() {
  const [stats] = await Promise.all([
    apiRequest("/api/stats"),
    loadFlights(),
  ]);
  document.querySelector("#stat-total").textContent = numberFormatter.format(stats.total);
  document.querySelector("#stat-scheduled").textContent =
    numberFormatter.format(stats.scheduled);
  document.querySelector("#stat-boarding").textContent =
    numberFormatter.format(stats.boarding);
  document.querySelector("#stat-bookings").textContent =
    numberFormatter.format(stats.confirmed_bookings);
  document.querySelector("#nav-count").textContent = numberFormatter.format(stats.total);
  document.querySelector("#booking-nav-count").textContent =
    numberFormatter.format(stats.confirmed_bookings);
}

function openFlightDialog(flight = null) {
  elements.flightForm.dataset.editingId = flight?.id ?? "";
  elements.flightForm.reset();
  elements.flightError.hidden = true;
  elements.flightError.textContent = "";
  document.querySelector("#dialog-title").textContent =
    flight ? "تعديل بيانات الرحلة" : "إضافة رحلة جديدة";
  elements.flightSubmit.textContent = flight ? "حفظ التعديلات" : "حفظ الرحلة";
  if (flight) {
    for (const [name, value] of Object.entries(flight)) {
      const field = elements.flightForm.elements.namedItem(name);
      if (!field) continue;
      if (name.endsWith("_time")) {
        const date = new Date(value);
        const localDate = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
        field.value = localDate.toISOString().slice(0, 16);
      } else {
        field.value = value;
      }
    }
  }
  elements.flightDialog.showModal();
}

function localDateTimeToIso(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) throw new Error("يرجى إدخال موعد صالح للرحلة.");
  return date.toISOString();
}

elements.flightForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  elements.flightError.hidden = true;
  elements.flightSubmit.disabled = true;
  const payload = Object.fromEntries(new FormData(elements.flightForm).entries());
  payload.flight_number = payload.flight_number.trim().replace(/\s+/g, "").toUpperCase();
  payload.origin = payload.origin.trim();
  payload.destination = payload.destination.trim();
  try {
    payload.departure_time = localDateTimeToIso(payload.departure_time);
    payload.arrival_time = localDateTimeToIso(payload.arrival_time);
    payload.capacity = Number(payload.capacity);
    payload.price = Number(payload.price);
    const id = elements.flightForm.dataset.editingId;
    await apiRequest(id ? `/api/flights/${id}` : "/api/flights", {
      method: id ? "PUT" : "POST",
      body: JSON.stringify(payload),
    });
    elements.flightDialog.close();
    await refreshDashboard();
    showToast(id ? "تم تحديث بيانات الرحلة بنجاح." : "تمت إضافة الرحلة بنجاح.");
  } catch (error) {
    elements.flightError.textContent = error.message;
    elements.flightError.hidden = false;
  } finally {
    elements.flightSubmit.disabled = false;
  }
});

elements.flightTable.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const id = Number(button.dataset.id);
  if (button.dataset.action === "edit-flight") {
    const flight = await apiRequest(`/api/flights/${id}`);
    openFlightDialog(flight);
    return;
  }
  if (!window.confirm("هل تريد حذف الرحلة؟ لا يمكن التراجع عن هذا الإجراء.")) return;
  try {
    await apiRequest(`/api/flights/${id}`, { method: "DELETE" });
    await refreshDashboard();
    showToast("تم حذف الرحلة.");
  } catch (error) {
    showToast(error.message);
  }
});

async function openBookingDialog() {
  elements.bookingForm.reset();
  elements.bookingError.hidden = true;
  elements.bookingSubmit.disabled = false;
  const flights = await apiRequest("/api/flights?limit=200");
  const availableFlights = flights.filter(
    (flight) => !["cancelled", "completed"].includes(flight.status),
  );
  const select = document.querySelector("#booking-flight");
  select.innerHTML = availableFlights.map((flight) => `
    <option value="${flight.id}">${escapeHtml(flight.flight_number)} — ${escapeHtml(flight.origin)} ← ${escapeHtml(flight.destination)}</option>
  `).join("");
  if (!availableFlights.length) {
    showToast("لا توجد رحلات متاحة للحجز.");
    return;
  }
  elements.bookingDialog.showModal();
}

function createBookingRow(booking) {
  const confirmed = booking.status === "confirmed";
  return `
    <tr>
      <td><span class="flight-number">${escapeHtml(booking.flight_number)}</span></td>
      <td>${escapeHtml(booking.passenger_name)}</td>
      <td>${escapeHtml(booking.phone)}</td>
      <td>${numberFormatter.format(booking.seats)}</td>
      <td><span class="status-badge status-${confirmed ? "scheduled" : "cancelled"}">${BOOKING_STATUS_LABELS[booking.status]}</span></td>
      <td>${dateFormatter.format(new Date(booking.created_at))}</td>
      <td><button class="button button-quiet booking-status-button" type="button" data-id="${booking.id}" data-next="${confirmed ? "cancelled" : "confirmed"}">${confirmed ? "إلغاء الحجز" : "إعادة التأكيد"}</button></td>
    </tr>`;
}

async function loadBookings() {
  const params = new URLSearchParams({
    page: String(state.bookingPage),
    page_size: "20",
  });
  const search = document.querySelector("#booking-search").value.trim();
  const status = document.querySelector("#booking-status-filter").value;
  if (search) params.set("search", search);
  if (status) params.set("status", status);
  const page = await apiRequest(`/api/bookings?${params.toString()}`);
  if (page.total_pages && state.bookingPage > page.total_pages) {
    state.bookingPage = page.total_pages;
    return loadBookings();
  }
  if (!page.total_pages && state.bookingPage > 1) {
    state.bookingPage = 1;
    return loadBookings();
  }
  elements.bookingTable.innerHTML = page.items.map(createBookingRow).join("");
  elements.bookingEmpty.hidden = page.items.length > 0;
  elements.bookingTable.closest("table").hidden = page.items.length === 0;
  document.querySelector("#booking-results-count").textContent =
    `${numberFormatter.format(page.total)} حجز`;
  state.bookingPages = page.total_pages;
  document.querySelector("#booking-page-label").textContent =
    `${numberFormatter.format(page.page)} / ${numberFormatter.format(page.total_pages || 1)}`;
  document.querySelector("#booking-prev").disabled = page.page <= 1;
  document.querySelector("#booking-next").disabled = page.page >= page.total_pages;
}

elements.bookingForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  elements.bookingError.hidden = true;
  elements.bookingSubmit.disabled = true;
  const payload = Object.fromEntries(new FormData(elements.bookingForm).entries());
  payload.flight_id = Number(payload.flight_id);
  payload.seats = Number(payload.seats);
  if (!payload.email) payload.email = null;
  try {
    await apiRequest("/api/bookings", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    elements.bookingDialog.close();
    await Promise.all([loadBookings(), refreshDashboard()]);
    showToast("تم إنشاء الحجز بنجاح.");
  } catch (error) {
    elements.bookingError.textContent = error.message;
    elements.bookingError.hidden = false;
  } finally {
    elements.bookingSubmit.disabled = false;
  }
});

elements.bookingTable.addEventListener("click", async (event) => {
  const button = event.target.closest(".booking-status-button");
  if (!button) return;
  const nextStatus = button.dataset.next;
  if (nextStatus === "cancelled" && !window.confirm("هل تريد إلغاء هذا الحجز؟")) return;
  try {
    await apiRequest(`/api/bookings/${button.dataset.id}`, {
      method: "PATCH",
      body: JSON.stringify({ status: nextStatus }),
    });
    await Promise.all([loadBookings(), refreshDashboard()]);
    showToast(nextStatus === "cancelled" ? "تم إلغاء الحجز." : "تمت إعادة تأكيد الحجز.");
  } catch (error) {
    showToast(error.message);
  }
});

async function loadAdminData() {
  if (state.role !== "admin") return;
  const [users, audit] = await Promise.all([
    apiRequest("/api/users"),
    apiRequest(`/api/audit?page=${state.auditPage}&page_size=30`),
  ]);
  if (audit.total_pages && state.auditPage > audit.total_pages) {
    state.auditPage = audit.total_pages;
    return loadAdminData();
  }
  if (!audit.total_pages && state.auditPage > 1) {
    state.auditPage = 1;
    return loadAdminData();
  }
  document.querySelector("#users-table-body").innerHTML = users.map((user) => `
    <tr>
      <td>${escapeHtml(user.username)}</td>
      <td>${user.role === "admin" ? "مدير" : "موظف"}</td>
      <td>${user.active ? "نشط" : "معطل"}</td>
      <td><button class="button button-quiet user-active-button" type="button" data-id="${user.id}" data-active="${!user.active}" ${user.id === currentUserId ? "disabled" : ""}>${user.active ? "تعطيل" : "تفعيل"}</button></td>
    </tr>
  `).join("");
  document.querySelector("#audit-table-body").innerHTML = audit.items.map((item) => `
    <tr>
      <td>${escapeHtml(item.actor_username)}</td>
      <td>${escapeHtml(item.action)}</td>
      <td>${escapeHtml(item.entity)} #${numberFormatter.format(item.entity_id ?? 0)}</td>
      <td>${escapeHtml(JSON.stringify(item.details))}</td>
      <td>${dateFormatter.format(new Date(item.created_at))}</td>
    </tr>
  `).join("");
  state.auditPages = audit.total_pages;
  document.querySelector("#audit-results-count").textContent =
    `${numberFormatter.format(audit.total)} تغيير`;
  document.querySelector("#audit-page-label").textContent =
    `${numberFormatter.format(audit.page)} / ${numberFormatter.format(audit.total_pages || 1)}`;
  document.querySelector("#audit-prev").disabled = audit.page <= 1;
  document.querySelector("#audit-next").disabled = audit.page >= audit.total_pages;
}

let currentUserId = null;
document.querySelector("#user-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const error = document.querySelector("#user-form-error");
  error.hidden = true;
  const payload = Object.fromEntries(new FormData(form).entries());
  try {
    await apiRequest("/api/users", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    form.reset();
    await loadAdminData();
    showToast("تم إنشاء حساب المستخدم.");
  } catch (requestError) {
    error.textContent = requestError.message;
    error.hidden = false;
  }
});

document.querySelector("#users-table-body").addEventListener("click", async (event) => {
  const button = event.target.closest(".user-active-button");
  if (!button) return;
  try {
    await apiRequest(`/api/users/${button.dataset.id}/active`, {
      method: "PUT",
      body: JSON.stringify({ active: button.dataset.active === "true" }),
    });
    await loadAdminData();
    showToast("تم تحديث حالة المستخدم.");
  } catch (error) {
    showToast(error.message);
  }
});

async function loadNotifications() {
  if (elements.appShell.hidden) return;
  try {
    const result = await apiRequest("/api/notifications");
    document.querySelector("#notification-count").textContent =
      numberFormatter.format(result.unread);
    const list = document.querySelector("#notifications-list");
    list.innerHTML = result.items.map((item) => `
      <li><span>${escapeHtml(item.message)}</span><button class="button button-quiet notification-read" type="button" data-id="${item.id}">تمت القراءة</button></li>
    `).join("");
    document.querySelector("#empty-notifications").hidden = result.items.length > 0;
  } catch (error) {
    showToast(error.message);
  }
}

document.querySelector("#notifications-list").addEventListener("click", async (event) => {
  const button = event.target.closest(".notification-read");
  if (!button) return;
  try {
    await apiRequest(`/api/notifications/${button.dataset.id}/read`, { method: "POST" });
    await loadNotifications();
  } catch (error) {
    showToast(error.message);
  }
});

document.querySelectorAll(".nav-link[data-view]").forEach((link) => {
  link.addEventListener("click", (event) => {
    event.preventDefault();
    setView(link.dataset.view);
  });
});
document.querySelector("#add-flight-button").addEventListener("click", () => openFlightDialog());
document.querySelector("#add-booking-button").addEventListener("click", () => {
  runRequest(openBookingDialog);
});
document.querySelector("#flight-prev").addEventListener("click", () => {
  state.flightPage = Math.max(1, state.flightPage - 1);
  runRequest(loadFlights);
});
document.querySelector("#flight-next").addEventListener("click", () => {
  state.flightPage = Math.min(state.flightPages, state.flightPage + 1);
  runRequest(loadFlights);
});
document.querySelector("#booking-prev").addEventListener("click", () => {
  state.bookingPage = Math.max(1, state.bookingPage - 1);
  runRequest(loadBookings);
});
document.querySelector("#booking-next").addEventListener("click", () => {
  state.bookingPage = Math.min(state.bookingPages, state.bookingPage + 1);
  runRequest(loadBookings);
});
document.querySelector("#audit-prev").addEventListener("click", () => {
  state.auditPage = Math.max(1, state.auditPage - 1);
  runRequest(loadAdminData);
});
document.querySelector("#audit-next").addEventListener("click", () => {
  state.auditPage = Math.min(state.auditPages, state.auditPage + 1);
  runRequest(loadAdminData);
});
document.querySelector("#flight-start-date").addEventListener("change", () => {
  state.flightPage = 1;
  runRequest(loadFlights);
});
document.querySelector("#flight-end-date").addEventListener("change", () => {
  state.flightPage = 1;
  runRequest(loadFlights);
});
document.querySelector("#flight-sort").addEventListener("change", () => {
  state.flightPage = 1;
  runRequest(loadFlights);
});
document.querySelector("#flight-sort-order").addEventListener("change", () => {
  state.flightPage = 1;
  runRequest(loadFlights);
});
elements.search.addEventListener("input", () => {
  window.clearTimeout(state.searchTimer);
  state.searchTimer = window.setTimeout(() => {
    state.flightPage = 1;
    runRequest(loadFlights);
  }, 250);
});
elements.statusFilter.addEventListener("change", () => {
  state.flightPage = 1;
  runRequest(loadFlights);
});
document.querySelector("#booking-search").addEventListener("input", () => {
  window.clearTimeout(state.searchTimer);
  state.searchTimer = window.setTimeout(() => {
    state.bookingPage = 1;
    runRequest(loadBookings);
  }, 250);
});
document.querySelector("#booking-status-filter").addEventListener("change", () => {
  state.bookingPage = 1;
  runRequest(loadBookings);
});
document.querySelector(".dialog-close").addEventListener("click", () => elements.flightDialog.close());
document.querySelector(".dialog-cancel").addEventListener("click", () => elements.flightDialog.close());
document.querySelector(".booking-dialog-close").addEventListener("click", () => elements.bookingDialog.close());
document.querySelector(".booking-dialog-cancel").addEventListener("click", () => elements.bookingDialog.close());
document.querySelector("#notifications-button").addEventListener("click", async () => {
  await loadNotifications();
  document.querySelector("#notifications-dialog").showModal();
});
document.querySelector(".notifications-close").addEventListener("click", () => {
  document.querySelector("#notifications-dialog").close();
});
document.querySelector("#backup-button").addEventListener("click", () => {
  window.location.assign("/api/admin/backup");
});
document.querySelectorAll(".theme-toggle").forEach((button) => {
  button.addEventListener("click", () => {
    applyTheme(document.body.dataset.theme === "dark" ? "light" : "dark");
  });
});
document.querySelector("#today-label").textContent = new Intl.DateTimeFormat("ar-SA", {
  weekday: "long",
  day: "numeric",
  month: "long",
}).format(new Date());

elements.loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  elements.loginError.hidden = true;
  elements.loginSubmit.disabled = true;
  const credentials = Object.fromEntries(new FormData(elements.loginForm).entries());
  try {
    const session = await apiRequest("/api/auth/login", {
      method: "POST",
      body: JSON.stringify(credentials),
    });
    currentUserId = session.user_id;
    showDashboard(session.username, session.role);
    await refreshDashboard();
    await loadNotifications();
  } catch (error) {
    elements.loginError.textContent = error.message;
    elements.loginError.hidden = false;
  } finally {
    elements.loginSubmit.disabled = false;
  }
});

document.querySelector("#logout-button").addEventListener("click", async () => {
  try {
    await apiRequest("/api/auth/logout", { method: "POST" });
  } finally {
    currentUserId = null;
    elements.loginForm.reset();
    showLogin();
  }
});

applyTheme(localStorage.getItem("rihal-theme") === "dark" ? "dark" : "light");
apiRequest("/api/auth/session")
  .then(async (session) => {
    if (!session.authenticated) return;
    currentUserId = session.user_id;
    showDashboard(session.username, session.role);
    await Promise.all([refreshDashboard(), loadNotifications()]);
  })
  .catch((error) => showLogin(error.message));
