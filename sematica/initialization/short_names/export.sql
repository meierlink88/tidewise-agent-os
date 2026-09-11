-- Operator-only read: never install Data DB credentials into AgentOS.
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL TIME ZONE 'UTC';
SELECT jsonb_build_object(
 'schema_version', 'entity-short-names.v1',
 'source', current_database(), 'exported_at', now(),
 'rows', (SELECT jsonb_agg(row ORDER BY row->>'entity_type', row->>'id') FROM (
 SELECT jsonb_build_object('entity_type','ChainNode','id',id,'name',name,'short_name',short_name) row FROM chain_node
 UNION ALL SELECT jsonb_build_object('entity_type','IndustryChain','id',id,'name',name,'short_name',short_name) FROM industry_chain
 UNION ALL SELECT jsonb_build_object('entity_type','MacroEconomic','id',id,'name',name,'short_name',short_name) FROM macro_economics
 UNION ALL SELECT jsonb_build_object('entity_type','GeopoliticRivalry','id',id,'name',name,'short_name',short_name) FROM geopolitic_rivalries
 ) facts));
COMMIT;
