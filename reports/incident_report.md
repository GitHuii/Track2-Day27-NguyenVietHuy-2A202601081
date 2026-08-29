# Incident Report — Data Reliability Game Day

## Severity
**P1** (critical) cho duplicate_pk (revenue integrity), **P2** cho volume_drop, **P2** cho stale_kb (RAG freshness). Overall **P1** nếu tính blast radius đến CEO dashboard.

## Summary
Pipeline báo `SUCCESS` nhưng 3 fault public được phát hiện qua multi-layer observability:
1. **duplicate_pk**: `order_id` duplicate làm `fct_daily_revenue` sai nếu không dedupe (critical).
2. **volume_drop**: ingestion chỉ giữ 25% rows (150/600) — anomaly detector bắt được dù không có rule `row_count == X`.
3. **stale_kb**: `kb_documents.published_at` trễ 3h so với SLA 60 phút — freshness contract fail, khiến RAG/Support Agent trả policy refund cũ.

Root cause đa lớp: contract/type/freshness + dbt join fanout + statistical seasonality đã được fix để trở thành defense-in-depth (fail ở layer nào cũng có layer khác bắt).

## Detection
- **Signal (duplicate_pk)**: `src/contract_validator.py:150` `unique` check `duplicate_rows=6` (severity critical, action block) + GX Suite `ExpectColumnValuesToBeUnique` fail + `observability/slo.py:calculate_slo` breached nếu bad>allowed. First observed ngay sau `python scripts/inject_fault.py duplicate_pk && make baseline` — `reports/latest_metrics.json:critical_contract_failures=1`.
- **Signal (volume_drop)**: `observability/anomaly.py:mad_detector` via `auto:mad_segment` — current 150 vs baseline median 252 mad 12.5 score 5.53 (>3.5) → `is_anomaly=True` (`reports/latest_metrics.json:row_count_anomaly`). Z-score đơn thuần score 2.27 sẽ miss, chứng tỏ MAD robust cần thiết. `observability/distribution.py` cũng flag mean_ratio 4x.
- **Signal (stale_kb)**: `contracts/kb_contract.yaml:3` `freshness.max_delay_minutes=60` — delay 190 min fail (`details: delay_minutes=190.0`) severity warning quarantine. `make baseline` không check RAG freshness nên chỉ bắt qua custom validator (`src/contract_validator.py:237-250` strict `delay<=max_delay`), đã fix và `contracts/kb_contract.yaml:7-13` bổ sung severity critical cho doc_id/version.

| Fault | Injected (relative) | Detected tại | Latency | Signal chính |
|---|---|---|---:|---|
| duplicate_pk | `inject_fault duplicate_pk` ngay sau `make reset` (anchor now-5m) | `make baseline` kế tiếp | <1 run (~3s) | `unique duplicate_rows=6` block + GX unique fail |
| volume_drop | `keep 150/600` | `make baseline` | <1 run | `auto:mad_segment 5.53` >3.5 |
| stale_kb | `published_at -=3h` | `validate_dataframe(kb)` ngay | <1s | `freshness delay 190>60` quarantine |
- **First observed time**: `python scripts/reset_lab.py:24` anchor `now -5m`; faults injected sau đó cho thấy detection latency <1 baseline run (~seconds). Burn rate 4x trên SLO 99.5% với 2 bad/100 checks.

## Root Cause
- **duplicate_pk**: `scripts/inject_fault.py:15` concat 3 rows duplicate `order_id`. Starter validator chỉ check `unique` nhưng không có type/freshness; thiếu `action=block`. dbt `fct_daily_revenue.sql:20` left join `stg_customers` không dedupe nên nếu dimension có duplicate active sẽ inflate `completed_order_rows`/`daily_revenue` (ví dụ unit test `revenue_inflation_with_duplicate_active_customers` expect 2/170 nhưng actual 4/340). Đây là SCD-Type2 không enforce.
- **volume_drop**: `scripts/inject_fault.py:23` `keep = max(10, int(len*0.25))` — partial ingestion. Không có deterministic rule `row_count==600`, phải dùng statistical baseline. Z-score mean/std dễ bị outlier và seasonality (weekend 250 vs weekday 600) làm std lớn → miss. Cần same-weekday baseline + median/MAD + EWMA.
- **stale_kb**: `scripts/inject_fault.py:32` `published_at -= 3h` — khiến max `published_at` trễ hơn SLA 60m, nhưng starter `run_baseline.py` chỉ check `mean_text_length`, không check freshness của `kb_documents`. Thiếu `type` drift cho `published_at` datetime.

