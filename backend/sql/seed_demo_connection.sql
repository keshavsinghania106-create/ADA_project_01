-- Additive, repeatable demo data for a real two-train connection.
-- Route used in the demo: AGC --(existing 12005)--> CNB --(99001)--> LKO.
-- For 12005 on 2026-11-18, CNB arrival is 2026-11-19 01:00.
-- The connector leaves CNB at 02:00, allowing a 60-minute transfer.
-- This script does not change existing trains, schedules, bookings, or occupancy.

BEGIN;

DO $$
DECLARE
    v_train_id bigint := 99001;
    v_cnb bigint;
    v_lko bigint;
    v_coach_id bigint;
    v_source_run_count integer;
BEGIN
    SELECT station_id INTO v_cnb FROM public.stations WHERE station_code = 'CNB';
    SELECT station_id INTO v_lko FROM public.stations WHERE station_code = 'LKO';
    IF v_cnb IS NULL OR v_lko IS NULL THEN
        RAISE EXCEPTION 'Required demo stations CNB and LKO must exist';
    END IF;

    SELECT count(*) INTO v_source_run_count
    FROM public.train_runs r
    JOIN public.trains t ON t.train_id = r.train_id
    WHERE t.train_number = '12005' AND r.run_status = 'SCHEDULED';
    IF v_source_run_count = 0 THEN
        RAISE EXCEPTION 'Scheduled runs for source train 12005 were not found';
    END IF;

    INSERT INTO public.trains
        (train_id, train_number, train_name, source_station_id,
         destination_station_id, total_coaches, operational_status)
    VALUES
        (v_train_id, '99001', 'Kanpur Lucknow Connector (Demo)', v_cnb,
         v_lko, 1, 'ACTIVE')
    ON CONFLICT DO NOTHING;

    IF NOT EXISTS (
        SELECT 1 FROM public.trains
        WHERE train_id = v_train_id AND train_number = '99001'
          AND source_station_id = v_cnb AND destination_station_id = v_lko
    ) THEN
        RAISE EXCEPTION 'Demo train ID/number 99001 is already used by different data';
    END IF;

    INSERT INTO public.train_stops
        (train_id, stop_sequence, station_id, arrival_time, departure_time, day_offset)
    VALUES
        (v_train_id, 1, v_cnb, TIME '01:45', TIME '02:00', 1),
        (v_train_id, 2, v_lko, TIME '08:00', TIME '08:00', 1)
    ON CONFLICT DO NOTHING;

    IF NOT EXISTS (
        SELECT 1 FROM public.train_stops
        WHERE train_id = v_train_id AND stop_sequence = 1 AND station_id = v_cnb
          AND departure_time = TIME '02:00' AND day_offset = 1
    ) OR NOT EXISTS (
        SELECT 1 FROM public.train_stops
        WHERE train_id = v_train_id AND stop_sequence = 2 AND station_id = v_lko
          AND arrival_time = TIME '08:00' AND day_offset = 1
    ) THEN
        RAISE EXCEPTION 'Existing stop rows for demo train 99001 do not match this script';
    END IF;

    INSERT INTO public.coaches (train_id, coach_number, coach_type)
    VALUES (v_train_id, 'D1', 'SL')
    ON CONFLICT (train_id, coach_number) DO NOTHING;

    SELECT coach_id INTO v_coach_id
    FROM public.coaches
    WHERE train_id = v_train_id AND coach_number = 'D1' AND coach_type = 'SL';
    IF v_coach_id IS NULL THEN
        RAISE EXCEPTION 'Could not find or create the demo coach';
    END IF;

    INSERT INTO public.seats (coach_id, seat_number, berth_type)
    SELECT v_coach_id, n::smallint,
           CASE (n - 1) % 6
               WHEN 0 THEN 'LOWER'
               WHEN 1 THEN 'MIDDLE'
               WHEN 2 THEN 'UPPER'
               WHEN 3 THEN 'LOWER'
               WHEN 4 THEN 'MIDDLE'
               ELSE 'SIDE_LOWER'
           END
    FROM generate_series(1, 12) AS n
    ON CONFLICT (coach_id, seat_number) DO NOTHING;

    INSERT INTO public.train_runs (train_id, journey_date, run_status)
    SELECT v_train_id, r.journey_date, 'SCHEDULED'
    FROM public.train_runs r
    JOIN public.trains source_train ON source_train.train_id = r.train_id
    WHERE source_train.train_number = '12005' AND r.run_status = 'SCHEDULED'
    ON CONFLICT (train_id, journey_date) DO NOTHING;
END;
$$;

COMMIT;

-- Result check: this should show 30 scheduled runs, a 12-seat coach, and the two stops.
SELECT t.train_number, t.train_name, count(DISTINCT r.run_id) AS scheduled_runs,
       count(DISTINCT s.seat_id) AS seats
FROM public.trains t
LEFT JOIN public.train_runs r ON r.train_id = t.train_id AND r.run_status = 'SCHEDULED'
LEFT JOIN public.coaches c ON c.train_id = t.train_id
LEFT JOIN public.seats s ON s.coach_id = c.coach_id
WHERE t.train_id = 99001
GROUP BY t.train_number, t.train_name;

SELECT ts.stop_sequence, st.station_code, ts.arrival_time, ts.departure_time, ts.day_offset
FROM public.train_stops ts
JOIN public.stations st ON st.station_id = ts.station_id
WHERE ts.train_id = 99001
ORDER BY ts.stop_sequence;
