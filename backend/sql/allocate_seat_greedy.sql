-- Additive allocator for the simulator API.
-- This leaves public.allocate_seat and the existing schema untouched.
-- Greedy policy: assign the least-flexible eligible seat first. For each
-- candidate, count the still-bookable origin/destination intervals that do
-- not overlap the requested interval or existing occupancy; choose the
-- candidate with the fewest remaining intervals, then break ties by coach
-- and seat number. Every availability check, lock, assignment, and occupancy
-- insert happens in the caller's PostgreSQL transaction.

CREATE OR REPLACE FUNCTION public.allocate_seat_greedy(
    p_booking_id bigint,
    p_passenger_id bigint
) RETURNS bigint
    LANGUAGE plpgsql
AS $$
DECLARE
    v_run_id bigint;
    v_train_id bigint;
    v_from smallint;
    v_to smallint;
    v_status varchar(20);
    v_candidate bigint;
    v_seat_id bigint;
    v_tried bigint[] := ARRAY[]::bigint[];
BEGIN
    SELECT run_id, train_id, from_stop_sequence, to_stop_sequence, booking_status
    INTO v_run_id, v_train_id, v_from, v_to, v_status
    FROM public.bookings
    WHERE booking_id = p_booking_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Booking not found';
    END IF;
    IF v_status NOT IN ('PENDING', 'CONFIRMED', 'WAITLISTED') THEN
        RAISE EXCEPTION 'Booking cannot be allocated';
    END IF;

    PERFORM 1
    FROM public.booking_passengers
    WHERE booking_id = p_booking_id
      AND passenger_id = p_passenger_id
      AND passenger_status = 'PENDING'
      AND seat_id IS NULL;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Invalid passenger allocation request';
    END IF;

    LOOP
        v_candidate := NULL;
        v_seat_id := NULL;

        SELECT s.seat_id
        INTO v_candidate
        FROM public.seats s
        JOIN public.coaches c ON c.coach_id = s.coach_id
        WHERE c.train_id = v_train_id
          AND s.seat_id <> ALL(v_tried)
          AND NOT EXISTS (
              SELECT 1
              FROM public.seat_segment_occupancy o
              WHERE o.run_id = v_run_id
                AND o.seat_id = s.seat_id
                AND o.segment_no >= v_from
                AND o.segment_no < v_to
          )
        ORDER BY (
            SELECT count(*)
            FROM public.train_stops a
            JOIN public.train_stops b
              ON b.train_id = a.train_id
             AND a.stop_sequence < b.stop_sequence
            WHERE a.train_id = v_train_id
              AND (b.stop_sequence <= v_from OR a.stop_sequence >= v_to)
              AND NOT EXISTS (
                  SELECT 1
                  FROM public.seat_segment_occupancy o
                  WHERE o.run_id = v_run_id
                    AND o.seat_id = s.seat_id
                    AND o.segment_no >= a.stop_sequence
                    AND o.segment_no < b.stop_sequence
              )
        ) ASC,
        c.coach_number,
        s.seat_number
        LIMIT 1
        FOR UPDATE OF s SKIP LOCKED;

        IF NOT FOUND THEN
            EXIT;
        END IF;

        -- Recheck after acquiring the seat row lock.
        SELECT v_candidate
        INTO v_seat_id
        WHERE NOT EXISTS (
            SELECT 1
            FROM public.seat_segment_occupancy o
            WHERE o.run_id = v_run_id
              AND o.seat_id = v_candidate
              AND o.segment_no >= v_from
              AND o.segment_no < v_to
        );

        IF FOUND THEN
            UPDATE public.booking_passengers
            SET seat_id = v_seat_id,
                passenger_status = 'CONFIRMED'
            WHERE booking_id = p_booking_id
              AND passenger_id = p_passenger_id
              AND passenger_status = 'PENDING'
              AND seat_id IS NULL;

            IF NOT FOUND THEN
                RAISE EXCEPTION 'Passenger state changed';
            END IF;

            INSERT INTO public.seat_segment_occupancy
                (run_id, seat_id, segment_no, booking_id, passenger_id)
            SELECT v_run_id, v_seat_id, segment_no::smallint,
                   p_booking_id, p_passenger_id
            FROM generate_series(v_from::integer, v_to::integer - 1) AS segment_no;

            UPDATE public.bookings b
            SET booking_status = 'CONFIRMED'
            WHERE b.booking_id = p_booking_id
              AND NOT EXISTS (
                  SELECT 1
                  FROM public.booking_passengers bp
                  WHERE bp.booking_id = p_booking_id
                    AND bp.passenger_status <> 'CONFIRMED'
              );

            RETURN v_seat_id;
        END IF;

        v_tried := array_append(v_tried, v_candidate);
    END LOOP;

    UPDATE public.booking_passengers
    SET passenger_status = 'WAITLISTED'
    WHERE booking_id = p_booking_id
      AND passenger_id = p_passenger_id;

    UPDATE public.bookings
    SET booking_status = 'WAITLISTED'
    WHERE booking_id = p_booking_id;

    RETURN NULL;
END;
$$;

-- FIFO promotion processor paired with the additive greedy allocator above.
-- The original process_waitlist calls allocate_seat, whose status guard in the
-- supplied export does not admit WAITLISTED bookings. This companion keeps
-- the original function unchanged and supports existing waitlisted bookings.
CREATE OR REPLACE FUNCTION public.process_waitlist_greedy(p_run_id bigint)
RETURNS integer
    LANGUAGE plpgsql
AS $$
DECLARE
    v_item record;
    v_seat_id bigint;
    v_promoted_count integer := 0;
BEGIN
    PERFORM pg_advisory_xact_lock(p_run_id);

    FOR v_item IN
        SELECT w.waitlist_id, w.booking_id, w.passenger_id
        FROM public.waitlist_entries w
        JOIN public.booking_passengers bp
          ON bp.booking_id = w.booking_id
         AND bp.passenger_id = w.passenger_id
        JOIN public.bookings b ON b.booking_id = w.booking_id
        WHERE w.run_id = p_run_id
          AND w.status = 'WAITING'
          AND bp.passenger_status = 'WAITLISTED'
          AND bp.seat_id IS NULL
          AND b.booking_status <> 'CANCELLED'
        ORDER BY w.created_at, w.waitlist_id
    LOOP
        v_seat_id := public.allocate_seat_greedy(v_item.booking_id, v_item.passenger_id);

        IF v_seat_id IS NOT NULL THEN
            UPDATE public.waitlist_entries
            SET status = 'PROMOTED',
                promoted_at = now(),
                attempt_count = attempt_count + 1
            WHERE waitlist_id = v_item.waitlist_id
              AND status = 'WAITING';
            v_promoted_count := v_promoted_count + 1;
        ELSE
            UPDATE public.waitlist_entries
            SET attempt_count = attempt_count + 1
            WHERE waitlist_id = v_item.waitlist_id
              AND status = 'WAITING';
        END IF;
    END LOOP;

    RETURN v_promoted_count;
END;
$$;
