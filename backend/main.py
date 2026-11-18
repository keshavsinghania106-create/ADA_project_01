from __future__ import annotations

import secrets
import string
import heapq
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Literal

import psycopg
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from psycopg.rows import dict_row


PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEB_ROOT = PROJECT_ROOT / "frontend"
load_dotenv(PROJECT_ROOT / ".env")

app = FastAPI(title="Railflow Reservation Simulator", version="1.0.0")
app.mount("/assets", StaticFiles(directory=WEB_ROOT / "assets"), name="assets")


def connect() -> psycopg.Connection:
    import os

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise HTTPException(
            status_code=503,
            detail="DATABASE_URL is missing. Copy .env.example to .env and set your PostgreSQL connection.",
        )
    try:
        return psycopg.connect(dsn, row_factory=dict_row)
    except psycopg.OperationalError as exc:
        raise HTTPException(status_code=503, detail="Could not connect to PostgreSQL. Check DATABASE_URL and that the server is running.") from exc


class PassengerInput(BaseModel):
    full_name: str = Field(min_length=1, max_length=100)
    age: int = Field(ge=0, le=120)
    gender: Literal["M", "F", "O"] | None = None
    phone_number: str | None = Field(default=None, max_length=15)

    @field_validator("full_name")
    @classmethod
    def trim_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Passenger name cannot be blank")
        return value


class BookingInput(BaseModel):
    run_id: int = Field(gt=0)
    from_code: str = Field(min_length=1, max_length=10)
    to_code: str = Field(min_length=1, max_length=10)
    passengers: list[PassengerInput] = Field(min_length=1)


class ItineraryLegInput(BaseModel):
    run_id: int = Field(gt=0)
    from_code: str = Field(min_length=1, max_length=10)
    to_code: str = Field(min_length=1, max_length=10)


class ItineraryBookingInput(BaseModel):
    legs: list[ItineraryLegInput] = Field(min_length=2, max_length=6)
    passengers: list[PassengerInput] = Field(min_length=1, max_length=8)
    transfer_minutes: int = Field(default=30, ge=0, le=240)


def _booking_detail(cur: psycopg.Cursor, pnr: str) -> dict:
    cur.execute(
        """
        SELECT b.booking_id, b.pnr, b.booking_status, b.booking_time,
               b.total_fare, b.run_id, r.journey_date,
               t.train_number, t.train_name,
               src.station_code AS from_code, src.station_name AS from_station,
               dst.station_code AS to_code, dst.station_name AS to_station,
               b.from_stop_sequence, b.to_stop_sequence
        FROM public.bookings b
        JOIN public.train_runs r ON r.run_id = b.run_id
        JOIN public.trains t ON t.train_id = b.train_id
        JOIN public.train_stops fs
          ON fs.train_id = b.train_id AND fs.stop_sequence = b.from_stop_sequence
        JOIN public.stations src ON src.station_id = fs.station_id
        JOIN public.train_stops ts
          ON ts.train_id = b.train_id AND ts.stop_sequence = b.to_stop_sequence
        JOIN public.stations dst ON dst.station_id = ts.station_id
        WHERE upper(b.pnr) = upper(%s)
        """,
        (pnr,),
    )
    booking = cur.fetchone()
    if booking is None:
        raise HTTPException(status_code=404, detail="PNR not found")

    cur.execute(
        """
        SELECT p.passenger_id, p.full_name,
               bp.passenger_status, bp.passenger_fare,
               c.coach_number, s.seat_number, s.berth_type
        FROM public.booking_passengers bp
        JOIN public.passengers p ON p.passenger_id = bp.passenger_id
        LEFT JOIN public.seats s ON s.seat_id = bp.seat_id
        LEFT JOIN public.coaches c ON c.coach_id = s.coach_id
        WHERE bp.booking_id = %s
        ORDER BY p.passenger_id
        """,
        (booking["booking_id"],),
    )
    booking["passengers"] = cur.fetchall()
    return booking


