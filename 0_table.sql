-- =====================================================================
-- deployment_tracker : optimized schema + migration (idempotent)
-- Run with:  psql -v ON_ERROR_STOP=1 -f deployment_tracker_optimized.sql
-- =====================================================================
BEGIN;

-- ---------------------------------------------------------------------
-- 1. Table (new databases). Status CHECK is added in step 4 so there is
--    a single definition of the allowed statuses.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.deployment_tracker (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    cr_number VARCHAR(50) NOT NULL,
    rfid CHAR(8) NOT NULL DEFAULT LPAD(
        FLOOR(RANDOM() * 100000000)::BIGINT::TEXT, 8, '0'
    ),
    deployment_date DATE NOT NULL DEFAULT CURRENT_DATE,
    slots VARCHAR(100),
    slot_start TIMESTAMP WITHOUT TIME ZONE,
    slot_end TIMESTAMP WITHOUT TIME ZONE,
    application_name VARCHAR(150) NOT NULL,
    deployment_type VARCHAR(20) NOT NULL
        CHECK (deployment_type IN ('Automation', 'Manual')),
    change_type VARCHAR(30) NOT NULL
        CHECK (change_type IN (
            'Normal CR', 'Emergency CR', 'Standard CR',
            'Forward Fix CR', 'Bug Fix CR'
        )),
    released_by VARCHAR(150),
    verified_and_deployed_by VARCHAR(150),
    change_requestor VARCHAR(150),
    release_notes TEXT,
    incident_number VARCHAR(50),
    comments TEXT,
    roll_back VARCHAR(3) NOT NULL DEFAULT 'No'
        CHECK (roll_back IN ('Yes', 'No')),
    status VARCHAR(30) NOT NULL DEFAULT 'Scheduled Deployment',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT chk_rfid_8_digits CHECK (rfid ~ '^[0-9]{8}$'),
    CONSTRAINT deployment_slot_pair_check CHECK (
        (slot_start IS NULL AND slot_end IS NULL)
        OR (slot_start IS NOT NULL AND slot_end IS NOT NULL)
    ),
    CONSTRAINT chk_slot_duration CHECK (
        slot_start IS NULL OR slot_end = slot_start + INTERVAL '30 minutes'
    )
);

-- Older tables: make sure these columns exist (one ALTER = one lock).
ALTER TABLE public.deployment_tracker
    ADD COLUMN IF NOT EXISTS slot_start TIMESTAMP WITHOUT TIME ZONE,
    ADD COLUMN IF NOT EXISTS slot_end   TIMESTAMP WITHOUT TIME ZONE,
    ADD COLUMN IF NOT EXISTS comments   TEXT;

-- ---------------------------------------------------------------------
-- 2. CR numbers and RFIDs repeat across rows: drop any UNIQUE constraint
--    that covers only cr_number or only rfid.
-- ---------------------------------------------------------------------
DO $$
DECLARE
    c RECORD;
BEGIN
    FOR c IN
        SELECT con.conname
        FROM pg_constraint con
        WHERE con.conrelid = 'public.deployment_tracker'::regclass
          AND con.contype = 'u'
          AND (
              SELECT array_agg(att.attname ORDER BY k.ord)
              FROM unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord)
              JOIN pg_attribute att
                ON att.attrelid = con.conrelid AND att.attnum = k.attnum
          ) IN (ARRAY['cr_number']::name[], ARRAY['rfid']::name[])
    LOOP
        EXECUTE format('ALTER TABLE public.deployment_tracker DROP CONSTRAINT %I', c.conname);
    END LOOP;
END $$;