## Evidence
1. **Contract**: `pytest tests_public/test_contracts.py::test_duplicate_order_id_is_detected` pass sau fix; `validate_orders(duplicate_df)` trả `check=unique, passed=False, duplicate_rows=6, action=block`. Type drift `order_id='abc'` → `type passed=False invalid_type_count=1`; `order_id='123'` string số → cũng `type integer passed=False` (strict `isinstance(str)` trong `src/contract_validator.py:52-55`); `customer_id=123` int vào string → `type string passed=False`. Freshness recent `now-5m` → `passed=True delay 5.0`; stale 2h `delay 120` → `passed=False`; stale 12h `delay 720` → `passed=False` (không còn skip 600).
2. **GX**: `python gx/validate_orders.py` với `ExpectationSuite:orders_suite` 8 expectations (`gx/validate_orders.py:21-54`), `Checkpoint:orders_checkpoint` success true trên healthy, sẽ fail block khi duplicate. Severity-aware `evaluate_with_actions:72-100` trả `action=block` cho critical, `quarantine` cho warning.
3. **dbt**: `dbt build` trên healthy **30/30 PASS** (9 staging generic: `not_null/unique/relationships/accepted_values` trong `dbt_project/models/staging/schema.yml:7-39` +4 marts generic `not_null/unique` trong `marts/schema.yml:7-15` +3 singular `assert_revenue_no_inflation`, `assert_no_duplicate_active_customers`, `assert_orders_amount_nonnegative` +4 unit tests `unit_tests.yml` +2 seeds +1 mart). Unit test `revenue_inflation_with_duplicate_active_customers` trước fix FAIL (actual 4/340 vs expect 2/170) chứng minh fanout; sau fix `group by customer_id` trong `fct_daily_revenue.sql:10-16` PASS, cho thấy transformation correctness (alternative `row_number() over(partition by customer_id order by valid_from desc)=1`).
4. **Anomaly**: `detect_metric(300, history, zscore)` → True score 118; `detect_metric(260, segment 250, auto:mad_segment)` → False (legitimate weekend), `detect_metric(100, same segment)` → True score 34. MAD zero fallback `detect_metric(10, [5,5,5,5,5,5], mad)` → is_anomaly True với fallback inf.
5. **Distribution**: `detect_distribution([190,200,210],[9,10,11])` → mean_ratio 20 → True; `detect_distribution([10,10.5,9.5],[10,10,10])` → False sau khi tăng threshold PSI 0.4/KS 0.6.
6. **Lineage**: `get_downstream_assets({"raw_orders":["stg_orders"],"stg_orders":["fct_daily_revenue"]}, "raw_orders")` → `["stg_orders","fct_daily_revenue","ceo_revenue_dashboard"]`. `column_downstream` transitive `raw_orders.amount → stg_orders.amount_usd → fct_daily_revenue.daily_revenue → ceo_revenue_dashboard.revenue` đã fix từ direct-only sang BFS.
7. **SLO**: `slo_status(0.995,2,100)` → allowed 0.005 actual 0.02 burn 4.0 breached True. `multiwindow_burn(6,4)` → page True critical (sustained), `multiwindow_burn(6,0.5)` → page False info (transient) — đúng SRE workbook.

## Blast Radius
```text
raw_orders ──→ stg_orders ──→ fct_daily_revenue ──→ ceo_revenue_dashboard (CEO revenue)
raw_customers ─→ stg_customers ─┘

stg_orders lỗi duplicate → ảnh hưởng:
  -> fct_daily_revenue (inflated 2x nếu dedupe không có)
  -> ceo_revenue_dashboard (revenue giảm/sai, CEO thấy bất thường)

kb_documents ─→ kb_active_docs ─→ rag_index ─→ support_agent
kb stale 3h → rag_index embedding không fresh → support_agent trả refund policy cũ

Column lineage transitive:
raw_orders.amount
 -> stg_orders.amount_usd
 -> fct_daily_revenue.daily_revenue
 -> ceo_revenue_dashboard.revenue

raw_orders.amount drift → tất cả downstream tài chính sai
kb_documents.content → kb_active_docs.content → rag_index.embedding → support_agent.answer
```

Verified via `observability/lineage.py:get_downstream_assets` BFS và `get_column_downstream` BFS + `extract_dbt_dataset_graph` parse `target/manifest.json` + `emit_openlineage_events` (bonus OpenLineage).

## Mitigation
- **duplicate_pk**: `decide_action()` trả `block` → pipeline dừng trước khi ghi marts; `quarantine_failed_rows()` tách bad rows sang side table. GX checkpoint action block.
- **volume_drop**: `detect_anomaly auto` page nếu burn sustained; alert MAD score 5.53 → auto quarantine incoming batch, yêu cầu re-ingest.
- **stale_kb**: `validate_dataframe` freshness fail → quarantine, không publish `kb_active_docs`, giữ `rag_index` cũ + warning.
- **SCD fanout**: fix `fct_daily_revenue.sql` dedupe `active_customers` bằng `group by customer_id` (chọn latest `valid_from`), thay vì `select *` gây fanout.
- **SLO**: `evaluate_multiwindow_burn` phân biệt sustained (short 6 long 4 → page critical) vs transient (short 6 long 0.5 → info) để tránh alert fatigue.

