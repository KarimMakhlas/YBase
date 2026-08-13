-- One observation can establish an event for its own canonical identity and
-- for a referenced decision/question that it revisits or resolves.
DROP INDEX IF EXISTS memory_events_observation_event_uidx;
CREATE UNIQUE INDEX memory_events_observation_node_event_uidx
    ON memory_events(observation_id, node_id, event_type)
    WHERE observation_id IS NOT NULL;
