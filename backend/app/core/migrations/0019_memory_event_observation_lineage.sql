-- Every automatically projected memory event must retain the immutable
-- observation that proposed it.  NULL is deliberately retained for legacy or
-- curator-created events that predate this relation.
ALTER TABLE memory_events ADD COLUMN IF NOT EXISTS observation_id BIGINT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'memory_events_observation_workspace_fk'
    ) THEN
        ALTER TABLE memory_events
            ADD CONSTRAINT memory_events_observation_workspace_fk
            FOREIGN KEY (observation_id, workspace_id)
            REFERENCES memory_observations(id, workspace_id) ON DELETE CASCADE;
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS memory_events_observation_event_uidx
    ON memory_events(observation_id, event_type)
    WHERE observation_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS memory_events_observation_idx
    ON memory_events(observation_id)
    WHERE observation_id IS NOT NULL;
