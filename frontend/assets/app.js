const state = { run: null, itinerary: null, passengerCount: 1 };
const byId = (id) => document.getElementById(id);

function showMessage(id, text = "", kind = "") {
  const el = byId(id);
  el.textContent = text;
  el.className = `message ${kind}`;
}

async function api(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || "Request failed. Check the server and try again.");
  return payload;
}

function stationOption(station) {
  return `<option value="${station.station_code}">${station.station_name} · ${station.city} (${station.station_code})</option>`;
}

async function loadStations() {
  try {
    const stations = await api("/api/stations");
    const options = stations.map(stationOption).join("");
    byId("from-code").innerHTML = options;
    byId("to-code").innerHTML = options;
    const delhi = stations.find((station) => /delhi/i.test(station.city) || /delhi/i.test(station.station_name));
    const patna = stations.find((station) => /patna/i.test(station.city) || /patna/i.test(station.station_name));
    if (delhi) byId("from-code").value = delhi.station_code;
    if (patna) byId("to-code").value = patna.station_code;
    byId("journey-date").value = new Date(Date.now() + 86400000).toISOString().slice(0, 10);
  } catch (error) {
    showMessage("search-message", error.message, "error");
  }
}

function formatDate(value) {
  if (!value) return "—";
  return new Date(value).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

function trainCard(train) {
  const noSeats = Number(train.available_seats) === 0;
  return `<article class="train-card">
    <div class="train-main">
      <div class="train-number">${train.train_number} <span>${train.train_name}</span></div>
      <div class="route-line"><strong>${train.from_code}</strong><span class="route-track"></span><strong>${train.to_code}</strong></div>
      <div class="train-times"><span>${formatDate(train.departure_datetime)}</span><span>${formatDate(train.arrival_datetime)}</span></div>
    </div>
    <div class="train-side"><span class="availability ${noSeats ? "sold-out" : ""}">${noSeats ? "Waitlist" : `${train.available_seats} seats`}</span>
      <button class="button ${noSeats ? "button-light" : "button-dark"} choose-train" data-run="${train.run_id}">${noSeats ? "Join waitlist" : "Select"}</button>
    </div>
  </article>`;
}

function addPassengerRow() {
  const index = state.passengerCount++;
  const row = document.createElement("div");
  row.className = "passenger-row";
  row.innerHTML = `<div class="passenger-index">${String(index).padStart(2, "0")}</div>
    <label>Full name<input name="full_name" maxlength="100" required placeholder="Passenger name"></label>
    <label>Age<input name="age" type="number" min="0" max="120" required placeholder="Age"></label>
    <label>Gender<select name="gender"><option value="">Skip</option><option value="F">Female</option><option value="M">Male</option><option value="O">Other</option></select></label>
    <label>Phone <span class="optional">OPTIONAL</span><input name="phone_number" maxlength="15" placeholder="Phone number"></label>
    <button class="remove-passenger" type="button" aria-label="Remove passenger">×</button>`;
  row.querySelector(".remove-passenger").addEventListener("click", () => {
    if (document.querySelectorAll(".passenger-row").length > 1) row.remove();
  });
  byId("passengers").appendChild(row);
}

function renderBooking(target, booking, manageable = false) {
  const passengers = booking.passengers.map((passenger) => {
    const seat = passenger.seat_number
      ? `${passenger.coach_number} · ${passenger.seat_number} · ${passenger.berth_type.replaceAll("_", " ")}`
      : passenger.passenger_status === "WAITLISTED" ? "Waiting list" : "No seat assigned";
    return `<div class="passenger-ticket"><div><strong>${passenger.full_name}</strong><small>${passenger.passenger_status}</small></div>
      <div class="seat-tag">${seat}</div>
      ${manageable && passenger.passenger_status !== "CANCELLED" ? `<button class="text-button cancel-passenger" data-passenger="${passenger.passenger_id}">Cancel passenger</button>` : ""}
    </div>`;
  }).join("");
  const cancellationMessage = booking.waitlisted_passengers_promoted > 0
    ? `<p class="promotion-note">${booking.waitlisted_passengers_promoted} waitlisted passenger(s) were promoted after seats were released.</p>` : "";
  target.innerHTML = `<div class="ticket-card">
    <div class="ticket-head"><div><span class="heading-caption">BOOKING CONFIRMATION</span><h3>${booking.pnr}</h3></div><span class="status-pill ${booking.booking_status.toLowerCase()}">${booking.booking_status}</span></div>
    <div class="ticket-route"><div><small>FROM</small><strong>${booking.from_station} (${booking.from_code})</strong></div><span>→</span><div><small>TO</small><strong>${booking.to_station} (${booking.to_code})</strong></div></div>
    <div class="ticket-meta"><span>${booking.train_number} · ${booking.train_name}</span><span>${booking.journey_date}</span></div>
    <div class="ticket-passengers">${passengers}</div>
    <div class="fare-note">Fare is not configured in this database demo. Payments are intentionally out of scope.</div>
    ${cancellationMessage}
    ${manageable && booking.booking_status !== "CANCELLED" ? `<button class="cancel-booking button button-light" type="button">Cancel entire booking</button>` : ""}
  </div>`;

  if (manageable) {
    target.querySelectorAll(".cancel-passenger").forEach((button) => button.addEventListener("click", async () => {
      if (!confirm("Cancel this passenger and release their assigned seat?")) return;
      try {
        const updated = await api(`/api/bookings/${encodeURIComponent(booking.pnr)}/passengers/${button.dataset.passenger}`, { method: "DELETE" });
        renderBooking(target, updated, true);
      } catch (error) { showMessage("lookup-message", error.message, "error"); }
    }));
    target.querySelector(".cancel-booking")?.addEventListener("click", async () => {
      if (!confirm("Cancel every passenger on this booking?")) return;
      try {
        const updated = await api(`/api/bookings/${encodeURIComponent(booking.pnr)}`, { method: "DELETE" });
        renderBooking(target, updated, true);
      } catch (error) { showMessage("lookup-message", error.message, "error"); }
    });
  }
}

function renderItinerary(target, itinerary, manageable = false) {
  const legs = itinerary.legs.map((leg, index) => {
    const passengers = leg.passengers.map((passenger) => {
      const seat = passenger.seat_number
        ? `${passenger.coach_number} · ${passenger.seat_number} · ${(passenger.berth_type || "").replaceAll("_", " ")}`
        : passenger.passenger_status;
      return `<div class="passenger-ticket"><div><strong>${passenger.full_name}</strong><small>${passenger.passenger_status}</small></div><div class="seat-tag">${seat}</div></div>`;
    }).join("");
    return `<section class="itinerary-leg"><div class="leg-label">LEG ${String(index + 1).padStart(2, "0")}</div>
      <div class="train-number">${leg.train_number} <span>${leg.train_name}</span></div>
      <div class="ticket-route"><div><small>BOARD</small><strong>${leg.from_station} (${leg.from_code})</strong><small>${formatDate(leg.departure_datetime)}</small></div><span>→</span><div><small>ALIGHT</small><strong>${leg.to_station} (${leg.to_code})</strong><small>${formatDate(leg.arrival_datetime)}</small></div></div>
      <div class="ticket-passengers">${passengers}</div></section>`;
  }).join("");
  target.innerHTML = `<div class="ticket-card"><div class="ticket-head"><div><span class="heading-caption">${manageable ? "ITINERARY DETAILS" : "BOOKING CONFIRMATION"}</span><h3>${itinerary.pnr}</h3></div><span class="status-pill ${itinerary.booking_status.toLowerCase()}">${itinerary.booking_status}</span></div>
    ${legs}<div class="fare-note">One PNR covers all train legs. Each passenger may receive a different seat on each leg.</div>
    ${manageable && itinerary.booking_status !== "CANCELLED" ? `<button class="cancel-booking button button-light" type="button">Cancel entire itinerary</button>` : ""}</div>`;
  if (manageable) target.querySelector(".cancel-booking")?.addEventListener("click", async () => {
    if (!confirm("Cancel every leg and release all assigned seats?")) return;
    try {
      const updated = await api(`/api/itineraries/${encodeURIComponent(itinerary.pnr)}`, { method: "DELETE" });
      renderItinerary(target, updated, true);
    } catch (error) { showMessage("lookup-message", error.message, "error"); }
  });
}

function itineraryCard(itinerary) {
  const legList = itinerary.legs.map((leg, index) => `<div class="itinerary-search-leg"><span class="leg-label">${index ? "TRANSFER" : "BOARD"}</span><strong>${leg.from_code} → ${leg.to_code}</strong><span>${leg.train_number} · ${leg.train_name}</span><span>${formatDate(leg.departure_datetime)} – ${formatDate(leg.arrival_datetime)}</span><span class="availability">${leg.available_seats} seats</span></div>`).join("");
  return `<article class="train-card itinerary-search-card"><div class="train-main"><div class="train-number">Fastest available itinerary <span>${itinerary.transfer_count} transfer(s)</span></div><div class="itinerary-search-legs">${legList}</div><div class="train-times">Arrives ${formatDate(itinerary.arrival_datetime)}</div></div><div class="train-side"><button class="button button-dark choose-itinerary">Select itinerary</button></div></article>`;
}

byId("swap").addEventListener("click", () => {
  const from = byId("from-code").value;
  byId("from-code").value = byId("to-code").value;
  byId("to-code").value = from;
});

byId("search-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const mode = event.submitter?.dataset.mode || "direct";
  state.run = null;
  state.itinerary = null;
  byId("selected-run-copy").textContent = "Search and select a train or itinerary to start a booking.";
  byId("train-results").innerHTML = "";
  showMessage("search-message", mode === "fastest" ? "Finding the earliest-arrival connection…" : "Searching scheduled runs…");
  const query = new URLSearchParams({
    from_code: byId("from-code").value,
    to_code: byId("to-code").value,
    journey_date: byId("journey-date").value,
  });
  try {
    if (mode === "fastest") {
      const itinerary = await api(`/api/itineraries/search?${query}&transfer_minutes=30`);
      if (!itinerary.found) {
        showMessage("search-message", itinerary.message, "empty");
        return;
      }
      showMessage("search-message", `Earliest arrival found with ${itinerary.transfer_count} transfer(s).`, "success");
      byId("train-results").innerHTML = itineraryCard(itinerary);
      byId("train-results").querySelector(".choose-itinerary").addEventListener("click", () => {
        state.itinerary = itinerary;
        byId("selected-run-copy").textContent = `Fastest route · ${itinerary.from_code} to ${itinerary.to_code} · ${itinerary.legs.length} train legs`;
        byId("booking-panel").scrollIntoView({ behavior: "smooth", block: "start" });
      });
    } else {
      const trains = await api(`/api/search?${query}`);
      showMessage("search-message", trains.length ? `${trains.length} train(s) found for this route.` : "No scheduled train serves this station pair on that date.", trains.length ? "success" : "empty");
      byId("train-results").innerHTML = trains.map(trainCard).join("");
      byId("train-results").querySelectorAll(".choose-train").forEach((button) => button.addEventListener("click", () => {
        state.run = trains.find((train) => String(train.run_id) === button.dataset.run);
        byId("selected-run-copy").textContent = `${state.run.train_number} ${state.run.train_name} · ${state.run.from_code} to ${state.run.to_code} · ${state.run.journey_date}`;
        byId("booking-panel").scrollIntoView({ behavior: "smooth", block: "start" });
      }));
    }
  } catch (error) { showMessage("search-message", error.message, "error"); }
});