def _itinerary_detail(cur: psycopg.Cursor, pnr: str) -> dict:
    cur.execute(
        """
        SELECT journey_booking_id, pnr, booking_status, booking_time, cancelled_at
        FROM public.journey_bookings WHERE upper(pnr) = upper(%s)
        """,
        (pnr,),
    )
    parent = cur.fetchone()
    if parent is None:
        raise HTTPException(status_code=404, detail="Itinerary PNR not found")
    cur.execute(
        """
        SELECT jl.leg_order, b.booking_id, b.booking_status, b.run_id, r.journey_date,
               t.train_number, t.train_name,
               src.station_code AS from_code, src.station_name AS from_station,
               dst.station_code AS to_code, dst.station_name AS to_station,
               (r.journey_date + fs.day_offset::integer + fs.departure_time) AS departure_datetime,
               (r.journey_date + ts.day_offset::integer + ts.arrival_time) AS arrival_datetime
        FROM public.journey_legs jl
        JOIN public.bookings b ON b.booking_id = jl.booking_id
        JOIN public.train_runs r ON r.run_id = b.run_id
        JOIN public.trains t ON t.train_id = b.train_id
        JOIN public.train_stops fs ON fs.train_id = b.train_id AND fs.stop_sequence = b.from_stop_sequence
        JOIN public.stations src ON src.station_id = fs.station_id
        JOIN public.train_stops ts ON ts.train_id = b.train_id AND ts.stop_sequence = b.to_stop_sequence
        JOIN public.stations dst ON dst.station_id = ts.station_id
        WHERE jl.journey_booking_id = %s ORDER BY jl.leg_order
        """,
        (parent["journey_booking_id"],),
    )
    legs = cur.fetchall()
    for leg in legs:
        cur.execute(
            """
            SELECT p.passenger_id, p.full_name, bp.passenger_status,
                   c.coach_number, s.seat_number, s.berth_type
            FROM public.booking_passengers bp
            JOIN public.passengers p ON p.passenger_id = bp.passenger_id
            LEFT JOIN public.seats s ON s.seat_id = bp.seat_id
            LEFT JOIN public.coaches c ON c.coach_id = s.coach_id
            WHERE bp.booking_id = %s ORDER BY p.passenger_id
            """,
            (leg["booking_id"],),
        )
        leg["passengers"] = cur.fetchall()
    parent["legs"] = legs
    return parent


def _new_pnr(prefix: str) -> str:
    return prefix + "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))


@app.get("/")
def home() -> FileResponse:
    return FileResponse(WEB_ROOT / "index.html")


@app.get("/api/health")
def health() -> dict:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 AS database_connected")
        return {"ok": True, "database": cur.fetchone()["database_connected"] == 1}


@app.get("/api/stations")
def stations() -> list[dict]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT station_code, station_name, city, state
            FROM public.stations
            ORDER BY station_name, station_code
            """
        )
        return cur.fetchall()


@app.get("/api/search")
def search_trains(
    from_code: str = Query(min_length=1, max_length=10),
    to_code: str = Query(min_length=1, max_length=10),
    journey_date: date = Query(),
) -> list[dict]:
    if from_code.upper() == to_code.upper():
        raise HTTPException(status_code=400, detail="Choose different boarding and destination stations")

    # Same segment interval convention as get_available_seats: [from, to).
    # The scalar count keeps trains visible when the count reaches zero, so
    # the existing waitlist flow can still be demonstrated.
    sql = """
        SELECT r.run_id, t.train_id, t.train_number, t.train_name,
               src.station_code AS from_code, src.station_name AS from_station,
               dst.station_code AS to_code, dst.station_name AS to_station,
               fs.stop_sequence AS from_sequence,
               ts.stop_sequence AS to_sequence,
               r.journey_date,
               (r.journey_date + fs.day_offset::integer + fs.departure_time) AS departure_datetime,
               (r.journey_date + ts.day_offset::integer + ts.arrival_time) AS arrival_datetime,
               (
                   SELECT count(*)
                   FROM public.coaches c
                   JOIN public.seats s ON s.coach_id = c.coach_id
                   WHERE c.train_id = t.train_id
                     AND NOT EXISTS (
                         SELECT 1 FROM public.seat_segment_occupancy o
                         WHERE o.run_id = r.run_id AND o.seat_id = s.seat_id
                           AND o.segment_no >= fs.stop_sequence
                           AND o.segment_no < ts.stop_sequence
                     )
               )::integer AS available_seats
        FROM public.train_runs r
        JOIN public.trains t ON t.train_id = r.train_id
        JOIN public.train_stops fs ON fs.train_id = t.train_id
        JOIN public.stations src ON src.station_id = fs.station_id
        JOIN public.train_stops ts ON ts.train_id = t.train_id
        JOIN public.stations dst ON dst.station_id = ts.station_id
        WHERE src.station_code = upper(%s)
          AND dst.station_code = upper(%s)
          AND fs.stop_sequence < ts.stop_sequence
          AND r.journey_date = %s
          AND r.run_status = 'SCHEDULED'
          AND t.operational_status = 'ACTIVE'
        ORDER BY departure_datetime, t.train_number
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, (from_code, to_code, journey_date))
        return cur.fetchall()


