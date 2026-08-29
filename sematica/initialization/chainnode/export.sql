BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL TIME ZONE 'UTC';

SELECT jsonb_build_object(
    'kind', 'chain_node',
    'payload', jsonb_build_object(
        'id', node.id,
        'name', node.name,
        'aliases', to_jsonb(node.aliases),
        'definition', node.definition,
        'review_status', node.review_status,
        'created_at', node.created_at,
        'updated_at', node.updated_at
    )
)
FROM chain_node AS node
WHERE node.review_status = 'approved'
ORDER BY node.id;

SELECT jsonb_build_object(
    'kind', 'membership',
    'payload', jsonb_build_object(
        'industry_chain_id', membership.industry_chain_id,
        'industry_chain_name', chain.name,
        'chain_node_id', membership.chain_node_id,
        'chain_node_name', node.name,
        'position', membership.position,
        'contextual_stage', membership.contextual_stage
    )
)
FROM industry_chain_node_memberships AS membership
JOIN industry_chain AS chain ON chain.id = membership.industry_chain_id
JOIN chain_node AS node ON node.id = membership.chain_node_id
WHERE membership.review_status = 'approved'
  AND membership.status = 'active'
  AND node.review_status = 'approved'
ORDER BY membership.industry_chain_id, membership.position, membership.chain_node_id;

SELECT jsonb_build_object(
    'kind', 'graph_edge',
    'payload', jsonb_build_object(
        'id', edge.id,
        'industry_chain_id', edge.industry_chain_id,
        'industry_chain_name', chain.name,
        'from_chain_node_id', edge.from_chain_node_id,
        'from_node_name', source_node.name,
        'to_chain_node_id', edge.to_chain_node_id,
        'to_node_name', target_node.name,
        'relation_type', edge.relation_type
    )
)
FROM industry_chain_graph_edges AS edge
JOIN industry_chain AS chain ON chain.id = edge.industry_chain_id
JOIN chain_node AS source_node ON source_node.id = edge.from_chain_node_id
JOIN chain_node AS target_node ON target_node.id = edge.to_chain_node_id
WHERE edge.review_status = 'approved'
  AND edge.status = 'active'
  AND edge.segment_kind = 'direct_candidate'
  AND source_node.review_status = 'approved'
  AND target_node.review_status = 'approved'
ORDER BY edge.industry_chain_id, edge.from_chain_node_id, edge.to_chain_node_id,
         edge.relation_type, edge.id;

COMMIT;