byId("add-passenger").addEventListener("click", addPassengerRow);
addPassengerRow();

byId("booking-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.run && !state.itinerary) {
    showMessage("booking-message", "Search first, then select a train or itinerary.", "error");
    return;
  }
  const passengers = [...document.querySelectorAll(".passenger-row")].map((row) => ({
    full_name: row.querySelector('[name="full_name"]').value.trim(),
    age: Number(row.querySelector('[name="age"]').value),
    gender: row.querySelector('[name="gender"]').value || null,
    phone_number: row.querySelector('[name="phone_number"]').value.trim() || null,
  }));
  showMessage("booking-message", "Creating booking and allocating seats in one database transaction…");
  try {
    const booking = await api(state.itinerary ? "/api/itineraries" : "/api/bookings", {
      method: "POST",
      body: JSON.stringify(state.itinerary ? {
        legs: state.itinerary.legs.map((leg) => ({ run_id: leg.run_id, from_code: leg.from_code, to_code: leg.to_code })),
        passengers,
        transfer_minutes: state.itinerary.transfer_minutes,
      } : {
        run_id: state.run.run_id,
        from_code: state.run.from_code,
        to_code: state.run.to_code,
        passengers,
      }),
    });
    showMessage("booking-message", "Booking saved. Keep this PNR for lookup and cancellation.", "success");
    if (state.itinerary) renderItinerary(byId("booking-result"), booking);
    else renderBooking(byId("booking-result"), booking);
  } catch (error) { showMessage("booking-message", error.message, "error"); }
});

byId("lookup-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  showMessage("lookup-message", "Looking up booking…");
  try {
    let booking;
    let isItinerary = false;
    try { booking = await api(`/api/bookings/${encodeURIComponent(byId("pnr-input").value.trim())}`); }
    catch (directError) {
      booking = await api(`/api/itineraries/${encodeURIComponent(byId("pnr-input").value.trim())}`);
      isItinerary = true;
    }
    showMessage("lookup-message", "Booking found.", "success");
    if (isItinerary) renderItinerary(byId("lookup-result"), booking, true);
    else renderBooking(byId("lookup-result"), booking, true);
  } catch (error) { byId("lookup-result").innerHTML = ""; showMessage("lookup-message", error.message, "error"); }
});

loadStations();