@app.get("/api/itineraries/search")
def search_itinerary(
    from_code: str = Query(min_length=1, max_length=10),
    to_code: str = Query(min_length=1, max_length=10),
    journey_date: date = Query(),
    transfer_minutes: int = Query(default=30, ge=0, le=240),
) -> dict:
    """Find one earliest-arrival itinerary using scheduled rides as Dijkstra edges."""
    from_code, to_code = from_code.upper(), to_code.upper()
    if from_code == to_code:
        raise HTTPException(status_code=400, detail="Choose different boarding and destination stations")
    sql = """
        SELECT r.run_id, r.train_id, t.train_number, t.train_name,
               fs.station_id AS from_station_id, src.station_code AS from_code,
               src.station_name AS from_station, fs.stop_sequence AS from_sequence,
               ts.station_id AS to_station_id, dst.station_code AS to_code,
               dst.station_name AS to_station, ts.stop_sequence AS to_sequence,
               r.journey_date,
               (r.journey_date + fs.day_offset::integer + fs.departure_time) AS departure_datetime,
               (r.journey_date + ts.day_offset::integer + ts.arrival_time) AS arrival_datetime,
               (
                   SELECT count(*) FROM public.coaches c
                   JOIN public.seats s ON s.coach_id = c.coach_id
                   WHERE c.train_id = t.train_id
                     AND NOT EXISTS (
                         SELECT 1 FROM public.seat_segment_occupancy o
                         WHERE o.run_id = r.run_id AND o.seat_id = s.seat_id
                           AND o.segment_no >= fs.stop_sequence AND o.segment_no < ts.stop_sequence
                     )
               )::integer AS available_seats
        FROM public.train_runs r
        JOIN public.trains t ON t.train_id = r.train_id
        JOIN public.train_stops fs ON fs.train_id = t.train_id
        JOIN public.stations src ON src.station_id = fs.station_id
        JOIN public.train_stops ts ON ts.train_id = t.train_id
        JOIN public.stations dst ON dst.station_id = ts.station_id
        WHERE fs.stop_sequence < ts.stop_sequence
          AND r.journey_date BETWEEN %s AND %s
          AND r.run_status = 'SCHEDULED' AND t.operational_status = 'ACTIVE'
          AND src.station_code <> dst.station_code
        ORDER BY departure_datetime, arrival_datetime, r.run_id
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, (journey_date, journey_date + timedelta(days=3)))
        rides = [ride for ride in cur.fetchall() if ride["available_seats"] > 0]
        cur.execute("SELECT station_id FROM public.stations WHERE station_code = %s", (from_code,))
        origin = cur.fetchone()
        cur.execute("SELECT station_id FROM public.stations WHERE station_code = %s", (to_code,))
        destination = cur.fetchone()
    if origin is None or destination is None:
        raise HTTPException(status_code=404, detail="Unknown station code")
    adjacency: dict[int, list[dict]] = {}
    for ride in rides:
        adjacency.setdefault(ride["from_station_id"], []).append(ride)

    # Earliest arrival is the primary cost; transfer count breaks equal-time ties.
    start_time = datetime.now().replace(tzinfo=None) if journey_date == date.today() else datetime.combine(journey_date, time.min)
    best: dict[int, tuple[datetime, int]] = {origin["station_id"]: (start_time, 0)}
    previous: dict[int, tuple[int, dict]] = {}
    queue: list[tuple[datetime, int, int]] = [(start_time, 0, origin["station_id"])]
    transfer_wait = timedelta(minutes=transfer_minutes)
    while queue:
        arrived, transfers, station_id = heapq.heappop(queue)
        if best.get(station_id) != (arrived, transfers):
            continue
        if station_id == destination["station_id"]:
            break
        earliest_departure = arrived if station_id == origin["station_id"] else arrived + transfer_wait
        for ride in adjacency.get(station_id, []):
            if ride["departure_datetime"] < earliest_departure:
                continue
            next_transfers = transfers + (0 if transfers == 0 and station_id == origin["station_id"] else 1)
            candidate = (ride["arrival_datetime"], next_transfers)
            next_station = ride["to_station_id"]
            if candidate < best.get(next_station, (datetime.max, 10**9)):
                best[next_station] = candidate
                previous[next_station] = (station_id, ride)
                heapq.heappush(queue, (candidate[0], candidate[1], next_station))

    if destination["station_id"] not in best:
        return {"found": False, "legs": [], "message": "No seat-available itinerary found with the selected transfer time."}
    path = []
    node = destination["station_id"]
    while node != origin["station_id"]:
        prior_station, ride = previous[node]
        path.append(ride)
        node = prior_station
    path.reverse()
    return {
        "found": True,
        "from_code": from_code,
        "to_code": to_code,
        "departure_datetime": path[0]["departure_datetime"],
        "arrival_datetime": path[-1]["arrival_datetime"],
        "transfer_count": max(0, len(path) - 1),
        "transfer_minutes": transfer_minutes,
        "legs": path,
    }


@app.post("/api/bookings", status_code=201)
def create_booking(payload: BookingInput) -> dict:
    from_code = payload.from_code.upper()
    to_code = payload.to_code.upper()

    with connect() as conn, conn.cursor() as cur:
        # Resolve and validate the actual run and ordered stop pair.
        cur.execute(
            """
            SELECT r.run_id, r.train_id, r.journey_date,
                   fs.stop_sequence AS from_sequence,
                   ts.stop_sequence AS to_sequence
            FROM public.train_runs r
            JOIN public.trains t ON t.train_id = r.train_id
            JOIN public.train_stops fs ON fs.train_id = t.train_id
            JOIN public.stations src ON src.station_id = fs.station_id
            JOIN public.train_stops ts ON ts.train_id = t.train_id
            JOIN public.stations dst ON dst.station_id = ts.station_id
            WHERE r.run_id = %s
              AND src.station_code = %s
              AND dst.station_code = %s
              AND fs.stop_sequence < ts.stop_sequence
              AND r.run_status = 'SCHEDULED'
              AND t.operational_status = 'ACTIVE'
            FOR SHARE OF r
            """,
            (payload.run_id, from_code, to_code),
        )
        route = cur.fetchone()
        if route is None:
            raise HTTPException(status_code=400, detail="That train run does not serve the selected stations in that order")

        pnr = "RF" + "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))
        cur.execute(
            """
            INSERT INTO public.bookings
                (pnr, run_id, train_id, from_stop_sequence, to_stop_sequence,
                 booking_status, total_fare)
            VALUES (%s, %s, %s, %s, %s, 'PENDING', 0)
            RETURNING booking_id
            """,
            (pnr, route["run_id"], route["train_id"], route["from_sequence"], route["to_sequence"]),
        )
        booking_id = cur.fetchone()["booking_id"]

        for passenger in payload.passengers:
            cur.execute(
                """
                INSERT INTO public.passengers (full_name, age, gender, phone_number)
                VALUES (%s, %s, %s, %s)
                RETURNING passenger_id
                """,
                (passenger.full_name.strip(), passenger.age, passenger.gender, passenger.phone_number),
            )
            passenger_id = cur.fetchone()["passenger_id"]
            cur.execute(
                """
                INSERT INTO public.booking_passengers
                    (booking_id, passenger_id, passenger_status, passenger_fare)
                VALUES (%s, %s, 'PENDING', 0)
                """,
                (booking_id, passenger_id),
            )

            try:
                cur.execute("SELECT public.allocate_seat_greedy(%s, %s) AS seat_id", (booking_id, passenger_id))
            except psycopg.errors.UndefinedFunction as exc:
                raise HTTPException(
                    status_code=503,
                    detail="Greedy allocator is not installed. Run backend/sql/allocate_seat_greedy.sql once in pgAdmin, then retry.",
                ) from exc
            seat_id = cur.fetchone()["seat_id"]
            if seat_id is None:
                cur.execute(
                    """
                    INSERT INTO public.waitlist_entries
                        (booking_id, passenger_id, run_id, train_id,
                         from_stop_sequence, to_stop_sequence, status)
                    VALUES (%s, %s, %s, %s, %s, %s, 'WAITING')
                    """,
                    (booking_id, passenger_id, route["run_id"], route["train_id"], route["from_sequence"], route["to_sequence"]),
                )

        cur.execute(
            """
            UPDATE public.bookings b
            SET booking_status = CASE
                WHEN EXISTS (SELECT 1 FROM public.booking_passengers bp WHERE bp.booking_id = b.booking_id AND bp.passenger_status = 'WAITLISTED') THEN 'WAITLISTED'
                WHEN EXISTS (SELECT 1 FROM public.booking_passengers bp WHERE bp.booking_id = b.booking_id AND bp.passenger_status = 'PENDING') THEN 'PENDING'
                ELSE 'CONFIRMED'
            END
            WHERE b.booking_id = %s
            """,
            (booking_id,),
        )
        return _booking_detail(cur, pnr)


@app.post("/api/itineraries", status_code=201)
def create_itinerary(payload: ItineraryBookingInput) -> dict:
    """Create all legs and their seat assignments atomically under one public PNR."""
    routes = []
    with connect() as conn, conn.cursor() as cur:
        for leg in payload.legs:
            cur.execute(
                """
                SELECT r.run_id, r.train_id, r.journey_date,
                       fs.stop_sequence AS from_sequence, ts.stop_sequence AS to_sequence,
                       src.station_id AS from_station_id, src.station_code AS from_code,
                       dst.station_id AS to_station_id, dst.station_code AS to_code,
                       (r.journey_date + fs.day_offset::integer + fs.departure_time) AS departure_datetime,
                       (r.journey_date + ts.day_offset::integer + ts.arrival_time) AS arrival_datetime
                FROM public.train_runs r
                JOIN public.trains t ON t.train_id = r.train_id
                JOIN public.train_stops fs ON fs.train_id = t.train_id
                JOIN public.stations src ON src.station_id = fs.station_id
                JOIN public.train_stops ts ON ts.train_id = t.train_id
                JOIN public.stations dst ON dst.station_id = ts.station_id
                WHERE r.run_id = %s AND src.station_code = upper(%s) AND dst.station_code = upper(%s)
                  AND fs.stop_sequence < ts.stop_sequence
                  AND r.run_status = 'SCHEDULED' AND t.operational_status = 'ACTIVE'
                FOR SHARE OF r
                """,
                (leg.run_id, leg.from_code, leg.to_code),
            )
            route = cur.fetchone()
            if route is None:
                raise HTTPException(status_code=409, detail="A selected train leg is no longer scheduled for those stations. Search again.")
            routes.append(route)

        for index in range(1, len(routes)):
            prev, current = routes[index - 1], routes[index]
            if prev["to_station_id"] != current["from_station_id"]:
                raise HTTPException(status_code=400, detail="Itinerary legs do not connect at the same station")
            if current["departure_datetime"] < prev["arrival_datetime"] + timedelta(minutes=payload.transfer_minutes):
                raise HTTPException(status_code=400, detail=f"The connection does not allow the required {payload.transfer_minutes}-minute transfer buffer")

        pnr = _new_pnr("RF")
        cur.execute(
            "INSERT INTO public.journey_bookings (pnr, booking_status) VALUES (%s, 'PENDING') RETURNING journey_booking_id",
            (pnr,),
        )
        journey_booking_id = cur.fetchone()["journey_booking_id"]

        passenger_ids = []
        for passenger in payload.passengers:
            cur.execute(
                "INSERT INTO public.passengers (full_name, age, gender, phone_number) VALUES (%s, %s, %s, %s) RETURNING passenger_id",
                (passenger.full_name.strip(), passenger.age, passenger.gender, passenger.phone_number),
            )
            passenger_ids.append(cur.fetchone()["passenger_id"])

        for leg_order, route in enumerate(routes, start=1):
            internal_pnr = _new_pnr("LG")
            cur.execute(
                """
                INSERT INTO public.bookings
                    (pnr, run_id, train_id, from_stop_sequence, to_stop_sequence, booking_status, total_fare)
                VALUES (%s, %s, %s, %s, %s, 'PENDING', 0) RETURNING booking_id
                """,
                (internal_pnr, route["run_id"], route["train_id"], route["from_sequence"], route["to_sequence"]),
            )
            booking_id = cur.fetchone()["booking_id"]
            cur.execute(
                "INSERT INTO public.journey_legs (journey_booking_id, leg_order, booking_id) VALUES (%s, %s, %s)",
                (journey_booking_id, leg_order, booking_id),
            )
            for passenger_id in passenger_ids:
                cur.execute(
                    "INSERT INTO public.booking_passengers (booking_id, passenger_id, passenger_status, passenger_fare) VALUES (%s, %s, 'PENDING', 0)",
                    (booking_id, passenger_id),
                )
                try:
                    cur.execute("SELECT public.allocate_seat_greedy(%s, %s) AS seat_id", (booking_id, passenger_id))
                except psycopg.errors.UndefinedFunction as exc:
                    raise HTTPException(status_code=503, detail="Greedy allocator is not installed. Run backend/sql/allocate_seat_greedy.sql in pgAdmin.") from exc
                if cur.fetchone()["seat_id"] is None:
                    raise HTTPException(status_code=409, detail="Not enough seats remain for every passenger on every leg. No partial itinerary was saved; reduce passengers, choose another route, or search again.")

        cur.execute("UPDATE public.journey_bookings SET booking_status = 'CONFIRMED' WHERE journey_booking_id = %s", (journey_booking_id,))
        return _itinerary_detail(cur, pnr)


@app.get("/api/bookings/{pnr}")
def get_booking(pnr: str) -> dict:
    with connect() as conn, conn.cursor() as cur:
        return _booking_detail(cur, pnr)


@app.get("/api/itineraries/{pnr}")
def get_itinerary(pnr: str) -> dict:
    with connect() as conn, conn.cursor() as cur:
        return _itinerary_detail(cur, pnr)


@app.delete("/api/itineraries/{pnr}")
def cancel_itinerary(pnr: str) -> dict:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT journey_booking_id, booking_status FROM public.journey_bookings WHERE upper(pnr) = upper(%s) FOR UPDATE", (pnr,))
        parent = cur.fetchone()
        if parent is None:
            raise HTTPException(status_code=404, detail="Itinerary PNR not found")
        if parent["booking_status"] == "CANCELLED":
            return _itinerary_detail(cur, pnr)
        cur.execute(
            "SELECT b.booking_id, b.run_id FROM public.journey_legs jl JOIN public.bookings b ON b.booking_id = jl.booking_id WHERE jl.journey_booking_id = %s ORDER BY jl.leg_order",
            (parent["journey_booking_id"],),
        )
        legs = cur.fetchall()
        for leg in legs:
            cur.execute("SELECT public.cancel_booking(%s) AS cancelled", (leg["booking_id"],))
        cur.execute("UPDATE public.journey_bookings SET booking_status = 'CANCELLED', cancelled_at = now() WHERE journey_booking_id = %s", (parent["journey_booking_id"],))
        for run_id in sorted({leg["run_id"] for leg in legs}):
            cur.execute("SELECT public.process_waitlist_greedy(%s)", (run_id,))
        return _itinerary_detail(cur, pnr)


@app.delete("/api/bookings/{pnr}/passengers/{passenger_id}")
def cancel_passenger(pnr: str, passenger_id: int) -> dict:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT booking_id, run_id FROM public.bookings WHERE upper(pnr) = upper(%s)", (pnr,))
        booking = cur.fetchone()
        if booking is None:
            raise HTTPException(status_code=404, detail="PNR not found")
        cur.execute(
            "SELECT public.cancel_passenger(%s, %s) AS cancelled",
            (booking["booking_id"], passenger_id),
        )
        cancelled = cur.fetchone()["cancelled"]
        cur.execute(
            """
            UPDATE public.waitlist_entries
            SET status = 'CANCELLED', cancelled_at = now()
            WHERE booking_id = %s AND passenger_id = %s AND status = 'WAITING'
            """,
            (booking["booking_id"], passenger_id),
        )
        cur.execute("SELECT public.process_waitlist_greedy(%s) AS promoted_count", (booking["run_id"],))
        promoted = cur.fetchone()["promoted_count"]
        result = _booking_detail(cur, pnr)
        result["cancelled_now"] = cancelled
        result["waitlisted_passengers_promoted"] = promoted
        return result


@app.delete("/api/bookings/{pnr}")
def cancel_booking(pnr: str) -> dict:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT booking_id, run_id FROM public.bookings WHERE upper(pnr) = upper(%s)", (pnr,))
        booking = cur.fetchone()
        if booking is None:
            raise HTTPException(status_code=404, detail="PNR not found")
        cur.execute("SELECT public.cancel_booking(%s) AS cancelled", (booking["booking_id"],))
        cancelled = cur.fetchone()["cancelled"]
        cur.execute(
            """
            UPDATE public.waitlist_entries
            SET status = 'CANCELLED', cancelled_at = now()
            WHERE booking_id = %s AND status = 'WAITING'
            """,
            (booking["booking_id"],),
        )
        cur.execute("SELECT public.process_waitlist_greedy(%s) AS promoted_count", (booking["run_id"],))
        promoted = cur.fetchone()["promoted_count"]
        result = _booking_detail(cur, pnr)
        result["cancelled_now"] = cancelled
        result["waitlisted_passengers_promoted"] = promoted
        return result
