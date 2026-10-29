-- Owner-only projection upgrade. Review before deployment. The writer retains SELECT/INSERT only.
-- Existing rows default active. Subsequent bounded reconciliation inserts tombstones for absent source IDs.
ALTER TABLE amem.sessions ADD COLUMN IF NOT EXISTS _amem_deleted UInt8 DEFAULT 0;
ALTER TABLE amem.runs ADD COLUMN IF NOT EXISTS _amem_deleted UInt8 DEFAULT 0;
ALTER TABLE amem.deviations ADD COLUMN IF NOT EXISTS _amem_deleted UInt8 DEFAULT 0;
ALTER TABLE amem.scores ADD COLUMN IF NOT EXISTS _amem_deleted UInt8 DEFAULT 0;
ALTER TABLE amem.skill_calls ADD COLUMN IF NOT EXISTS _amem_deleted UInt8 DEFAULT 0;
ALTER TABLE amem.rsi_proposals ADD COLUMN IF NOT EXISTS _amem_deleted UInt8 DEFAULT 0;
ALTER TABLE amem.rsi_decisions ADD COLUMN IF NOT EXISTS _amem_deleted UInt8 DEFAULT 0;
ALTER TABLE amem.research_runs ADD COLUMN IF NOT EXISTS _amem_deleted UInt8 DEFAULT 0;
ALTER TABLE amem.rsi_eval_results ADD COLUMN IF NOT EXISTS _amem_deleted UInt8 DEFAULT 0;
