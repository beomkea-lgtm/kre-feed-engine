-- 속도: 자주 쓰는 조회 인덱스 + 무거운 바구니 집계는 10분마다 미리 계산
create index if not exists leads_email_idx on leads ((contact->>'email'));
create index if not exists leads_type_idx on leads (type);
create index if not exists rt_trades_sgg_date_idx on rt_trades (sgg_cd, deal_date) where not cancel_flag;
create index if not exists events_type_time_idx on events (type, occurred_at);
create index if not exists events_seq_idx on events ((meta->>'sequence'), type);
create materialized view if not exists basket_counts_mv as select * from basket_counts;
create unique index if not exists basket_counts_mv_tab on basket_counts_mv (tab);
-- pg_cron (Supabase에서 실행): basket-counts */10 · seq-run '* 1-12 * * 1-5' · seq-run-evening '* 13 * * 1-5'
--   inbox-check '*/10 0,14-23 * * 1-5' (?inbox=1&send=0) · inbox-check-weekend '*/10 * * * 0,6' · partner-retention '* 0-1 * * 1-5'