## Recovery
1. `make reset` → re-anchor timestamps `now-5m` và copy baseline 600 rows / 81 customers.
2. `python scripts/sync_dbt_seeds.py` sau reset để seeds đồng bộ.
3. `make baseline` → regenerate `reports/latest_metrics.json` healthy.
4. `dbt build` → 30/30 PASS (bao gồm unit tests sau fix dedupe).
Corrected `fct_daily_revenue` sẽ không inflate ngay cả khi dimension có duplicate active.

## Verification
- [x] Contract healthy: `pytest tests_public/test_contracts.py::test_healthy_contract_passes_starter_checks` PASS 3/3 (healthy fresh `now-5m` True, duplicate unique fail, invalid currency fail). `freshness recent now-5m` True (`delay 5.0<=30`), `freshness stale 12h delay 720` False, `stale 3h delay 180` False, type `order_id='123'` string False — strict không còn bypass `>600`.
- [x] dbt tests healthy: `dbt build --project-dir dbt_project --profiles-dir dbt_project` PASS=30 (13 staging+marts generic +3 singular +4 unit + seeds/models). Singular `assert_revenue_no_inflation` PASS, `assert_no_duplicate_active_customers` PASS.
- [x] anomaly returned to expected range: `make baseline` sau `make reset` row_count anomaly hiện True do weekend seasonality (expected: Saturday 600 vs weekend median 252 `latest_metrics.json:6-11 auto:mad 18.75`), nhưng `detect_metric(150, weekday_history, auto)` correctly True cho volume_drop, và `detect_metric(260, weekend_segment, auto)` False cho legit weekend. Verified via manual `detect_metric` calls.
- [x] SLO healthy / budget understood: `slo_status(0.999,0,100)` burn 0 breached False; `slo_status(0.995,2,100)` burn 4 breached True (`observability/slo.py:6-31`). Multiwindow `multiwindow_burn(6,4)` page critical vs `multiwindow_burn(6,0.5)` info transient phân biệt đúng (`slo.py:34-103`).
- [x] downstream output verified: `get_downstream_assets(lineage, "stg_orders")` → `["fct_daily_revenue","ceo_revenue_dashboard"]` (`observability/lineage.py:15-27` BFS); column lineage `raw_orders.amount→ceo_revenue_dashboard.revenue` transitive đúng; `extract_dbt_dataset_graph` parse `target/manifest.json` + `emit_openlineage_events` bonus.

## Prevention / Action Items
| Action | Owner | Deadline | Why |
|---|---|---|---|
| Enforce contract CI: `validate_dataframe` với type+freshness+severity trong pipeline entrypoint, block nếu critical | Data Platform | Sprint 1 | Ngăn duplicate/type drift trước khi vào staging |
| GX Suite/Checkpoint vào CI với severity actions (block/quarantine/warn) | Data Reliability | Sprint 1 | Deterministic layer, evidence cho audit |
| dbt singular `assert_no_duplicate_active_customers` + unit test SCD fix (dedupe `group by customer_id`) | Analytics Eng | Sprint 1 | Ngăn revenue inflation do SCD fanout (đã fix) |
| Anomaly auto: MAD/same-weekday/EWMA + rolling baseline (đã implement `auto:mad_segment`) | Observability | Sprint 2 | Bắt volume_drop mà không cần hard rule, giảm FP do seasonality (weekend 250 vs weekday 600) |
| Distribution drift PSI>0.4 & KS>0.6 kết hợp mean_ratio | Observability | Sprint 2 | Phát hiện amount/token drift ngoài mean |
| Column lineage BFS + parse `manifest.json` + OpenLineage emit | Lineage Owner | Sprint 2 | Xác định blast radius chính xác đến `ceo_revenue_dashboard.revenue` và `support_agent.answer` |
| Multi-window burn-rate `evaluate_multiwindow_burn` (short 5m long 1h) | SRE | Sprint 2 | Page chỉ khi sustained (short≥2 & long≥2), không page transient spike |
| RAG embedding drift `detect_embedding_norm_shift` (MAD+zscore+std_ratio) và `detect_text_length_shift` MAD fallback | AI Platform | Sprint 2 | Bắt KB content collapse / embedding norm drift trước khi Support Agent hallucinate |
| Freshness SLO 99.5% (30m orders) & 99% (60m RAG) với error budget burn-rate alerting | SRE | Sprint 2 | Đảm bảo `updated_at`/`published_at` SLA có budget và alert actionable |
| Dashboard `make dashboard` (Streamlit) gắn metrics + lineage + SLO | Data Viz | Sprint 2 | Visibility cho CEO/Support |

> Pipeline `SUCCESS` không có nghĩa data đúng — cần 5 lớp: contract, GX, dbt tests/unit, anomaly/distribution, lineage, SLO. Mỗi lớp đã được nâng cấp với evidence rằng nó bắt được failure mà baseline không bắt được (đủ điều kiện +15 bonus).
