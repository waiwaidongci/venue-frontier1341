# 多代回算验收记录

环境：Python 3.11.2 / SQLAlchemy 2.0.36 / SciPy 1.17.1；SQLite 隔离库 + 同步 drain（无 Docker）。
`python -m pytest tests` → 22 passed。

| 要求 | 用例 | 结果 |
|---|---|---|
| 新代完成前旧代继续对外使用 | test_new_generation_keeps_old_result_visible_until_published、test_api full_generation_api_flow（入队后 GET /runs/{id} 仍返回第1代结果） | 通过 |
| 逐馆核对 θ / 可行状态 / 样本指纹后一次发布 | test_first_generation…（verification.checked_venues=12, ok=true）、test_multi_benchmark_selection_and_verification_of_every_scenario（验证每馆每情景）、test_verification_failure_blocks_publish（篡改 θ 被复算拦截） | 通过 |
| 任一部分失败不能发布 | test_retry_recomputes_only_failed_venues（首代失败，published_generation_id=None）、test_failure_after_old_published_keeps_old_result | 通过 |
| 恢复时只补未完成部分 | test_retry…（断言已完成馆不在重算调用内）、test_interrupted_generation_resumes_only_missing_parts | 通过 |
| 重放或乱序完成不得改写当前代 | test_replay…、test_out_of_order_publish…、test_publish_concurrency（双线程发布 + 晚到代） | 通过 |
| 页面展示各代差异与失败范围 | GenerationPanel 组件（components/GenerationPanel.tsx）+ GET /runs/{id}/diff、failure_scope 字段；构建通过 | 通过 |
| 假设固定引用原代、可迁移并保留前后依据 | test_case_pins_source_generation_and_migrates、test_case_api_pins_generation_and_migrates（含不可回迁与 CSV 列） | 通过 |
| 升级前历史研究 | test_legacy_run_is_adopted_as_generation_one（幂等登记为第1代，不重算） | 通过 |