-- ---------------------------------------------------------------------
-- 3. Remove the overlap exclusion constraint.
--    The app allows N CRs per slot (Super Manager "CRs per slot"), and the
--    app already serializes bookings with advisory locks. With N > 1 this
--    constraint rejects every second booking on a slot.
--    If you will ALWAYS use capacity 1, you can re-create it:
--      CREATE EXTENSION IF NOT EXISTS btree_gist;
--      ALTER TABLE public.deployment_tracker ADD CONSTRAINT
--        no_overlapping_deployment_slots EXCLUDE USING gist (
--          tsrange(slot_start, slot_end, '[)') WITH &&)
--        WHERE (slot_start IS NOT NULL AND slot_end IS NOT NULL
--          AND LOWER(TRIM(COALESCE(status, ''))) NOT IN ('cancelled','rejected'));
-- ---------------------------------------------------------------------
ALTER TABLE public.deployment_tracker
    DROP CONSTRAINT IF EXISTS no_overlapping_deployment_slots;

-- ---------------------------------------------------------------------
-- 4. Statuses: drop old CHECK -> migrate data in ONE pass -> add new CHECK.
--    (The old script added the old CHECK first, so its UPDATE to
--    'Scheduled Deployment' would have failed.)
-- ---------------------------------------------------------------------
ALTER TABLE public.deployment_tracker
    DROP CONSTRAINT IF EXISTS deployment_tracker_status_check;

ALTER TABLE public.deployment_tracker
    ALTER COLUMN status SET DEFAULT 'Scheduled Deployment';

UPDATE public.deployment_tracker
SET status = CASE LOWER(TRIM(status))
        WHEN 'pending'     THEN 'Scheduled Deployment'
        WHEN 'scheduled'   THEN 'Scheduled Deployment'
        WHEN 'in progress' THEN 'Scheduled Deployment'
        WHEN ''            THEN 'Scheduled Deployment'
        WHEN 'successful'  THEN 'Deployment Completed'
        WHEN 'failed'      THEN 'Deployment Failed'
        WHEN 'rolled back' THEN 'Rolled'
        ELSE status
    END
WHERE LOWER(TRIM(status)) IN
      ('pending', 'scheduled', 'in progress', '', 'successful', 'failed', 'rolled back');

ALTER TABLE public.deployment_tracker
    ADD CONSTRAINT deployment_tracker_status_check
    CHECK (status IN (
        'Scheduled Deployment', 'Deployment Completed', 'Deployment Failed',
        'On Hold', 'Postponed', 'Rolled', 'Cancelled',
        'Approved', 'Rejected'
    ));

-- ---------------------------------------------------------------------
-- 5. Indexes matched to the queries the app actually runs
-- ---------------------------------------------------------------------

-- Daily lists, slot grid, running-status: WHERE deployment_date = ? ORDER BY slot_start
-- (supersedes idx_deployment_date, which was only a prefix of this).
DROP INDEX IF EXISTS public.idx_deployment_date;
CREATE INDEX IF NOT EXISTS idx_dt_date_slot
    ON public.deployment_tracker (deployment_date, slot_start);

-- Capacity checks, slot availability, calendar "booked" counts: active rows only.
-- Predicate text must match the queries exactly to be used.
CREATE INDEX IF NOT EXISTS idx_dt_active_date_slot
    ON public.deployment_tracker (deployment_date, slot_start)
    WHERE LOWER(TRIM(COALESCE(status, ''))) NOT IN ('cancelled', 'rejected');

-- RFID uniqueness probe at save time (the UNIQUE constraint is gone).
CREATE INDEX IF NOT EXISTS idx_dt_rfid
    ON public.deployment_tracker (rfid);

-- CR lookups: LOWER(TRIM(cr_number)) = LOWER(TRIM(?))
CREATE INDEX IF NOT EXISTS idx_deployment_tracker_cr_number_normalized
    ON public.deployment_tracker (LOWER(TRIM(cr_number)));

-- Kept as-is. Check usage and drop if idx_scan stays 0:
--   SELECT indexrelname, idx_scan FROM pg_stat_user_indexes
--   WHERE relname = 'deployment_tracker' ORDER BY idx_scan;
CREATE INDEX IF NOT EXISTS idx_application_name
    ON public.deployment_tracker (application_name);
CREATE INDEX IF NOT EXISTS idx_deployment_status
    ON public.deployment_tracker (status);

COMMIT;

-- Refresh planner statistics after the migration.
ANALYZE public.deployment_tracker;