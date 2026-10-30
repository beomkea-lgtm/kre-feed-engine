-- 빨랑튀어 '발송' 탭: 한 번에 모아 주는 JSON (앱이 60초 캐시)
create or replace function send_board() returns json language sql stable as $$
with kst as (select (now() at time zone 'Asia/Seoul')::date as today),
day0 as (select (today::timestamp at time zone 'Asia/Seoul') as t0 from kst),
c as (select meta->>'sequence' s, (meta->>'step')::int st, count(distinct lead_id) sent
      from events where type='contact' and meta->>'channel'='email' and meta ? 'sequence' and meta ? 'step' group by 1,2),
o as (select meta->>'sequence' s, (meta->>'step')::int st, count(distinct lead_id) opened
      from events where type='email_open' and meta ? 'sequence' and meta ? 'step' group by 1,2),
r as (select sequence s, step-1 st, count(*) filter (where status='replied') replied, count(*) filter (where status='bounced') bounced from enrollments group by 1,2),
w as (select sequence s, step st, count(*) filter (where status='active') waiting, count(*) filter (where status='paused') paused from enrollments group by 1,2),
a as (select e.sequence s, count(distinct ap.id) filter (where ap.status='approved') approved, count(distinct ap.id) applied, count(distinct v.lead_id) clicked
      from enrollments e left join applications ap on ap.lead_id=e.lead_id left join events v on v.lead_id=e.lead_id and v.type='apply_view' group by 1),
p as (select distinct on (sequence) sequence s, plan from enrollments where plan is not null order by sequence, created_at desc),
sched as (select to_char(next_send_at at time zone 'Asia/Seoul','YYYY-MM-DD') d, step, count(*) n
          from enrollments where status='active' and next_send_at < now() + interval '9 days' group by 1,2),
td as (select coalesce(meta->>'sequence', 'other') s, coalesce((meta->>'step')::int, 0) st, count(*) n
       from events, day0 where type='contact' and meta->>'channel'='email' and occurred_at >= day0.t0 group by 1,2)
select json_build_object(
  'now', now(),
  'caps', (select json_object_agg(key, value) from app_state where key in ('daily_send_cap','hourly_send_cap','paused_sequences','nudge_hold')),
  'today', (select coalesce(json_agg(td), '[]') from td),
  'hour', (select count(*) from events where type='contact' and meta->>'channel'='email' and occurred_at > now() - interval '1 hour'),
  'steps', (select coalesce(json_agg(json_build_object('s',c.s,'st',c.st,'sent',c.sent,'opened',coalesce(o.opened,0),'replied',coalesce(r.replied,0),'bounced',coalesce(r.bounced,0))), '[]')
            from c left join o using (s,st) left join r using (s,st)),
  'waiting', (select coalesce(json_agg(w), '[]') from w),
  'apply', (select coalesce(json_agg(a), '[]') from a),
  'plans', (select coalesce(json_agg(json_build_object('s',p.s,'i',i-1,'subject',b->>'subject','body',left(b->>'body',900),'files',jsonb_array_length(coalesce(b->'attachments','[]'::jsonb)),
            'wait_type',b->>'wait_type','gap_days',(b->>'gap_days')::numeric)), '[]') from p, jsonb_array_elements(p.plan) with ordinality x(b,i)),
  'sched', (select coalesce(json_agg(sched), '[]') from sched),
  'partners', (select coalesce(json_agg(json_build_object('id',id,'decided',to_char(decided_at at time zone 'Asia/Seoul','YYYY-MM-DD'),'posted',first_post_url is not null)), '[]')
               from applications where status='approved' and merged_into is null),
  'posts', (select count(*) from partner_posts)
);
$$;
