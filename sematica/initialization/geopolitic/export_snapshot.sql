-- Operator-only Data export. Run against the approved Data PostgreSQL database.
-- AgentOS runtime must not hold Data database credentials or execute this query.
-- One statement supplies a consistent MVCC snapshot; source_count detects a lossy join.
SELECT jsonb_build_object(
    'schema_version', 'geopolitic-projection-snapshot.v1',
    'source_count', (SELECT count(*) FROM geopolitic_rivalries),
    'items', (
        SELECT jsonb_agg(
            to_jsonb(r) || jsonb_build_object(
                'domain_code', d.code,
                'domain_name', d.name,
                'domain_description', d.description,
                'tactics', d.tactics,
                'domain_created_at', d.created_at,
                'domain_updated_at', d.updated_at
            ) ORDER BY r.id
        )
        FROM geopolitic_rivalries r
        JOIN geopolitic_domains d ON d.id = r.geopolitic_domain_id
    )
);
