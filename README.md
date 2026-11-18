# Railflow reservation simulator

This is a small FastAPI application and browser UI wired to the PostgreSQL objects in the supplied `database.sql` export. The export was inspected as read-only reference material. It contains both schema definitions and sample table data, so it is intentionally not copied into this project.

## Architecture

```text
Browser (frontend/index.html, CSS, JavaScript)
          │ same-origin JSON requests
          ▼
FastAPI (backend/main.py)
          │ psycopg, parameterized SQL, one transaction per write
          ▼
Existing PostgreSQL database
  cancel_booking / cancel_passenger
  plus additive allocate_seat_greedy / process_waitlist_greedy functions
  plus additive journey_bookings / journey_legs tables for connected itineraries
```

The application does not apply the database dump or run migrations automatically. Existing tables and functions remain in place. One additive function is provided because the existing `allocate_seat` chooses the first seat in coach/seat order and cannot accept a preferred greedy candidate. The new function uses the same segment occupancy table, row locks, trigger, and transaction boundaries while ranking seats by how many valid station-to-station journeys remain after the proposed assignment.

## One-time database setup

1. In pgAdmin, connect to the same database that contains the Railflow tables.
2. Open Query Tool for that database.
3. Open and execute `backend/sql/allocate_seat_greedy.sql` once. It only creates or replaces the new `public.allocate_seat_greedy(bigint, bigint)` and `public.process_waitlist_greedy(bigint)` functions. It does not drop or alter existing tables or functions.
4. Open and execute `backend/sql/create_journey_tables.sql` once in pgAdmin. It adds two tables that group existing per-train bookings under one itinerary PNR. It does not change existing tables, functions, triggers, or occupancy records. Its `IF NOT EXISTS` statements make repeat execution safe.
5. Confirm your app database role can read/write the tables and execute the existing functions and the new functions.

The supplied export contains table data as well as DDL. Do not restore it over the live project database just to install the application.

## Run locally on Windows

1. Copy `.env.example` to `.env` and set `DATABASE_URL` to the same PostgreSQL connection used in pgAdmin. URL-encode special characters in the password.
2. From this project folder, create and activate a virtual environment, then install dependencies:

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install -r backend\requirements.txt
   ```

3. Start the server:

   ```powershell
   python -m uvicorn backend.main:app --reload
   ```

4. Open `http://127.0.0.1:8000`. The API health endpoint is `http://127.0.0.1:8000/api/health`; interactive API docs are at `http://127.0.0.1:8000/docs`.

## Evaluation walkthrough

1. Open the app and choose two stations and a date with a scheduled run. The station selectors are populated from `public.stations`.
2. Use **Direct trains** to see every active scheduled train serving the selected stations in order, including runs with zero currently available seats. Use **Fastest route** for a timetable-aware Dijkstra search. It returns the earliest-arrival route with available seats, allows runs dated through the next three calendar days, and requires a 30-minute transfer window.
3. Select a run, enter one or more passengers, and reserve. The app creates a booking and passenger records in one database transaction, then allocates each passenger with `allocate_seat_greedy`. The PNR and each passenger's confirmation or waitlist status are shown.
4. In pgAdmin, inspect `bookings`, `booking_passengers`, and `seat_segment_occupancy`. Occupancy is written for each segment in `[from_stop_sequence, to_stop_sequence)`, so a seat may still be used on a non-overlapping part of the same run.
5. Copy the PNR into “Manage a booking”. Cancel one passenger to show that only that passenger's occupancy is released, or cancel the whole booking. The API calls the existing cancellation function and then `process_waitlist_greedy` in the same transaction so queued passengers can be promoted.
6. Search the same journey again. The released seat should be reflected in availability after the transaction commits.

### Connected itinerary (Dijkstra) demonstration

1. In pgAdmin, execute `backend/sql/seed_demo_connection.sql` once. It adds one demo connector train, one 12-seat coach, and scheduled runs on the same dates as existing train 12005. It leaves existing records untouched and can be rerun safely.
2. In the UI, choose **AGC** as the origin, **LKO** as the destination, and a date for which train 12005 has a scheduled run (the current sample includes November 2026). Choose **Fastest route**. The expected route is train 12005 from AGC to CNB, then demo train 99001 from CNB to LKO. For the November 18 run of 12005, it arrives at CNB at 01:00 on November 19; train 99001 leaves at 02:00, giving a 60-minute transfer.
3. The API loads scheduled train-stop pairs and current segment-aware free-seat counts from PostgreSQL.
4. Each possible ride from a station to a later station on one run is a directed edge. Its departure and arrival timestamps come from `train_runs`, `train_stops`, and each stop's `day_offset`. Dijkstra's priority queue processes the station with the earliest known arrival. A connecting train must depart at least 30 minutes after the previous leg arrives. Equal arrival times prefer fewer transfers.
5. Select the route and enter passengers. The backend revalidates each run, station pair, leg connection, transfer time, and seat allocation. It creates one normal `public.bookings` row per train leg, connects these rows to `journey_bookings` and `journey_legs`, then calls the existing greedy allocator for every passenger on every leg.
6. Creating all legs and assigning all seats happens in one PostgreSQL transaction. If any leg loses its seats to a concurrent request, the whole operation rolls back, so no partial itinerary or occupancy is saved. The UI asks the user to search again. A successful trip has one public `RF...` PNR; its per-train booking rows have internal `LG...` PNRs.
7. Look up the public PNR under **Manage a booking** to see all train legs, times, and per-leg passenger seats. Cancelling the itinerary calls the existing `cancel_booking` function for every leg, releases their occupancy, then runs waitlist promotion for affected train runs.

