CREATE TEMP TABLE mvp_removed_source_nodes ON COMMIT DROP AS
SELECT DISTINCT cl.node_id
FROM chunk_links cl
JOIN chunks c ON c.id = cl.chunk_id
JOIN documents d ON d.id = c.document_id
WHERE d.source IN ('jira', 'linear', 'confluence', 'discord', 'googledocs', 'figma');

DELETE FROM oauth_states
WHERE provider IN ('jira', 'linear', 'confluence', 'discord', 'googledocs', 'figma');

DELETE FROM documents
WHERE source IN ('jira', 'linear', 'confluence', 'discord', 'googledocs', 'figma');

DELETE FROM source_connections
WHERE provider IN ('jira', 'linear', 'confluence', 'discord', 'googledocs', 'figma');

DELETE FROM memory_nodes n
USING mvp_removed_source_nodes affected
WHERE n.id = affected.node_id
  AND NOT EXISTS (SELECT 1 FROM chunk_links cl WHERE cl.node_id = n.id);

DROP TABLE IF EXISTS answer_feedback;
DROP TABLE IF EXISTS decision_shares;
DROP TABLE IF EXISTS digests;
DROP TABLE IF EXISTS workspace_invites;
DROP TABLE IF EXISTS activity_days;
