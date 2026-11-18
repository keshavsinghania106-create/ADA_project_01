-- DEMONSTRATION DATA ONLY — run manually in pgAdmin on railway_simulator.
-- Creates one persistent synthetic booking with one passenger per seat that
-- is currently free on the selected run and segment interval, except for the
-- number you choose to leave free. It uses the
-- greedy allocator, occupancy trigger, and existing constraints. It does not
-- update or delete existing rows. Cancel the generated PNR in the app after
-- the waitlist-promotion demonstration to release the seats.

DO $$
DECLARE
    -- Edit these four values to match the route/date you will demonstrate.
    v_train_number varchar(10) := '12002';
    v_journey_date date := DATE '2026-11-06';
    v_from_code varchar(10) := 'NDLS';
    v_to_code varchar(10) := 'PNBE';
    -- Use 0 for the waitlist-promotion demo, or 1 for the concurrency demo.
    v_seats_to_leave_free bigint := 0;

    v_run_id bigint;
    v_train_id bigint;
    v_from_sequence smallint;
    v_to_sequence smallint;
    v_free_seats bigint;
    v_passengers_to_create bigint;
    v_booking_id bigint;
    v_passenger_id bigint;
    v_seat_id bigint;
    v_pnr varchar(10);
BEGIN
    SELECT r.run_id, t.train_id, fs.stop_sequence, ts.stop_sequence
    INTO v_run_id, v_train_id, v_from_sequence, v_to_sequence
    FROM public.train_runs r
    JOIN public.trains t ON t.train_id = r.train_id
    JOIN public.train_stops fs ON fs.train_id = t.train_id
    JOIN public.stations src ON src.station_id = fs.station_id
    JOIN public.train_stops ts ON ts.train_id = t.train_id
    JOIN public.stations dst ON dst.station_id = ts.station_id
    WHERE t.train_number = v_train_number
      AND r.journey_date = v_journey_date
      AND r.run_status = 'SCHEDULED'
      AND t.operational_status = 'ACTIVE'
      AND src.station_code = v_from_code
      AND dst.station_code = v_to_code
      AND fs.stop_sequence < ts.stop_sequence;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'No active scheduled run found for train %, date %, route % to %',
            v_train_number, v_journey_date, v_from_code, v_to_code;
    END IF;

    SELECT count(*)
    INTO v_free_seats
    FROM public.coaches c
    JOIN public.seats s ON s.coach_id = c.coach_id
    WHERE c.train_id = v_train_id
      AND NOT EXISTS (
          SELECT 1
          FROM public.seat_segment_occupancy o
          WHERE o.run_id = v_run_id
            AND o.seat_id = s.seat_id
            AND o.segment_no >= v_from_sequence
            AND o.segment_no < v_to_sequence
      );

    IF v_free_seats <= v_seats_to_leave_free THEN
        RAISE EXCEPTION 'There are % seats free; cannot leave % free and still seed demo passengers',
            v_free_seats, v_seats_to_leave_free;
    END IF;

    v_passengers_to_create := v_free_seats - v_seats_to_leave_free;

    v_pnr := 'DEMO' || upper(substr(md5(random()::text || clock_timestamp()::text), 1, 6));

    INSERT INTO public.bookings
        (pnr, run_id, train_id, from_stop_sequence, to_stop_sequence,
         booking_status, total_fare)
    VALUES
        (v_pnr, v_run_id, v_train_id, v_from_sequence, v_to_sequence,
         'PENDING', 0)
    RETURNING booking_id INTO v_booking_id;

    WITH created_passengers AS (
        INSERT INTO public.passengers (full_name, age)
        SELECT format('DEMO %s PAX-%s', v_pnr, g.n), 25
        FROM generate_series(1::bigint, v_passengers_to_create) AS g(n)
        RETURNING passenger_id
    )
    INSERT INTO public.booking_passengers
        (booking_id, passenger_id, passenger_status, passenger_fare)
    SELECT v_booking_id, passenger_id, 'PENDING', 0
    FROM created_passengers;

    FOR v_passenger_id IN
        SELECT bp.passenger_id
        FROM public.booking_passengers bp
        WHERE bp.booking_id = v_booking_id
        ORDER BY bp.passenger_id
    LOOP
        SELECT public.allocate_seat_greedy(v_booking_id, v_passenger_id)
        INTO v_seat_id;

        IF v_seat_id IS NULL THEN
            RAISE EXCEPTION 'A seat became unavailable while filling the demo interval; transaction rolled back';
        END IF;
    END LOOP;

    RAISE NOTICE 'Demo booking created: PNR=%, passengers=%, train=%, date=%, route=% to %',
        v_pnr, v_passengers_to_create, v_train_number, v_journey_date, v_from_code, v_to_code;
    RAISE NOTICE 'Seats intentionally left free on this interval: %', v_seats_to_leave_free;
END;
$$;