This is a timetable-aware **earliest-arrival** Dijkstra implementation, not distance minimization: the current schema has schedule times and day offsets but no route-distance field. It returns one best available route, searches from the selected date through three days afterward, uses a 30-minute transfer rule, and allows at most six legs/eight passengers. Direct booking and its waitlist behavior stay as before. Connected itineraries are confirmed all-or-nothing and do not enter a waitlist; a seat race returns a conflict response.

### Fast waitlist-promotion demonstration

When a run has 144 seats, do not create 144 UI bookings. Use `backend/sql/demo_fill_interval.sql` in pgAdmin to create one synthetic booking containing one passenger for each seat currently free on a selected run and interval. The script defaults to train `12002`, `NDLS` → `PNBE`, on `2026-11-06`; edit the route/date values at the top if your demo run differs. It invokes the greedy allocator per passenger, so the occupancy trigger and segment checks still run. The script only inserts demo rows and prints the generated PNR in pgAdmin's Messages panel. Set `v_seats_to_leave_free` to `0` for waitlist promotion or `1` for the concurrency demo.

After the script succeeds, make one normal passenger booking for the exact same run/date/station pair through the UI. It should be waitlisted. Then look up the PNR printed by the script and cancel that whole demo booking in the UI; the freed seats should allow the waitlist processor to promote the waiting passenger. The demo booking is persistent until cancelled, and cancellation keeps its booking/passenger history while removing seat occupancy.

### Concurrent booking demonstration

No user accounts are implemented or required. For a visible conflict test, set `v_seats_to_leave_free := 1` in the same SQL script, run it, then submit two one-passenger bookings for the same run/date/station pair from two browser sessions at nearly the same time. With only one seat free, at most one request can confirm; the other should waitlist. The allocator locks a candidate seat row with `FOR UPDATE ... SKIP LOCKED`, rechecks occupancy, and writes occupancy inside the booking transaction. The unique occupancy key is an additional database guard. Two manually clicked requests may not overlap in time, so this setup proves the capacity conflict outcome reliably but does not guarantee the requests reached the lock at the same instant. For a strict race demonstration, use two parallel HTTP requests to `/api/bookings` and compare their responses. Cancel the confirmed PNR to show the waiting booking being promoted.

## Booking and greedy allocation flow

1. Search resolves station codes to stop sequences for each run. Availability tests occupancy only on segments where `segment_no >= boarding_sequence AND segment_no < destination_sequence`, matching `get_available_seats` in the supplied SQL.
2. On booking, the API validates the selected run and route again; it does not trust only the earlier search result. It inserts the booking and passenger master/association rows, then invokes the allocator for each passenger before committing.
3. The greedy function considers seats that are free across the requested segment interval. It scores each candidate by counting station-pair intervals that would remain usable after the booking, chooses the seat with the fewest remaining options, and breaks ties by coach and seat number. This “most constrained seat first” rule preserves more flexible seats for later, longer journeys.
4. It locks the selected seat with `FOR UPDATE ... SKIP LOCKED`, rechecks the requested segments, assigns the passenger, and inserts one occupancy row per segment. The existing occupancy trigger and unique key remain the final validation guard. If there is no eligible seat, the passenger and booking are marked waitlisted and a FIFO `waitlist_entries` row is created.
5. All rows for a booking and its allocations commit together. A database error rolls the entire request back, avoiding a half-created PNR.

## Scope and known limits

- No payment processing, login, admin tools, or external deployment is included. Fare columns are set to zero because the supplied schema has no fare rule or pricing table; the UI labels this clearly.
- The original allocator and waitlist processor remain unchanged. New booking allocations and FIFO waitlist promotions use the additive greedy functions. The companion processor is needed because the supplied first-fit allocator rejects bookings whose status is already `WAITLISTED`.
- Connected itinerary booking requires the additive `journey_bookings` and `journey_legs` tables. Execute the new SQL script in pgAdmin before using that feature; the application does not migrate the database automatically.
- The application has not been tested against the user's running PostgreSQL instance in this workspace. The database connection, role grants, additive function setup, and end-to-end booking flow must be confirmed in the user's environment before claiming a live demonstration works.
